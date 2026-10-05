"""Sole input gateway: Win32, guarded ADB taps, or scrcpy X11 holds/drags.

For X11, supply a calibrated path_guard and a live session guard. Use close()
or a context manager on pause/exit BEFORE closing the capture transport.
"""
import time
import threading
import math
import logging
from types import SimpleNamespace
try:
    import win32api
    import win32con
    import win32gui
except ImportError:
    win32api = win32gui = None
    # Keep mocked Windows tests usable on hosts without pywin32.
    win32con = SimpleNamespace(MOUSEEVENTF_LEFTDOWN=0x0002, MOUSEEVENTF_LEFTUP=0x0004)
from safety import forbidden, path_allowed

logger = logging.getLogger(__name__)


class MouseController:
    def __init__(self, hwnd=None, click_delay=0.1, guard=None, *, capture=None,
                 desktop_capture=None):
        if desktop_capture is not None and (capture is not None or hwnd is not None):
            raise ValueError('desktop_capture is distinct from ADB capture and HWND')
        self.desktop_capture = desktop_capture
        self.hwnd = hwnd
        self.capture = capture
        self._native_size = (tuple(capture.native_size)
                             if capture is not None and capture.native_size is not None else None)
        self._size_failed = False
        if capture is None and desktop_capture is None and (win32gui is None or win32api is None):
            raise RuntimeError('Win32 input requires pywin32; Android input requires capture')
        self.click_delay = click_delay
        source = desktop_capture if desktop_capture is not None else capture
        self.size = tuple(source.calibrated_size) if source is not None else win32gui.GetClientRect(hwnd)[2:]
        self.guard = guard if guard is not None else (lambda: capture is None and desktop_capture is None)
        self.observer = None
        # X11 must explicitly receive the calibrated gameplay-region guard.
        self.path_guard = lambda start, end: desktop_capture is None
        self.max_hold_duration = None
        self.pressed = False
        self.lock = threading.RLock()
        self._desktop_action_lock = threading.Lock()
        self._release_generation = 0
        self._closed = False

    def is_in_forbidden_zone(self, x, y):
        return forbidden(x, y)

    def get_window_position(self):
        if self.desktop_capture is not None:
            return self.desktop_capture.get_window_rect()[:2]
        if self.capture is not None:
            raise ValueError('ADB coordinates have no desktop window position')
        return win32gui.ClientToScreen(self.hwnd, (0, 0))

    def ready(self):
        if self.desktop_capture is not None:
            try:
                source = self.desktop_capture
                if tuple(source.calibrated_size) != self.size:
                    self._size_failed = True
                if self._closed or self._size_failed or not self.guard() or not source.is_window_active():
                    return False
                if source.get_window_rect()[2:] != self.size:
                    self._size_failed = True
                    return False
                return bool(source.last_capture_ok and self.guard())
            except Exception:
                logger.exception('X11 input readiness failed')
                return False
        if self.capture is not None:
            try:
                if self._size_failed or not self.guard() or not self.capture.input_ready():
                    return False
                native = tuple(self.capture.native_size)
                if (tuple(self.capture.calibrated_size) != self.size or
                        any(isinstance(v, bool) or not isinstance(v, int) or v < 2
                            for v in (*native, *self.size)) or len(native) != 2 or len(self.size) != 2):
                    self._size_failed = True
                    return False
                if self._native_size is None:
                    self._native_size = native
                if native != self._native_size:
                    self._size_failed = True
                    return False
                # A fresh capture may take time: pause must still deny dispatch.
                return bool(self.guard())
            except Exception:
                logger.exception('ADB input readiness failed')
                return False
        return (win32gui.IsWindow(self.hwnd) and
                win32gui.GetForegroundWindow() == self.hwnd and
                win32gui.GetClientRect(self.hwnd)[2:] == self.size and self.guard())

    def release(self):
        with self.lock:
            self._release_generation += 1
            if self.desktop_capture is not None:
                try:
                    if self.pressed:
                        self._x11_event('release')
                except Exception:
                    self._size_failed = True
                    self.desktop_capture.last_capture_ok = False
                    logger.exception('X11 button release failed; input disabled')
                finally:
                    self.pressed = False
                return
            if self.capture is not None:
                # input tap owns its down/up pair; no cancellable held button.
                self.pressed = False
                return
            if self.pressed:
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                self.pressed = False

    def close(self):
        """Disable further X11 input and release; capture remains caller-owned."""
        with self.lock:
            self._closed = True
            self.release()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _point(self, x, y, relative):
        if self.desktop_capture is not None:
            point = (float(x), float(y))
            if not relative:
                origin = self.get_window_position()
                point = tuple(p - o for p, o in zip(point, origin))
            return point
        if self.capture is not None:
            if not relative:
                raise ValueError('ADB input requires canonical relative coordinates')
            return (float(x), float(y))
        return (int(x), int(y)) if relative else win32gui.ScreenToClient(self.hwnd, (int(x), int(y)))

    def move_to(self, x, y, relative=True):
        if self.desktop_capture is not None:
            if not self._desktop_action_lock.acquire(blocking=False):
                return False
            try:
                with self.lock:
                    generation = self._release_generation
                    origin = self.get_window_position()
                    point = self._point(x, y, relative)
                    if self.pressed or self._closed or not self.guard() or not self._x11_path(point, point):
                        return False
                if not self.desktop_capture.input_ready():
                    return False
                with self.lock:
                    if generation != self._release_generation or not self.ready():
                        return False
                    with self.desktop_capture.lock:
                        if (not self.ready() or not self._x11_path(point, point)
                                or origin != self.get_window_position()):
                            return False
                        self._x11_event('motion', tuple(round(v) for v in point))
                        return True
            except Exception:
                self.release()
                logger.exception('X11 move failed')
                return False
            finally:
                self._desktop_action_lock.release()
        if self.capture is not None:
            return False
        point = self._point(x, y, relative)
        if self.ready() and path_allowed(point, point, self.size) and not self.pressed:
            win32api.SetCursorPos(win32gui.ClientToScreen(self.hwnd, point))

    def _perform(self, kind, start, end, duration):
        if self.desktop_capture is not None:
            return self._x11_perform(kind, start, end, duration)
        if self.capture is not None:
            return self._adb_tap(kind, start, end, duration)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError('Input duration must be finite and non-negative')
        if not path_allowed(start, end, self.size) or not self.path_guard(start, end) or not self.ready():
            return False
        token = self.observer('before', kind, start, end, duration, None) if self.observer else None
        origin = self.get_window_position()
        completed = False
        executed = False
        try:
            with self.lock:
                if not self.ready() or not self.path_guard(start, end) or origin != self.get_window_position():
                    return False
                win32api.SetCursorPos(win32gui.ClientToScreen(self.hwnd, start))
                if not self.ready():
                    return False
                self.pressed = True
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                executed = True
            started = time.monotonic()
            while True:
                with self.lock:
                    if not self.pressed or not self.ready() or origin != self.get_window_position():
                        return False
                    elapsed = time.monotonic() - started
                    fraction = min(1., elapsed / max(.001, duration))
                    point = tuple(round(a + (b-a)*fraction) for a,b in zip(start,end))
                    if not path_allowed(start, point, self.size):
                        return False
                    win32api.SetCursorPos(win32gui.ClientToScreen(self.hwnd, point))
                if elapsed >= duration:
                    break
                time.sleep(min(.02, max(0., duration - (time.monotonic()-started))))
            completed = True
        finally:
            self.release()
            if self.observer:
                self.observer('after', kind, start, end, duration, (token, completed, executed))
        time.sleep(self.click_delay)
        return completed

    def _x11_event(self, kind, point=None):
        """The sole XTest dispatch site; serialize with capture's Xlib lock."""
        from Xlib import X
        from Xlib.ext.xtest import fake_input
        source = self.desktop_capture
        with source.lock:
            if kind == 'motion':
                origin = self.get_window_position()
                fake_input(source.display, X.MotionNotify, x=origin[0] + point[0],
                           y=origin[1] + point[1], root=source.root)
            else:
                fake_input(source.display, X.ButtonPress if kind == 'press' else X.ButtonRelease,
                           detail=1)
            # sync, not just flush: detect server rejection before continuing.
            source.display.sync()
            if kind != 'release':
                source.check_transport()
            elif getattr(source, '_transport_error', None) is not None:
                raise RuntimeError('X11 button release reported a transport error')

    def _x11_path(self, start, end):
        if not path_allowed(start, end, self.size) or not self.path_guard(start, end):
            return False
        a, b = (tuple(round(v) for v in p) for p in (start, end))
        return all(path_allowed(p, q, self.size) and self.path_guard(p, q)
                   for p, q in ((start, a), (a, b), (b, end)))

    def _x11_perform(self, kind, start, end, duration, expected_origin=None):
        if not self._desktop_action_lock.acquire(blocking=False):
            return False
        completed = executed = auditing = False
        token = None
        observer = self.observer
        try:
            if not math.isfinite(duration) or duration < 0:
                return False
            with self.lock:
                generation = self._release_generation
                if self.pressed or self._closed or not self._x11_path(start, end) or not self.guard():
                    return False
            # Wireless dumpsys can be slow. Pause/release must never wait for it.
            if not self.desktop_capture.input_ready():
                return False
            with self.lock:
                if generation != self._release_generation or not self.ready():
                    return False
                origin = self.get_window_position()
                if expected_origin is not None and expected_origin != origin:
                    return False
                auditing = True
                if observer:
                    token = observer('before', kind, start, end, duration, None)
                # Pin client origin throughout gesture, including observer callbacks.
                with self.desktop_capture.lock:
                    if (not self.ready() or not self._x11_path(start, end)
                            or origin != self.get_window_position()):
                        return False
                    self._x11_event('motion', tuple(round(v) for v in start))
                    if not self.ready() or origin != self.get_window_position():
                        return False
                    self.pressed = True  # Set before dispatch: even a sync error must release.
                    executed = True
                    self._x11_event('press')
            started = time.monotonic()
            previous = tuple(round(v) for v in start)
            while True:
                # Never hold these locks during sleep: pause can release asynchronously.
                with self.lock, self.desktop_capture.lock:
                    if (not self.pressed or not self.ready()
                            or origin != self.get_window_position()):
                        return False
                    elapsed = time.monotonic() - started
                    fraction = min(1., elapsed / max(.001, duration))
                    point = tuple(round(a + (b-a)*fraction) for a, b in zip(start, end))
                    if not self._x11_path(start, end) or not self._x11_path(previous, point):
                        return False
                    if point != previous:
                        self._x11_event('motion', point)
                    if not self.ready() or origin != self.get_window_position():
                        return False
                    previous = point
                if elapsed >= duration:
                    break
                time.sleep(min(.02, max(0., duration - (time.monotonic() - started))))
            completed = True
        except Exception:
            logger.exception('X11 gesture failed')
            self._size_failed = True
            self.desktop_capture.last_capture_ok = False
            completed = False
        finally:
            self.release()
            if self._size_failed:
                completed = False
            self._desktop_action_lock.release()
            if auditing and observer:
                try:
                    observer('after', kind, start, end, duration, (token, completed, executed))
                except Exception:
                    logger.exception('X11 gesture audit failed')
        if completed and not self._size_failed:
            time.sleep(self.click_delay)
            return True
        return False

    def _desktop_action(self, kind, points, duration, relative):
        try:
            origin = self.get_window_position() if not relative else None
            converted = [tuple(float(v) for v in p) for p in points]
            if origin is not None:
                converted = [tuple(v - o for v, o in zip(p, origin)) for p in converted]
            return self._x11_perform(kind, *converted, duration, expected_origin=origin)
        except Exception:
            self.release()
            logger.exception('X11 coordinate conversion failed')
            return False

    def supports_action(self, kind):
        """Capability mask; Android deliberately has no hold/drag emulation."""
        return kind in ({'click', 'tap', 'double_click'} if self.capture is not None
                        else {'click', 'tap', 'double_click', 'hold', 'drag'})

    def _mapped_tap(self, start, end):
        if (start != end or not path_allowed(start, end, self.size) or
                not self.path_guard(start, end)):
            return None
        native = tuple(max(0, min(n - 1, round(p * (n - 1) / (c - 1))))
                       for p, n, c in zip(start, self._native_size, self.size))
        reverse = tuple(p * (c - 1) / (n - 1)
                        for p, n, c in zip(native, self._native_size, self.size))
        # Rounding must not move the actual tap into an inclusive forbidden zone
        # or outside the calibrated gameplay mask. Validate the full segment too.
        if (not path_allowed(reverse, reverse, self.size) or
                not path_allowed(start, reverse, self.size) or
                not self.path_guard(reverse, reverse) or
                not self.path_guard(start, reverse)):
            return None
        return native

    def _adb_tap(self, kind, start, end, duration):
        if kind != 'click':
            return False
        token = None
        completed = executed = False
        observer = self.observer
        auditing = False
        try:
            with self.lock:
                if not self.ready() or self._mapped_tap(start, end) is None:
                    return False
                auditing = True
                if observer:
                    token = observer('before', kind, start, end, duration, None)
                # Observer callbacks can pause, change focus, or change masks.
                if not self.ready():
                    return False
                native = self._mapped_tap(start, end)
                if native is None or not self.guard():
                    return False
                transport = self.capture.transport
                # executed denotes a dispatch attempt, even when a timeout makes
                # device-side completion unknowable; never retry a failed tap.
                executed = True
                transport._execute([transport.adb_path, '-s', transport.serial,
                                    'shell', 'input', 'tap', *(str(v) for v in native)])
                completed = True
        except Exception:
            logger.exception('ADB tap failed')
            return False
        finally:
            self.release()
            if auditing and observer:
                try:
                    observer('after', kind, start, end, duration, (token, completed, executed))
                except Exception:
                    logger.exception('ADB tap audit failed')
        if completed:
            time.sleep(self.click_delay)
        return completed

    def click(self, x, y, relative=True):
        if self.desktop_capture is not None:
            return self._desktop_action('click', ((x, y), (x, y)), .04, relative)
        if self.capture is not None:
            try:
                p = self._point(x, y, relative)
            except (ValueError, TypeError, OverflowError):
                return False
            return self._perform('click', p, p, .04)
        p = self._point(x, y, relative)
        return self._perform('click', p, p, .04)

    def double_click(self, x, y, relative=True):
        return self.click(x, y, relative) and self.click(x, y, relative)

    def hold_at(self, x, y, duration=2., relative=True):
        if self.desktop_capture is not None:
            try:
                if not math.isfinite(duration) or duration < 0:
                    return False
                if self.max_hold_duration is not None:
                    duration = min(duration, self.max_hold_duration)
                return self._desktop_action('hold', ((x, y), (x, y)), duration, relative)
            except Exception:
                self.release()
                return False
        if self.capture is not None:
            return False
        p = self._point(x, y, relative)
        if self.max_hold_duration is not None:
            duration = min(duration, self.max_hold_duration)
        return self._perform('hold', p, p, duration)

    def drag(self, from_x, from_y, to_x, to_y, duration=.3, relative=True):
        if self.desktop_capture is not None:
            return self._desktop_action('drag', ((from_x, from_y), (to_x, to_y)), duration, relative)
        if self.capture is not None:
            return False
        return self._perform('drag', self._point(from_x, from_y, relative),
                             self._point(to_x, to_y, relative), duration)
