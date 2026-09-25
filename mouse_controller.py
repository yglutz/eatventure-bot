"""The sole Win32 mouse input gateway. All coordinates are client-relative internally."""
import time
import threading
import math
import win32api
import win32con
import win32gui
from safety import forbidden, path_allowed


class MouseController:
    def __init__(self, hwnd, click_delay=0.1, guard=None):
        self.hwnd = hwnd
        self.click_delay = click_delay
        self.size = win32gui.GetClientRect(hwnd)[2:]
        self.guard = guard or (lambda: True)
        self.observer = None
        self.path_guard = lambda start, end: True
        self.max_hold_duration = None
        self.pressed = False
        self.lock = threading.RLock()

    def is_in_forbidden_zone(self, x, y):
        return forbidden(x, y)

    def get_window_position(self):
        return win32gui.ClientToScreen(self.hwnd, (0, 0))

    def ready(self):
        return (win32gui.IsWindow(self.hwnd) and
                win32gui.GetForegroundWindow() == self.hwnd and
                win32gui.GetClientRect(self.hwnd)[2:] == self.size and self.guard())

    def release(self):
        with self.lock:
            if self.pressed:
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                self.pressed = False

    def _point(self, x, y, relative):
        return (int(x), int(y)) if relative else win32gui.ScreenToClient(self.hwnd, (int(x), int(y)))

    def move_to(self, x, y, relative=True):
        point = self._point(x, y, relative)
        if self.ready() and path_allowed(point, point, self.size) and not self.pressed:
            win32api.SetCursorPos(win32gui.ClientToScreen(self.hwnd, point))

    def _perform(self, kind, start, end, duration):
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

    def click(self, x, y, relative=True):
        p = self._point(x, y, relative)
        return self._perform('click', p, p, .04)

    def double_click(self, x, y, relative=True):
        return self.click(x, y, relative) and self.click(x, y, relative)

    def hold_at(self, x, y, duration=2., relative=True):
        p = self._point(x, y, relative)
        if self.max_hold_duration is not None:
            duration = min(duration, self.max_hold_duration)
        return self._perform('hold', p, p, duration)

    def drag(self, from_x, from_y, to_x, to_y, duration=.3, relative=True):
        return self._perform('drag', self._point(from_x, from_y, relative),
                             self._point(to_x, to_y, relative), duration)
