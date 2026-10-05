"""Sole input gateway: Win32 mouse or guarded, canonical-coordinate ADB taps."""
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
    def __init__(self, hwnd=None, click_delay=0.1, guard=None, *, capture=None):
        self.hwnd = hwnd
        self.capture = capture
        self._native_size = (tuple(capture.native_size)
                             if capture is not None and capture.native_size is not None else None)
        self._size_failed = False
        if capture is None and (win32gui is None or win32api is None):
            raise RuntimeError('Win32 input requires pywin32; Android input requires capture')
        self.click_delay = click_delay
        self.size = tuple(capture.calibrated_size) if capture is not None else win32gui.GetClientRect(hwnd)[2:]
        self.guard = guard if guard is not None else (lambda: capture is None)
        self.observer = None
        self.path_guard = lambda start, end: True
        self.max_hold_duration = None
        self.pressed = False
        self.lock = threading.RLock()

    def is_in_forbidden_zone(self, x, y):
        return forbidden(x, y)

    def get_window_position(self):
        if self.capture is not None:
            raise ValueError('ADB coordinates have no desktop window position')
        return win32gui.ClientToScreen(self.hwnd, (0, 0))

    def ready(self):
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
            if self.capture is not None:
                # input tap owns its down/up pair; no cancellable held button.
                self.pressed = False
                return
            if self.pressed:
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                self.pressed = False

    def _point(self, x, y, relative):
        if self.capture is not None:
            if not relative:
                raise ValueError('ADB input requires canonical relative coordinates')
            return (float(x), float(y))
        return (int(x), int(y)) if relative else win32gui.ScreenToClient(self.hwnd, (int(x), int(y)))

    def move_to(self, x, y, relative=True):
        if self.capture is not None:
            return False
        point = self._point(x, y, relative)
        if self.ready() and path_allowed(point, point, self.size) and not self.pressed:
            win32api.SetCursorPos(win32gui.ClientToScreen(self.hwnd, point))

    def _perform(self, kind, start, end, duration):
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
        if self.capture is not None:
            return False
        p = self._point(x, y, relative)
        if self.max_hold_duration is not None:
            duration = min(duration, self.max_hold_duration)
        return self._perform('hold', p, p, duration)

    def drag(self, from_x, from_y, to_x, to_y, duration=.3, relative=True):
        if self.capture is not None:
            return False
        return self._perform('drag', self._point(from_x, from_y, relative),
                             self._point(to_x, to_y, relative), duration)
