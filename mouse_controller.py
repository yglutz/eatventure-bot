import logging
import math
import time
from contextlib import contextmanager
from functools import wraps

import win32api
import win32con
import win32gui

import config
from action_result import ActionResult
from runtime_metrics import metrics

logger = logging.getLogger(__name__)


def _input_operation(method):
    """Nested operation timings/counts are per method, not additive totals."""
    @wraps(method)
    def measured(*args, **kwargs):
        outcome = "exception"
        try:
            with metrics.measure("input", operation=method.__name__):
                result = method(*args, **kwargs)
                outcome = result.value
                return result
        finally:
            # Count outcomes separately: measure labels are fixed at entry.
            with metrics.measure("input.outcome", operation=method.__name__, outcome=outcome):
                pass
    return measured


class MouseController:
    """SENT means input delivery, never purchase confirmation.

    Exceptions propagate after release is attempted. ``last_reason`` describes
    the most recent guard failure; it is diagnostic, not thread-safe state.
    """

    CHECK_INTERVAL = 0.02

    def __init__(self, hwnd, click_delay=0.1, cancelled=None):
        self.hwnd = hwnd
        self.click_delay = click_delay
        self.cancelled = cancelled or (lambda: False)
        self.last_reason = None
        self._held = None
        self._held_invalid = False

    def _deny(self, reason, result=ActionResult.DENIED):
        if self._held is not None:
            self._held_invalid = True
            result = ActionResult.INTERRUPTED
        self.last_reason = reason
        logger.warning("Input %s: %s", result.value, reason)
        return result

    def _zones(self):
        yield (config.FORBIDDEN_CLICK_X_MIN, config.FORBIDDEN_CLICK_X_MAX,
               config.FORBIDDEN_CLICK_Y_MIN, math.inf)
        for number in range(1, 6):
            prefix = f"FORBIDDEN_ZONE_{number}_"
            yield tuple(getattr(config, prefix + suffix)
                        for suffix in ("X_MIN", "X_MAX", "Y_MIN", "Y_MAX"))

    def is_in_forbidden_zone(self, x, y):
        return any(left <= x <= right and top <= y <= bottom
                   for left, right, top, bottom in self._zones())

    def get_window_position(self):
        return win32gui.ClientToScreen(self.hwnd, (0, 0))

    def _context(self):
        if self.cancelled():
            return self._deny("cancelled", ActionResult.CANCELLED)
        if not win32gui.IsWindow(self.hwnd):
            return self._deny("window missing")
        if win32gui.GetForegroundWindow() != self.hwnd:
            return self._deny("window is not foreground")
        return ActionResult.SENT

    def _prepare(self, x, y, relative):
        self.last_reason = None
        result = self._context()
        if not result:
            return result, None, None
        if not all(math.isfinite(v) for v in (x, y)):
            return self._deny("non-finite coordinates"), None, None
        origin = self.get_window_position()
        screen = (int(x + origin[0]), int(y + origin[1])) if relative else (int(x), int(y))
        result = self._check_point(screen, origin)
        return result, screen, origin

    def _check_point(self, screen, origin):
        if self._held is not None and self._held_invalid:
            return ActionResult.INTERRUPTED
        result = self._context()
        if not result:
            return result
        if self.get_window_position() != origin:
            return self._deny("window moved")
        x, y = screen[0] - origin[0], screen[1] - origin[1]
        left, top, right, bottom = win32gui.GetClientRect(self.hwnd)
        if not (left <= x < right and top <= y < bottom):
            return self._deny("outside client bounds")
        if self.is_in_forbidden_zone(x, y):
            return self._deny("forbidden zone")
        return ActionResult.SENT

    def _wait(self, duration, screen, origin):
        # Guard time and scheduler oversleep count toward the required duration.
        deadline = time.monotonic() + duration
        while True:
            result = self._check_point(screen, origin)
            if not result:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return result
            interval = min(self.CHECK_INTERVAL, remaining)
            metrics.wait(interval)

    def check_held_input(self):
        """Poll inside pressed_at; no input is emitted by this check.

        Uses the original window origin, not a newly resolved relative target.
        SENT means still valid, INTERRUPTED means stop and exit the context
        immediately (partial effects possible), DENIED means no active press.
        A failed guard stays interrupted until release, even if focus returns.
        Exceptions propagate; the enclosing pressed_at still releases on exit.
        """
        if self._held is None:
            return self._deny("no active press")
        return self._check_point(*self._held)

    @contextmanager
    def pressed_at(self, x, y, relative=True):
        """Yield SENT after down, or DENIED/CANCELLED without down.

        Always attempts release, including when down or the caller body raises.
        Callers must skip their body on a false result and poll check_held_input
        during long bodies; this synchronous context cannot interrupt that body.
        The yielded result describes entry only, not subsequent interruption.
        """
        with metrics.measure("input", operation="pressed_at.prepare"):
            result, screen, origin = self._prepare(x, y, relative)
        if not result:
            yield result
            return
        with self._pressed(screen, origin) as result:
            yield result

    @contextmanager
    def _pressed(self, screen, origin):
        if self._held is not None:
            yield self._deny("nested press is not supported")
            return
        with metrics.measure("input", operation="press.prepare"):
            result = self._check_point(screen, origin)
            if result:
                win32api.SetCursorPos(screen)
                result = self._wait(0.02, screen, origin)
        if not result:
            yield result
            return
        try:
            with metrics.measure("input", operation="press.down"):
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, *screen, 0, 0)
            self._held = (screen, origin)
            self._held_invalid = False
            # Do not attribute the caller's context body to input execution.
            yield ActionResult.SENT
        finally:
            try:
                with metrics.measure("input", operation="press.up"):
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, *screen, 0, 0)
            finally:
                self._held = None
                self._held_invalid = False

    @_input_operation
    def move_to(self, x, y, relative=True):
        result, screen, _ = self._prepare(x, y, relative)
        if result:
            win32api.SetCursorPos(screen)
        return result

    @_input_operation
    def click(self, x, y, relative=True):
        return self.hold_at(x, y, duration=0.02, relative=relative)

    @_input_operation
    def double_click(self, x, y, relative=True):
        # Retain historical single-click behavior and extra delay.
        result = self.click(x, y, relative)
        if result:
            metrics.wait(0.1)
        return result

    @_input_operation
    def hold_at(self, x, y, duration=2.0, relative=True):
        if not math.isfinite(duration) or duration < 0:
            return self._deny("invalid duration")
        result, screen, origin = self._prepare(x, y, relative)
        if not result:
            return result
        with self._pressed(screen, origin) as result:
            if result:
                result = self._wait(duration, screen, origin)
        if result:
            metrics.wait(self.click_delay)
        return result

    def _path_blocked(self, start, end, origin):
        # Closed segment/rectangle intersection, not endpoint sampling.
        start = (start[0] - origin[0], start[1] - origin[1])
        end = (end[0] - origin[0], end[1] - origin[1])
        for left, right, top, bottom in self._zones():
            low, high = 0.0, 1.0
            for a, b, minimum, maximum in zip(start, end, (left, top), (right, bottom)):
                delta = b - a
                if delta == 0:
                    if not minimum <= a <= maximum:
                        low, high = 1.0, 0.0
                        break
                else:
                    t1, t2 = (minimum - a) / delta, (maximum - a) / delta
                    low, high = max(low, min(t1, t2)), min(high, max(t1, t2))
            if low <= high:
                return True
        return False

    @_input_operation
    def drag(self, from_x, from_y, to_x, to_y, duration=0.3, relative=True):
        if not math.isfinite(duration) or duration < 0:
            return self._deny("invalid duration")
        result, start, origin = self._prepare(from_x, from_y, relative)
        if not result:
            return result
        result, end, end_origin = self._prepare(to_x, to_y, relative)
        if not result:
            return result
        if origin != end_origin:
            return self._deny("window moved")
        if self._path_blocked(start, end, origin):
            return self._deny("drag path intersects forbidden zone")
        with self._pressed(start, origin) as result:
            if not result:
                return result
            result = self._wait(0.02, start, origin)
            if not result:
                return result
            steps = 20
            for i in range(steps + 1):
                point = tuple(int(a + (b - a) * i / steps) for a, b in zip(start, end))
                result = self._check_point(point, origin)
                if not result:
                    return result
                win32api.SetCursorPos(point)
                result = self._wait(duration / steps, point, origin)
                if not result:
                    return result
        metrics.wait(self.click_delay)
        return ActionResult.SENT
