"""Experimental per-event Android input; no external touch tracking is available.

Separate adb processes are not a validated cross-process gesture transport. Each
command has a two-second timeout, not a 50ms cancellation guarantee. While
pressed, capture and validation share the absolute gesture deadline (at most ten
seconds after DOWN). Callers must keep checking and release in a finally block;
there is no independent release watchdog when their code stops running. Cleanup
has a separate two-second timeout. Process scheduling/creation can add latency.
Unknown device
state formats are refused. All safety policy for game controls remains upstream.
"""
import re
import time
import math


def _state_field(text, name):
    """Read only complete, uniquely named state lines, including unknown values."""
    return [value.strip() for value in re.findall(
        rf"^[ \t]*{re.escape(name)}=[ \t]*(.*)$", text, re.MULTILINE)]


def validate_interactive_state(window, power):
    """Accept two explicit dumpsys schemas; refuse conflicts in either schema."""
    fields = {
        "display": (_state_field(window, "mTopFocusedDisplayId"), "0"),
        "wake": (_state_field(power, "mWakefulness"), "Awake"),
        "locked": (_state_field(window, "mShowingLockscreen"), "false"),
        "interactive": (_state_field(power, "mInteractive"), "true"),
        "keyguard": (_state_field(window, "isKeyguardShowing"), "false"),
        "screen": (_state_field(window, "screenState"), "SCREEN_STATE_ON"),
        "awake": (_state_field(window, "interactiveState"), "INTERACTIVE_STATE_AWAKE"),
    }
    for values, expected in fields.values():
        if values and values != [expected]:
            raise RuntimeError("Conflicting or ambiguous Android interactive state")
    if not fields["display"][0] or not fields["wake"][0]:
        raise RuntimeError("Unverified default display or wakefulness")
    old_schema = all(fields[name][0] for name in ("locked", "interactive"))
    observed_schema = all(fields[name][0] for name in ("keyguard", "screen", "awake"))
    if not (old_schema or observed_schema):
        raise RuntimeError("Unverified Android unlock or interactive schema")
    # Some window policy dumps also expose this combined summary. It cannot
    # establish safety alone, but contradictory or duplicate summaries veto it.
    summaries = _state_field(window, "mAwake")
    if summaries and summaries != ["true mScreenOnEarly=true mScreenOnFully=true"]:
        raise RuntimeError("Conflicting Android screen summary")


class ExperimentalADBInput:
    tracks_external_pointer = False

    def set_gesture_deadline(self, deadline):
        """Stage an absolute monotonic limit; a running gesture cannot extend it."""
        if self._pressed:
            raise RuntimeError("Cannot change deadline during an ADB gesture")
        if deadline is not None and (isinstance(deadline, bool)
                                     or not isinstance(deadline, (int, float))
                                     or not math.isfinite(deadline)):
            raise ValueError("Gesture deadline must be finite monotonic time or None")
        self._requested_gesture_deadline = deadline

    def _check_running_gesture(self):
        self._cancelled()
        if self._pressed:
            if self._input_fault:
                raise RuntimeError("ADB input faulted during gesture")
            if time.monotonic() >= self._gesture_deadline:
                raise RuntimeError("ADB gesture deadline expired")

    def _input_enabled(self):
        if not self.enable_input:
            raise NotImplementedError("ADB input requires explicit experimental opt-in")
        if self._closed or self._input_fault:
            raise RuntimeError("ADB input closed or faulted; recreate backend after review")

    def _cancelled(self):
        if self.cancel_check is not None and self.cancel_check():
            raise RuntimeError("ADB input cancelled")

    def _signature(self):
        return (self.serial, self.package, self.native_size, self.crop, self.logical_size)

    def _validate_input(self):
        self._cancelled()
        if (self.last_capture_rect is None or self._capture_signature != self._signature()
                or time.monotonic() - self._capture_time > 5
                or (self._pressed and self._gesture_signature != self._signature())):
            raise RuntimeError("ADB input requires fresh unchanged capture calibration")
        self.validate(self.last_capture_rect)
        window = self._run("shell", "dumpsys", "window").decode("utf-8", "strict")
        power = self._run("shell", "dumpsys", "power").decode("utf-8", "strict")
        validate_interactive_state(window, power)
        self._cancelled()

    def get_cursor_pos(self):
        """Return staged logical coordinates, NOT the physical/external pointer."""
        self._input_enabled()
        return self._pointer

    def _event(self, action):
        x, y = self._native_pointer
        self._run("shell", "input", "touchscreen", "motionevent", action, str(x), str(y))

    def _guarded(self, operation):
        self._input_enabled()
        self._input_deadline = time.monotonic() + 2
        try:
            operation()
        except BaseException:
            self._input_fault = True
            self.button_up()
            raise
        finally:
            self._input_deadline = None

    def move_pointer(self, screen_x, screen_y):
        def move():
            self._cancelled()
            native = self._map_point(screen_x, screen_y)
            if self._pressed:
                self._validate_input()
            self._pointer = (screen_x, screen_y)
            self._native_pointer = native
            if self._pressed:
                self._event("MOVE")
                self._cancelled()
        self._guarded(move)

    def button_down(self):
        def down():
            if self._pressed:
                raise RuntimeError("ADB button already potentially pressed")
            if self._native_pointer is None:
                raise RuntimeError("Stage a logical pointer before DOWN")
            self._validate_input()
            self._native_pointer = self._map_point(*self._pointer)
            self._gesture_signature = self._signature()
            self._gesture_deadline = time.monotonic() + 10
            if self._requested_gesture_deadline is not None:
                self._gesture_deadline = min(
                    self._gesture_deadline, self._requested_gesture_deadline)
            if time.monotonic() >= self._gesture_deadline:
                raise RuntimeError("ADB gesture deadline expired before DOWN")
            self._pressed = True  # Timeout/interrupt can occur after delivery.
            self._event("DOWN")
            self._cancelled()
        self._guarded(down)

    def button_up(self):
        if not self.enable_input:
            raise NotImplementedError("ADB input requires explicit experimental opt-in")
        if not self._pressed:
            return
        # Exactly one cleanup attempt, even when focus/cancellation/fault changed.
        self._pressed = False
        deadline = self._input_deadline
        self._input_deadline = time.monotonic() + 2
        self._input_releasing = True
        try:
            self._event("UP")
        except BaseException:
            self._input_fault = True
            raise
        finally:
            self._input_releasing = False
            self._input_deadline = deadline
