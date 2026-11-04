"""Experimental input tests: no adb process or device is ever used."""
import subprocess
import time
import unittest
from unittest.mock import patch

from adb_backend import ADBBackend
from adb_input import validate_interactive_state
from mouse_controller import MouseController


OBSERVED_WINDOW = (
    "  mTopFocusedDisplayId=0\n"
    "  mCurrentFocus=Window{ab u0 com.example.game/.Main}\n"
    "      mCurrentRotation=ROTATION_0\n"
    "    isKeyguardShowing=false\n"
    "      screenState=SCREEN_STATE_ON\n"
    "      interactiveState=INTERACTIVE_STATE_AWAKE\n"
    "    mAwake=true mScreenOnEarly=true mScreenOnFully=true\n")
OBSERVED_POWER = "POWER MANAGER (dumpsys power)\n  mWakefulness=Awake\n"


class ADBInputTests(unittest.TestCase):
    def setUp(self):
        self.backend = ADBBackend(serial="fixed", package="com.example.game",
                                  native_size=(12, 24), crop=(2, 4, 8, 16),
                                  logical_size=(4, 8), enable_input=True)
        self.backend.last_capture_rect = (0, 0, 4, 8)
        self.backend._capture_signature = self.backend._signature()
        self.backend._capture_time = time.monotonic()
        self.cancel = False
        self.backend.cancel_check = lambda: self.cancel
        self.window = (b"mCurrentFocus=Window{ab u0 com.example.game/.Main}\n"
                       b"mTopFocusedDisplayId=0\nmShowingLockscreen=false\n")
        self.events = []
        self.failure = None
        self.mock = patch("adb_backend.subprocess.run", side_effect=self.command)
        self.run = self.mock.start()
        self.addCleanup(self.mock.stop)

    def command(self, argv, **kwargs):
        self.assertEqual(argv[1:3], ["-s", "fixed"])
        self.assertFalse(kwargs["shell"])
        self.assertGreater(kwargs["timeout"], 0)
        self.assertLessEqual(kwargs["timeout"], 2)
        args = argv[3:]
        if args[:4] == ["shell", "input", "touchscreen", "motionevent"]:
            self.events.append(args[4:])
            if self.failure:
                self.failure(args[4])
            output = b""
        else:
            output = {("shell", "dumpsys", "window"): self.window,
                      ("shell", "dumpsys", "input"): b"SurfaceOrientation: 0\n",
                      ("shell", "wm", "size"): b"Physical size: 12x24\n",
                      ("shell", "dumpsys", "power"): b"mWakefulness=Awake\nmInteractive=true\n"}[tuple(args)]
        return subprocess.CompletedProcess(argv, 0, stdout=output)

    def down(self):
        self.backend.move_pointer(0, 0)
        self.backend.button_down()

    def test_staged_moves_and_mapping(self):
        self.backend.move_pointer(0, 0)
        self.assertEqual(self.backend.get_cursor_pos(), (0, 0))
        self.assertFalse(self.backend.tracks_external_pointer)
        self.run.assert_not_called()
        self.backend.button_down()
        self.backend.move_pointer(3, 7)
        self.backend.button_up()
        self.assertEqual(self.events, [["DOWN", "3", "5"], ["MOVE", "9", "19"],
                                       ["UP", "9", "19"]])

    def test_drag_continues_past_capture_age_but_requires_next_capture(self):
        now = time.monotonic()
        with patch("adb_backend.time.monotonic", return_value=now):
            self.down()
        deadline = self.backend._gesture_deadline
        original = self.command
        clock = [now + 4.9]

        def slow_command(argv, **kwargs):
            result = original(argv, **kwargs)
            clock[0] += .05
            return result

        self.run.side_effect = slow_command
        with patch("adb_backend.time.monotonic", side_effect=lambda: clock[0]):
            self.backend.move_pointer(1, 1)
            self.backend.move_pointer(2, 2)
            self.assertGreater(clock[0] - now, 5)
            self.assertEqual(self.backend._gesture_deadline, deadline)
            self.backend.button_up()
            self.assertIsNone(self.backend.last_capture_rect)
            with self.assertRaisesRegex(RuntimeError, "capture calibration"):
                self.backend.button_down()
        self.assertEqual([event[0] for event in self.events],
                         ["DOWN", "MOVE", "MOVE", "UP"])

    def test_admission_expiring_during_validation_refuses_down(self):
        now = time.monotonic()
        self.backend._capture_time = now - 4.9
        clock = [now]
        original = self.command

        def slow_command(argv, **kwargs):
            result = original(argv, **kwargs)
            clock[0] += .05
            return result

        self.run.side_effect = slow_command
        with patch("adb_backend.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(RuntimeError, "capture calibration"):
                self.down()
        self.assertEqual(self.events, [])

    def test_calibration_mutation_during_move_validation_releases(self):
        self.down()
        original = self.command

        def mutate(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[3:] == ["shell", "dumpsys", "power"]:
                self.backend.crop = (0, 0, 8, 16)
            return result

        self.run.side_effect = mutate
        with self.assertRaisesRegex(RuntimeError, "capture calibration"):
            self.backend.move_pointer(1, 1)
        self.assertEqual([event[0] for event in self.events], ["DOWN", "UP"])

    def test_cancel_after_age_threshold_releases(self):
        now = time.monotonic()
        with patch("adb_backend.time.monotonic", return_value=now):
            self.down()
        self.cancel = True
        with patch("adb_backend.time.monotonic", return_value=now + 6):
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                self.backend.move_pointer(1, 1)
        self.assertEqual([event[0] for event in self.events], ["DOWN", "UP"])

    def test_controller_multievent_drag_crosses_five_seconds(self):
        self.backend.timeout = 2
        clock = [time.monotonic()]
        started = clock[0]
        self.backend._capture_time = started
        original = self.command

        def slow_command(argv, **kwargs):
            result = original(argv, **kwargs)
            clock[0] += .025
            return result

        self.run.side_effect = slow_command
        controller = MouseController(backend=self.backend, click_delay=0)
        with patch("adb_backend.time.monotonic", side_effect=lambda: clock[0]), \
                patch("mouse_controller.time.sleep",
                      side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            result = controller.drag(0, 0, 3, 7)
        self.assertTrue(result, result.reason)
        self.assertGreater(clock[0] - started, 5)
        self.assertEqual([event[0] for event in self.events],
                         ["DOWN"] + ["MOVE"] * 20 + ["UP"])
        self.assertIsNone(self.backend.last_capture_rect)

    def test_observed_interactive_schema(self):
        validate_interactive_state(OBSERVED_WINDOW, OBSERVED_POWER)
        self.window = OBSERVED_WINDOW.encode()
        original = self.command
        def observed_command(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[3:] == ["shell", "dumpsys", "power"]:
                result.stdout = OBSERVED_POWER.encode()
            elif argv[3:] == ["shell", "dumpsys", "input"]:
                result.stdout = b"Input Reader State:\n"
            return result
        self.run.side_effect = observed_command
        self.down()
        self.backend.button_up()
        self.assertEqual([event[0] for event in self.events], ["DOWN", "UP"])

    def test_observed_schema_conflicts_duplicates_and_missing_fields(self):
        invalid_windows = [
            OBSERVED_WINDOW.replace("isKeyguardShowing=false", "isKeyguardShowing=true"),
            OBSERVED_WINDOW.replace("SCREEN_STATE_ON", "SCREEN_STATE_OFF"),
            OBSERVED_WINDOW.replace("INTERACTIVE_STATE_AWAKE", "INTERACTIVE_STATE_ASLEEP"),
            OBSERVED_WINDOW.replace("mAwake=true", "mAwake=false"),
            OBSERVED_WINDOW.replace("    isKeyguardShowing=false\n", ""),
            OBSERVED_WINDOW.replace("      screenState=SCREEN_STATE_ON\n", ""),
            OBSERVED_WINDOW.replace("      interactiveState=INTERACTIVE_STATE_AWAKE\n", ""),
            OBSERVED_WINDOW + "isKeyguardShowing=false\n",
            OBSERVED_WINDOW + "mShowingLockscreen=true\n",
            OBSERVED_WINDOW + "screenState=UNKNOWN\n",
            "history: " + OBSERVED_WINDOW.replace("\n", " "),
        ]
        for window in invalid_windows:
            with self.subTest(window=window), self.assertRaises(RuntimeError):
                validate_interactive_state(window, OBSERVED_POWER)
        for power in ["", OBSERVED_POWER * 2, OBSERVED_POWER + "mInteractive=false\n",
                      OBSERVED_POWER.replace("Awake", "Asleep")]:
            with self.subTest(power=power), self.assertRaises(RuntimeError):
                validate_interactive_state(OBSERVED_WINDOW, power)

    def test_cancel_and_focus_loss_release(self):
        for cancel in (True, False):
            with self.subTest(cancel=cancel):
                self.setUp()
                self.down()
                if cancel:
                    self.cancel = True
                else:
                    self.window = b"mCurrentFocus=null\n"
                with self.assertRaises(RuntimeError):
                    self.backend.move_pointer(1, 1)
                self.assertEqual([e[0] for e in self.events], ["DOWN", "UP"])
                with self.assertRaises(RuntimeError):
                    self.backend.button_down()

    def test_down_timeout_attempts_up_once_and_latches(self):
        def fail(action):
            if action == "DOWN":
                raise subprocess.TimeoutExpired("adb", 2)
        self.failure = fail
        with self.assertRaises(RuntimeError):
            self.down()
        self.backend.close()
        self.assertEqual([e[0] for e in self.events], ["DOWN", "UP"])

    def test_failed_up_not_retried_even_on_close(self):
        self.down()
        self.failure = lambda action: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.backend.button_up()
        self.backend.close()
        self.assertEqual([e[0] for e in self.events], ["DOWN", "UP"])
        self.assertTrue(self.backend._input_fault)

    def test_cancel_after_down_and_interrupt_release(self):
        def cancel(action):
            if action == "DOWN":
                self.cancel = True
        self.failure = cancel
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.down()
        self.assertEqual([e[0] for e in self.events], ["DOWN", "UP"])

    def test_stale_missing_changed_capture(self):
        for change in (lambda: setattr(self.backend, "_capture_time", 0),
                       lambda: setattr(self.backend, "last_capture_rect", None),
                       lambda: setattr(self.backend, "crop", (0, 0, 8, 16))):
            with self.subTest(change=change):
                self.setUp()
                change()
                with self.assertRaises(RuntimeError):
                    self.down()
                self.assertEqual(self.events, [])

    def test_unknown_or_duplicate_state_refused(self):
        for state in (b"", self.window * 2,
                      self.window.replace(b"DisplayId=0", b"DisplayId=1"),
                      self.window.replace(b"Lockscreen=false", b"Lockscreen=true")):
            with self.subTest(state=state):
                self.setUp()
                self.window = state
                with self.assertRaises(RuntimeError):
                    self.down()
                self.assertEqual(self.events, [])

    def test_close_releases_and_blocks(self):
        self.down()
        self.backend.close()
        self.backend.close()
        with self.assertRaises(RuntimeError):
            self.backend.move_pointer(1, 1)
        self.assertEqual([e[0] for e in self.events], ["DOWN", "UP"])

    def test_invalid_coordinate_during_hold_releases(self):
        self.down()
        with self.assertRaises(ValueError):
            self.backend.move_pointer(4, 0)
        self.assertEqual([e[0] for e in self.events], ["DOWN", "UP"])

    def test_interrupt_during_down_releases_fixed_transport(self):
        def interrupt(action):
            if action == "DOWN":
                self.backend.serial = "changed"
                raise KeyboardInterrupt()
        self.failure = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.down()
        self.assertEqual([e[0] for e in self.events], ["DOWN", "UP"])

    def test_cancel_callback_exception_still_releases(self):
        self.down()
        def broken_callback():
            raise ValueError("callback failed")
        self.backend.cancel_check = broken_callback
        with self.assertRaises(ValueError):
            self.backend.move_pointer(1, 1)
        self.assertEqual([e[0] for e in self.events], ["DOWN", "UP"])

    def test_ordinary_capture_and_validate_while_pressed_check_cancel(self):
        for method in ("capture", "validate"):
            with self.subTest(method=method):
                self.setUp()
                self.down()
                self.assertIsNone(self.backend._input_deadline)
                self.cancel = True
                count = self.run.call_count
                try:
                    with self.assertRaisesRegex(RuntimeError, "cancelled"):
                        getattr(self.backend, method)()
                finally:
                    self.backend.button_up()
                self.assertEqual(self.run.call_count, count + 1)  # UP only
                self.assertTrue(self.backend._input_fault)
                self.assertEqual(self.events[-1][0], "UP")

    def test_ordinary_capture_timeout_latches_and_releases(self):
        self.down()
        original = self.command
        def timeout(argv, **kwargs):
            if argv[3:] == ["shell", "dumpsys", "window"]:
                self.assertLessEqual(kwargs["timeout"], 2)
                raise subprocess.TimeoutExpired("adb", kwargs["timeout"])
            return original(argv, **kwargs)
        self.run.side_effect = timeout
        try:
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                self.backend.capture()
        finally:
            self.backend.button_up()
        self.assertTrue(self.backend._input_fault)
        self.assertEqual(self.events[-1][0], "UP")

    def test_absolute_gesture_deadline_not_refreshed_by_validation(self):
        now = time.monotonic()
        self.backend.set_gesture_deadline(now + 1)
        with patch("adb_backend.time.monotonic", return_value=now):
            self.down()
        deadline = self.backend._gesture_deadline
        with patch("adb_backend.time.monotonic", return_value=now + .75):
            self.backend.validate()
            self.assertLessEqual(self.run.call_args.kwargs["timeout"], .25)
        self.assertEqual(self.backend._gesture_deadline, deadline)
        with patch("adb_backend.time.monotonic", return_value=now + 1):
            try:
                with self.assertRaisesRegex(RuntimeError, "deadline"):
                    self.backend.capture()
            finally:
                self.backend.button_up()
        self.assertEqual(self.events[-1][0], "UP")

    def test_default_hard_limit_and_post_command_cancel(self):
        now = time.monotonic()
        with patch("adb_backend.time.monotonic", return_value=now):
            self.down()
        self.assertEqual(self.backend._gesture_deadline, now + 10)
        original = self.command
        def cancel_after(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[3:] == ["shell", "dumpsys", "window"]:
                self.cancel = True
            return result
        self.run.side_effect = cancel_after
        try:
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                self.backend.validate()
        finally:
            self.backend.button_up()
        self.assertTrue(self.backend._input_fault)

    def test_post_command_deadline_expiry_latches(self):
        self.down()
        clock = [self.backend._gesture_deadline - .1]
        original = self.command
        def expire(argv, **kwargs):
            result = original(argv, **kwargs)
            clock[0] += .2
            return result
        self.run.side_effect = expire
        with patch("adb_backend.time.monotonic", side_effect=lambda: clock[0]):
            try:
                with self.assertRaisesRegex(RuntimeError, "deadline"):
                    self.backend.validate()
            finally:
                self.backend.button_up()
        self.assertTrue(self.backend._input_fault)

    def test_pressed_capture_screencap_timeout_is_bounded(self):
        self.down()
        original = self.command
        reached_capture = []
        def timeout_screencap(argv, **kwargs):
            if argv[3:] == ["exec-out", "screencap", "-p"]:
                reached_capture.append(True)
                self.assertLessEqual(kwargs["timeout"], 2)
                raise subprocess.TimeoutExpired("adb", kwargs["timeout"])
            return original(argv, **kwargs)
        self.run.side_effect = timeout_screencap
        try:
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                self.backend.capture()
        finally:
            self.backend.button_up()
        self.assertTrue(reached_capture)
        self.assertTrue(self.backend._input_fault)
        self.assertEqual(self.events[-1][0], "UP")

    def test_expired_staged_deadline_refuses_down(self):
        self.backend.set_gesture_deadline(time.monotonic() - 1)
        with self.assertRaisesRegex(RuntimeError, "deadline"):
            self.down()
        self.assertEqual(self.events, [])
        self.backend.set_gesture_deadline(None)
