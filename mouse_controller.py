"""Fail-closed, backend-neutral desktop input. Importing sends no input."""
from contextlib import contextmanager
from dataclasses import dataclass
import math
import time
import config
from runtime_metrics import metrics


@dataclass(frozen=True)
class InputResult:
    success: bool
    reason: str = ""

    def __bool__(self):
        return self.success


class MouseController:
    def __init__(self, hwnd=None, click_delay=0.1, *, backend=None, cancel_check=None):
        self._legacy = backend is None
        if backend is None:
            import sys
            if sys.platform != "win32" or hwnd is None:
                raise RuntimeError("A desktop backend is required")
            from windows_backend import WindowsBackend
            # Attach without finding or resizing a different window.
            backend = WindowsBackend.__new__(WindowsBackend)
            backend.hwnd = hwnd
            backend.last_capture_rect = None
        self.backend = backend
        self.hwnd = hwnd if hwnd is not None else getattr(backend, "hwnd", None)
        self.click_delay = click_delay
        self.cancel_check = cancel_check
        self._pressed_rect = None
        self._pressed_point = None

    def _zones(self):
        yield (config.FORBIDDEN_CLICK_X_MIN, config.FORBIDDEN_CLICK_X_MAX,
               config.FORBIDDEN_CLICK_Y_MIN, math.inf)
        for i in range(1, 6):
            prefix = f"FORBIDDEN_ZONE_{i}_"
            yield tuple(getattr(config, prefix + key)
                        for key in ("X_MIN", "X_MAX", "Y_MIN", "Y_MAX"))

    def is_in_forbidden_zone(self, x, y):
        return any(left <= x <= right and top <= y <= bottom
                   for left, right, top, bottom in self._zones())

    def validate(self, expected_rect=None, cancel_check=None):
        """Return verified rect or raise. Cancellation callbacks return True to stop."""
        if ((self.cancel_check and self.cancel_check()) or
                (cancel_check and cancel_check())):
            raise RuntimeError("Input cancelled")
        captured = self.backend.last_capture_rect
        if captured is None and not self._legacy:
            raise RuntimeError("Input requires a prior captured frame")
        expected = expected_rect or self._pressed_rect or captured
        rect = tuple(self.backend.validate(expected_rect=expected))
        if len(rect) != 4 or not all(math.isfinite(v) for v in rect) or min(rect[2:]) <= 0:
            raise RuntimeError("Invalid client geometry")
        if expected is not None and rect != tuple(expected):
            raise RuntimeError("Window geometry changed")
        if captured is not None and rect != tuple(captured):
            raise RuntimeError("Captured geometry changed")
        if self._pressed_rect is not None:
            # Any uncommanded motion could already have crossed an exclusion;
            # do not accept even a new position that is itself otherwise safe.
            if tuple(self.backend.get_cursor_pos()) != self._pressed_point:
                raise RuntimeError("Pointer displaced during press")
        return rect

    def get_window_position(self):
        return self.validate()[:2]

    def _point(self, x, y, relative, rect):
        if not all(math.isfinite(v) for v in (x, y)):
            raise RuntimeError("Non-finite coordinates")
        sx, sy = (rect[0] + x, rect[1] + y) if relative else (x, y)
        sx, sy = int(sx), int(sy)
        # Validate requested and rounded positions to avoid fractional escapes.
        local = (x, y) if relative else (x - rect[0], y - rect[1])
        for px, py in (local, (sx - rect[0], sy - rect[1])):
            if not (0 <= px < rect[2] and 0 <= py < rect[3]):
                raise RuntimeError("Input outside client bounds")
            if self.is_in_forbidden_zone(px, py):
                raise RuntimeError("Input intersects forbidden zone")
        return sx, sy

    def _path(self, start, end, rect):
        # Slab intersection checks the entire segment, including tangencies.
        ax, ay = start[0] - rect[0], start[1] - rect[1]
        bx, by = end[0] - rect[0], end[1] - rect[1]
        for left, right, top, bottom in self._zones():
            low, high = 0., 1.
            for pos, delta, lo, hi in ((ax, bx-ax, left, right),
                                       (ay, by-ay, top, bottom)):
                if delta == 0:
                    if not lo <= pos <= hi:
                        low, high = 1., 0.
                        break
                else:
                    a, b = (lo-pos)/delta, (hi-pos)/delta
                    low, high = max(low, min(a, b)), min(high, max(a, b))
            if low <= high:
                raise RuntimeError("Drag path intersects forbidden zone")

    @contextmanager
    def pressed_at(self, x, y, relative=True, *, cancel_check=None, deadline=None):
        """Yield controller while pressed; unsafe entry raises, release always runs.

        Custom loops must call validate() at least every 50 ms and propagate
        failures. Use the constructor callback for cancellation in custom loops.
        Optional deadline is absolute monotonic time, forwarded to capable
        backends. Blocking ADB operations can delay cancellation by two seconds.
        """
        if self._pressed_rect is not None:
            raise RuntimeError("Nested presses are unsupported")
        rect = self.validate(cancel_check=cancel_check)
        point = self._point(x, y, relative, rect)
        self.backend.move_pointer(*point)
        self.validate(rect, cancel_check)
        self._pressed_rect = rect
        self._pressed_point = point
        deadline_hook = getattr(self.backend, "set_gesture_deadline", None)
        try:
            if deadline_hook is not None:
                deadline_hook(deadline)
            self.validate(rect, cancel_check)
            # A backend may send the down event before raising an exception.
            self.backend.button_down()
            yield self
        finally:
            try:
                self.backend.button_up()
            finally:
                self._pressed_rect = None
                self._pressed_point = None
                if deadline_hook is not None:
                    deadline_hook(None)

    def _wait(self, duration, rect, cancel_check=None):
        deadline = time.monotonic() + duration
        while True:
            self.validate(rect, cancel_check)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            metrics.wait(min(0.05, remaining))

    def move_to(self, x, y, relative=True):
        try:
            if self._pressed_rect is not None:
                raise RuntimeError("move_to is forbidden during an active press; use drag")
            rect = self.validate()
            point = self._point(x, y, relative, rect)
            self.validate(rect)
            self.backend.move_pointer(*point)
            return InputResult(True)
        except Exception as exc:
            return InputResult(False, str(exc))

    def hold_at(self, x, y, duration=2.0, relative=True, *, cancel_check=None):
        try:
            if not math.isfinite(duration) or duration < 0:
                raise RuntimeError("Invalid input duration")
            with self.pressed_at(x, y, relative, cancel_check=cancel_check):
                self._wait(duration, self._pressed_rect, cancel_check)
            metrics.wait(self.click_delay)
            return InputResult(True)
        except Exception as exc:
            return InputResult(False, str(exc))

    def click(self, x, y, relative=True):
        return self.hold_at(x, y, 0.02, relative)

    def double_click(self, x, y, relative=True):
        # Preserve the historical single click plus extra delay.
        result = self.click(x, y, relative)
        if result:
            metrics.wait(0.1)
        return result

    def drag(self, from_x, from_y, to_x, to_y, duration=0.3, relative=True):
        try:
            if not math.isfinite(duration) or duration < 0:
                raise RuntimeError("Invalid input duration")
            rect = self.validate()
            start = self._point(from_x, from_y, relative, rect)
            end = self._point(to_x, to_y, relative, rect)
            self._path(start, end, rect)
            with self.pressed_at(*start, relative=False):
                self.validate(rect)
                steps = max(20, math.ceil(duration / 0.05))
                previous = start
                for i in range(1, steps + 1):
                    self.validate(rect)
                    point = tuple(int(a + (b-a)*i/steps) for a, b in zip(start, end))
                    self._point(*point, False, rect)
                    self._path(previous, point, rect)
                    self.backend.move_pointer(*point)
                    self._pressed_point = point
                    previous = point
                    self._wait(duration / steps, rect)
            metrics.wait(self.click_delay)
            return InputResult(True)
        except Exception as exc:
            return InputResult(False, str(exc))

    def close(self):
        # The capture facade owns and closes the shared backend.
        if self._pressed_rect is not None:
            try:
                self.backend.button_up()
            finally:
                self._pressed_rect = None
                self._pressed_point = None
