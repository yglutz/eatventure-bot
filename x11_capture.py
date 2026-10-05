"""Read-only scrcpy X11 client capture; all XTest input lives in MouseController.

Requires python-xlib on Linux only. The supervisor owns Xvfb, the window manager,
scrcpy and focus acquisition. No focus stealing, ADB screenshots or input here.
The entire canonical client (including scrcpy letterboxes) is retained.
"""
import logging
from numbers import Integral
import threading

import numpy as np

from adb_capture import ADBCapture

logger = logging.getLogger(__name__)


class X11CaptureError(RuntimeError):
    """Missing/ambiguous window, unsafe state, or X11 transport failure."""


class X11Capture:
    def __init__(self, display_name, window_title='op', *, serial,
                 expected_package, adb_path='adb', process_alive=None,
                 target_size=(360, 780)):
        if not isinstance(display_name, str) or not display_name:
            raise ValueError('An explicit X11 display name is required')
        if not isinstance(window_title, str) or not window_title or not callable(process_alive):
            raise ValueError('A window title and process liveness callback are required')
        if (len(target_size) != 2 or any(isinstance(v, bool) or not isinstance(v, int)
                                       or v < 2 for v in target_size)):
            raise ValueError('target_size must contain two integer dimensions >= 2')
        self.android_guard = ADBCapture(serial, expected_package, adb_path=adb_path)
        self.transport = self.android_guard.transport
        self.expected_package = expected_package
        self.display_name = display_name
        self.window_title = window_title
        self.process_alive = process_alive
        self.calibrated_size = tuple(target_size)
        self.target_width, self.target_height = self.calibrated_size
        # This is the desktop CLIENT size, not an inferred Android/video size.
        self.native_size = None
        self.last_capture_ok = False
        self._size_changed = self._failed = False
        self.lock = threading.RLock()
        self._capture_lock = threading.Lock()
        self.display = None
        try:
            from Xlib import X, display
            self.X = X
            self.display = display.Display(display_name)
            # Void X requests report errors asynchronously, not by raising from
            # sync(). Pin a handler so rejected geometry/XTest requests latch.
            self._transport_error = None
            self.display.set_error_handler(self._on_x11_error)
            self.root = self.display.screen().root
            with self.lock:
                self.window = self._find_window()
        except Exception as exc:
            self.close()
            raise X11CaptureError(f'Cannot initialize X11 capture: {exc}') from exc

    def _find_window(self):
        matches = []
        pending = [self.root]
        while pending:
            parent = pending.pop()
            for child in parent.query_tree().children:
                attrs = child.get_attributes()
                # Unmapped clients do not qualify, including minimized ones.
                if attrs.map_state == self.X.IsViewable:
                    title = child.get_wm_name()
                    atom = self.display.intern_atom('_NET_WM_NAME', only_if_exists=True)
                    prop = child.get_full_property(atom, self.X.AnyPropertyType) if atom else None
                    if prop is not None:
                        title = prop.value.decode('utf-8', errors='strict') if isinstance(prop.value, bytes) else str(prop.value)
                    classes = child.get_wm_class() or ()
                    if title == self.window_title and any(str(c).lower() == 'scrcpy' for c in classes):
                        matches.append(child)
                pending.append(child)
        if len(matches) != 1:
            raise X11CaptureError('Expected exactly one mapped scrcpy window with requested title')
        return matches[0]

    def _on_x11_error(self, error, request):
        with self.lock:
            self._transport_error = error
            self._failed = True
            self.last_capture_ok = False

    def check_transport(self):
        """Called under the shared Xlib lock after synchronous input dispatch."""
        if self._failed or self.display is None:
            raise X11CaptureError(f'X11 transport disabled: {self._transport_error}')

    def resize_window(self):
        """Optional geometry-only setup, BEFORE first capture/calibration.

        The WM may apply resize asynchronously; supervisor must wait for it.
        A changed calibrated size cannot be repaired by this method.
        """
        with self.lock:
            if self.native_size is not None or self._failed or self._size_changed:
                raise X11CaptureError('Resize requires a new, uncalibrated capture instance')
            try:
                self.window.configure(width=self.target_width, height=self.target_height)
                self.display.sync()
                self.check_transport()
            except Exception as exc:
                self._failed = True
                raise X11CaptureError(f'X11 resize failed: {exc}') from exc

    def get_window_rect(self):
        with self.lock:
            try:
                geometry = self.window.get_geometry()
                position = self.root.translate_coords(self.window, 0, 0)
                return (position.x, position.y, geometry.width, geometry.height)
            except Exception as exc:
                self._failed = True
                self.last_capture_ok = False
                raise X11CaptureError(f'X11 geometry failed: {exc}') from exc

    def is_window_active(self):
        """Cheap live desktop guard; intentionally never issues ADB commands."""
        with self.lock:
            try:
                if not self.process_alive():
                    # A restarted process must never inherit this calibration.
                    self._failed = True
                active = not (self._failed or self._size_changed)
                if active:
                    size = self.get_window_rect()[2:]
                    if size != self.calibrated_size:
                        self._size_changed = True
                        active = False
                if active:
                    active = (self._find_window().id == self.window.id and
                              self.display.get_input_focus().focus == self.window)
                if active:
                    atom = self.display.intern_atom('_NET_ACTIVE_WINDOW', only_if_exists=True)
                    prop = self.root.get_full_property(atom, self.X.AnyPropertyType) if atom else None
                    if prop is not None:
                        active = len(prop.value) == 1 and int(prop.value[0]) == self.window.id
            except Exception:
                self._failed = True
                active = False
                logger.exception('X11 live guard failed')
            if not active:
                self.last_capture_ok = False
            return bool(active)

    def calibration_identity(self):
        with self.lock:
            if (self.native_size is None or not self.last_capture_ok
                    or not self.is_window_active()):
                raise X11CaptureError('Successful capture required before calibration')
            return {'backend': 'scrcpy-x11', 'serial': self.transport.serial,
                    'package': self.expected_package,
                    'display_name': self.display_name, 'window_id': self.window.id,
                    'canonical_size': list(self.calibrated_size),
                    'native_size': list(self.native_size),
                    'coordinate_system': 'canonical-client-pixels',
                    'video_size': None}

    def _decode(self, reply, geometry):
        """Decode ZPixmap; DirectColor indices require fresh window colormap LUTs."""
        info = self.display.display.info
        formats = [f for f in info.pixmap_formats if f.depth == reply.depth]
        visuals = [v for d in self.display.screen().allowed_depths
                   for v in d.visuals if v.visual_id == reply.visual]
        if len(formats) != 1 or len(visuals) != 1:
            raise X11CaptureError('Unknown pixmap format/visual')
        fmt, visual = formats[0], visuals[0]
        direct = visual.visual_class == self.X.DirectColor
        if visual.visual_class not in (self.X.TrueColor, self.X.DirectColor) or fmt.bits_per_pixel not in (16, 24, 32):
            raise X11CaptureError('Only 16/24/32-bit TrueColor/DirectColor is supported')
        width, height = geometry.width, geometry.height
        pad = fmt.scanline_pad
        if pad not in (8, 16, 32):
            raise X11CaptureError('Unsupported scanline padding')
        stride = ((width * fmt.bits_per_pixel + pad - 1) // pad) * (pad // 8)
        if len(reply.data) != stride * height:
            raise X11CaptureError('Truncated/malformed ZPixmap')
        raw = np.frombuffer(reply.data, dtype=np.uint8).reshape(height, stride)
        octets = raw[:, :width * (fmt.bits_per_pixel // 8)].reshape(height, width, -1)
        if info.image_byte_order not in (self.X.LSBFirst, self.X.MSBFirst):
            raise X11CaptureError('Unsupported server byte order')
        order = range(octets.shape[2]) if info.image_byte_order == self.X.LSBFirst else reversed(range(octets.shape[2]))
        pixels = np.zeros((height, width), dtype=np.uint32)
        for shift, index in enumerate(order):
            pixels |= octets[:, :, index].astype(np.uint32) << (8 * shift)
        channels = []
        masks = (visual.blue_mask, visual.green_mask, visual.red_mask)
        if any(a & b for i, a in enumerate(masks) for b in masks[i + 1:]):
            raise X11CaptureError('Overlapping visual masks')
        tables = self._directcolor_tables(visual, masks) if direct else None
        for mask in masks:
            if not mask or mask >= (1 << fmt.bits_per_pixel):
                raise X11CaptureError('Invalid visual mask')
            shift = (mask & -mask).bit_length() - 1
            maximum = mask >> shift
            if maximum & (maximum + 1):
                raise X11CaptureError('Noncontiguous visual mask')
            channel = ((pixels & mask) >> shift).astype(np.uint64)
            if direct:
                channels.append(tables[len(channels)][channel])
            else:
                channels.append(((channel * 255 + maximum // 2) // maximum).astype(np.uint8))
        return np.ascontiguousarray(np.stack(channels, axis=-1))

    @staticmethod
    def _resource_id(value):
        """Xlib attributes may contain a resource object or a numeric XID."""
        xid = value if isinstance(value, Integral) else getattr(value, 'id', None)
        if isinstance(xid, bool) or not isinstance(xid, Integral) or not 0 < xid <= 0xffffffff:
            raise X11CaptureError('Missing/invalid DirectColor resource ID')
        return int(xid)

    def _directcolor_tables(self, visual, masks):
        """Query programmable RGB components, never assume a linear palette.

        QueryColors returns a list of RGB records (not a reply wrapper) in
        python-xlib. Each request varies only the relevant channel's index.
        Bound each table to 4095 entries and reject malformed/deleted resources.
        No palette or colormap is cached between frames.
        """
        try:
            attrs = self.window.get_attributes()
            visual_id = self._resource_id(attrs.visual)
            cmap_id = self._resource_id(attrs.colormap)
            if visual_id != visual.visual_id:
                raise X11CaptureError('DirectColor window visual changed')
            entries = visual.colormap_entries
            if isinstance(entries, bool) or not isinstance(entries, Integral) or not 1 <= entries <= 4095:
                raise X11CaptureError('Invalid/oversized DirectColor palette')
            cmap = (attrs.colormap if callable(getattr(attrs.colormap, 'query_colors', None))
                    else self.display.create_resource_object('colormap', cmap_id))
            if self._resource_id(cmap) != cmap_id:
                raise X11CaptureError('DirectColor colormap resource mismatch')
            tables = []
            for mask, component in zip(masks, ('blue', 'green', 'red')):
                if isinstance(mask, bool) or not isinstance(mask, Integral) or not 0 < mask <= 0xffffffff:
                    raise X11CaptureError('Invalid DirectColor mask')
                shift = (int(mask) & -int(mask)).bit_length() - 1
                maximum = int(mask) >> shift
                if maximum & (maximum + 1) or maximum + 1 > entries:
                    raise X11CaptureError('Invalid/oversized DirectColor channel table')
                colors = cmap.query_colors([index << shift for index in range(maximum + 1)])
                if not isinstance(colors, (list, tuple)) or len(colors) != maximum + 1:
                    raise X11CaptureError('Malformed DirectColor QueryColors reply')
                values = [getattr(color, component, None) for color in colors]
                if any(isinstance(value, bool) or not isinstance(value, Integral)
                       or not 0 <= value <= 65535 for value in values):
                    raise X11CaptureError('Invalid DirectColor 16-bit component')
                table = np.asarray(values, dtype=np.uint64)
                tables.append(((table * 255 + 32767) // 65535).astype(np.uint8))
            current = self.window.get_attributes()
            if (self._resource_id(current.visual) != visual_id
                    or self._resource_id(current.colormap) != cmap_id):
                raise X11CaptureError('DirectColor window visual/colormap changed during query')
            self.display.sync()
            self.check_transport()
            return tables
        except X11CaptureError:
            raise
        except Exception as exc:
            raise X11CaptureError(f'DirectColor colormap query failed: {exc}') from exc

    def capture_view(self, max_y=None):
        """Fresh desktop-only BGR pixels for per-event supported-view guards.

        Safe while MouseController holds ``lock``: this method takes only that
        reentrant lock, never ``_capture_lock`` and never calls Android/ADB.
        Success preserves last_capture_ok; it cannot establish Android input
        eligibility or native_size. Desktop/capture failures latch until a new
        instance is created. Inspect the whole image before optional cropping.
        """
        with self.lock:
            if max_y is not None and (isinstance(max_y, bool) or not isinstance(max_y, int) or max_y <= 0):
                self.last_capture_ok = False
                raise ValueError('max_y must be a positive integer or None')
            try:
                if not self.is_window_active():
                    raise X11CaptureError('Desktop foreground, process or geometry unsafe')
                rect = self.get_window_rect()
                geometry = self.window.get_geometry()
                if (geometry.width, geometry.height) != self.calibrated_size:
                    self._size_changed = True
                    raise X11CaptureError('Client geometry changed before get_image')
                reply = self.window.get_image(0, 0, geometry.width, geometry.height,
                                             self.X.ZPixmap, 0xffffffff)
                if reply is None:
                    raise X11CaptureError('X11 get_image failed')
                image = self._decode(reply, geometry)
                if np.all(image == image[0, 0]):
                    raise X11CaptureError('Uniform client capture')
                if not self.is_window_active() or rect != self.get_window_rect():
                    raise X11CaptureError('Desktop state changed during capture')
                return np.ascontiguousarray(image[:max_y] if max_y is not None else image)
            except Exception as exc:
                self.last_capture_ok = False
                self._failed = True
                if isinstance(exc, X11CaptureError):
                    raise
                raise X11CaptureError(f'X11 view capture failed: {exc}') from exc

    def capture(self, max_y=None):
        # Serialize frames, but NEVER hold the Xlib transport lock across ADB.
        # Pause/release must not wait for a slow wireless dumpsys subprocess.
        with self._capture_lock:
            with self.lock:
                self.last_capture_ok = False
            try:
                if max_y is not None and (isinstance(max_y, bool) or not isinstance(max_y, int) or max_y <= 0):
                    raise ValueError('max_y must be a positive integer or None')
                if not self.is_window_active() or not self.android_guard.is_window_active():
                    raise X11CaptureError('Desktop/Android foreground, process or geometry unsafe')
                with self.lock:
                    rect = self.get_window_rect()
                    image = self.capture_view(max_y)
                if not self.android_guard.is_window_active():
                    raise X11CaptureError('Foreground or geometry changed during capture')
                with self.lock:
                    if not self.is_window_active() or rect != self.get_window_rect():
                        raise X11CaptureError('Foreground or geometry changed during capture')
                    self.native_size = self.calibrated_size
                    self.last_capture_ok = True
                    return image
            except ValueError:
                with self.lock:
                    self.last_capture_ok = False
                raise
            except Exception as exc:
                with self.lock:
                    self.last_capture_ok = False
                    self._failed = True
                if isinstance(exc, X11CaptureError):
                    raise
                raise X11CaptureError(f'X11 capture failed: {exc}') from exc

    def input_ready(self):
        try:
            self.capture()
            return self.last_capture_ok
        except Exception:
            self.last_capture_ok = False
            return False

    def close(self):
        """Caller must release MouseController BEFORE closing this transport."""
        with self.lock:
            self._failed = True
            self.last_capture_ok = False
            if self.display is not None:
                display, self.display = self.display, None
                try:
                    display.close()
                except Exception:
                    logger.exception('X11 close failed')

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
