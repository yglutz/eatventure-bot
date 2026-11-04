"""Offline only: load the controller against private mocked Win32 modules."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load_file(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MouseControllerTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.moves = []
        self.sleeps = []
        self.stopped = False
        self.api = SimpleNamespace(
            SetCursorPos=Mock(side_effect=self.moves.append),
            mouse_event=Mock(side_effect=lambda *args: self.events.append(args)),
        )
        self.gui = SimpleNamespace(
            IsWindow=Mock(return_value=True),
            GetForegroundWindow=Mock(return_value=42),
            ClientToScreen=Mock(return_value=(100, -200)),
            GetClientRect=Mock(return_value=(0, 0, 360, 780)),
        )
        self.config = load_file("mouse_test_config", "config.py")
        runtime_metrics = load_file("mouse_test_metrics", "runtime_metrics.py")
        self.results = load_file("action_result", "action_result.py").ActionResult
        with patch.dict(sys.modules, {
            "win32api": self.api, "win32gui": self.gui,
            "win32con": SimpleNamespace(MOUSEEVENTF_LEFTDOWN=2, MOUSEEVENTF_LEFTUP=4),
            "config": self.config,
            "action_result": SimpleNamespace(ActionResult=self.results),
            "runtime_metrics": runtime_metrics,
        }):
            self.module = load_file("mouse_under_test", "mouse_controller.py")
        self.now = 0.0
        self.module.time = SimpleNamespace(monotonic=lambda: self.now)
        def sleep(seconds):
            self.sleeps.append(seconds)
            self.now += seconds
        self.module.metrics = runtime_metrics.RuntimeMetrics(
            clock=lambda: self.now, cpu_clock=lambda: 0.0, sleeper=sleep)
        self.mouse = self.module.MouseController(42, cancelled=lambda: self.stopped)

    def test_only_sent_is_truthy(self):
        self.assertTrue(self.results.SENT)
        self.assertFalse(self.results.DENIED)
        self.assertFalse(self.results.CANCELLED)
        self.assertFalse(self.results.INTERRUPTED)

    def test_click_preserves_coordinates_and_total_delays(self):
        self.assertIs(self.mouse.click(100, 400), self.results.SENT)
        self.assertEqual(self.moves, [(200, 200)])
        self.assertEqual([e[0] for e in self.events], [2, 4])
        self.assertAlmostEqual(sum(self.sleeps), 0.14)

    def test_absolute_input_is_checked_in_client_space(self):
        self.assertIs(self.mouse.click(400, -100, relative=False), self.results.DENIED)
        self.assertEqual(self.events, [])
        self.assertEqual(self.moves, [])
        self.assertIs(self.mouse.click(200, 200, relative=False), self.results.SENT)

    def test_zone_edges_and_offset(self):
        for point in [(290, 93), (350, 270), (60, 668), (280, 779), (55, 660)]:
            with self.subTest(point=point):
                self.assertIs(self.mouse.click(*point), self.results.DENIED)
        self.assertEqual(self.events, [])
        self.assertFalse(self.mouse.is_in_forbidden_zone(289, 93))
        self.assertIs(self.mouse.click(280 + 10, 90 + 10), self.results.DENIED)

    def test_configured_stats_intersection_is_blocked(self):
        # Current checked-in zone 1 does NOT include (270, 304). Model the
        # reported intersecting configuration explicitly; do not invent a bypass.
        self.config.FORBIDDEN_ZONE_1_X_MIN = 260
        self.config.FORBIDDEN_ZONE_1_Y_MAX = 320
        self.assertIs(self.mouse.click(*self.config.STATS_UPGRADE_POS), self.results.DENIED)
        self.assertIs(self.mouse.hold_at(*self.config.STATS_UPGRADE_POS), self.results.DENIED)
        self.assertEqual(self.events, [])
        self.assertEqual(self.mouse.last_reason, "forbidden zone")

    def test_current_stats_coordinates_are_not_in_current_zones(self):
        self.assertFalse(self.mouse.is_in_forbidden_zone(*self.config.STATS_UPGRADE_POS))

    def test_bounds_and_nonfinite(self):
        for point in [(-1, 400), (360, 400), (100, 780), (float("nan"), 400)]:
            self.assertIs(self.mouse.click(*point), self.results.DENIED)
        self.assertEqual(self.moves, [])

    def test_missing_or_background_window(self):
        self.gui.IsWindow.return_value = False
        self.assertIs(self.mouse.click(100, 400), self.results.DENIED)
        self.gui.IsWindow.return_value = True
        self.gui.GetForegroundWindow.return_value = 99
        self.assertIs(self.mouse.hold_at(100, 400), self.results.DENIED)
        self.assertEqual(self.moves, [])

    def test_cancellation_before_any_input(self):
        self.stopped = True
        for operation in [lambda: self.mouse.click(100, 400),
                          lambda: self.mouse.hold_at(100, 400),
                          lambda: self.mouse.drag(100, 400, 100, 500)]:
            self.assertIs(operation(), self.results.CANCELLED)
        with self.mouse.pressed_at(100, 400) as result:
            self.assertIs(result, self.results.CANCELLED)
        self.assertEqual(self.moves, [])
        self.assertEqual(self.events, [])

    def test_cancellation_before_down(self):
        self.module.metrics.sleeper = lambda _: setattr(self, "stopped", True)
        self.assertIs(self.mouse.click(100, 400), self.results.CANCELLED)
        self.assertEqual(self.events, [])

    def test_window_moved_during_settle_refuses_down(self):
        self.module.metrics.sleeper = lambda _: setattr(
            self.gui.ClientToScreen, "return_value", (110, -200))
        self.assertIs(self.mouse.click(100, 400), self.results.DENIED)
        self.assertEqual(self.events, [])
        self.assertEqual(self.mouse.last_reason, "window moved")

    def test_cancel_callback_error_fails_closed_and_releases_if_held(self):
        def cancelled():
            raise RuntimeError("callback")
        self.mouse.cancelled = cancelled
        with self.assertRaisesRegex(RuntimeError, "callback"):
            self.mouse.click(100, 400)
        self.assertEqual(self.moves, [])
        self.mouse.cancelled = lambda: False
        def sleep(seconds):
            self.now += seconds
            if self.events:
                self.mouse.cancelled = cancelled
        self.module.metrics.sleeper = sleep
        with self.assertRaisesRegex(RuntimeError, "callback"):
            self.mouse.hold_at(100, 400)
        self.assertEqual([e[0] for e in self.events], [2, 4])

    def test_keyboard_interrupt_releases(self):
        with self.assertRaises(KeyboardInterrupt):
            with self.mouse.pressed_at(100, 400):
                raise KeyboardInterrupt()
        self.assertEqual([e[0] for e in self.events], [2, 4])

    def test_hold_and_drag_cancel_with_release(self):
        def sleep(seconds):
            self.now += seconds
            self.assertLessEqual(seconds, 0.02)
            if self.events:
                self.stopped = True
        self.module.metrics.sleeper = sleep
        for operation in [lambda: self.mouse.hold_at(100, 400, duration=5),
                          lambda: self.mouse.drag(100, 400, 100, 500, duration=5)]:
            self.events.clear()
            self.stopped = False
            self.assertIs(operation(), self.results.INTERRUPTED)
            self.assertEqual([e[0] for e in self.events], [2, 4])

    def test_focus_loss_and_window_move_while_held(self):
        for change in [lambda: setattr(self.gui.GetForegroundWindow, "return_value", 99),
                       lambda: setattr(self.gui.ClientToScreen, "return_value", (101, -200))]:
            self.gui.GetForegroundWindow.return_value = 42
            self.gui.ClientToScreen.return_value = (100, -200)
            self.events.clear()
            def sleep(seconds):
                self.now += seconds
                if self.events:
                    change()
            self.module.metrics.sleeper = sleep
            self.assertIs(self.mouse.hold_at(100, 400), self.results.INTERRUPTED)
            self.assertEqual([e[0] for e in self.events], [2, 4])

    def test_pressed_context_releases_on_body_exception_and_cancellation(self):
        with self.assertRaisesRegex(RuntimeError, "body"):
            with self.mouse.pressed_at(100, 400) as result:
                self.assertIs(result, self.results.SENT)
                raise RuntimeError("body")
        self.assertEqual([e[0] for e in self.events], [2, 4])
        with self.mouse.pressed_at(100, 400) as result:
            self.stopped = True
        self.assertEqual([e[0] for e in self.events], [2, 4, 2, 4])

    def test_down_exception_still_attempts_release(self):
        def event(*args):
            self.events.append(args)
            if args[0] == 2:
                raise RuntimeError("down")
        self.api.mouse_event.side_effect = event
        with self.assertRaisesRegex(RuntimeError, "down"):
            self.mouse.click(100, 400)
        self.assertEqual([e[0] for e in self.events], [2, 4])

    def test_wait_exception_releases(self):
        def sleep(seconds):
            self.now += seconds
            if self.events:
                raise RuntimeError("sleep")
        self.module.metrics.sleeper = sleep
        with self.assertRaisesRegex(RuntimeError, "sleep"):
            self.mouse.hold_at(100, 400)
        self.assertEqual([e[0] for e in self.events], [2, 4])

    def test_drag_move_exception_releases(self):
        def move(point):
            if self.events:
                raise RuntimeError("move")
        self.api.SetCursorPos.side_effect = move
        with self.assertRaisesRegex(RuntimeError, "move"):
            self.mouse.drag(100, 400, 100, 500)
        self.assertEqual([e[0] for e in self.events], [2, 4])

    def test_drag_path_crossing_and_tangency_are_denied(self):
        # Both endpoints are allowed, but the segment crosses zone 4.
        for points in [(100, 80, 250, 80), (100, 65, 250, 65), (250, 80, 100, 80)]:
            self.assertIs(self.mouse.drag(*points), self.results.DENIED)
        self.assertIs(self.mouse.drag(200, -120, 350, -120, relative=False), self.results.DENIED)
        self.assertEqual(self.moves, [])
        self.assertEqual(self.events, [])

    def test_allowed_drag_preserves_steps_and_duration(self):
        self.assertIs(self.mouse.drag(100, 400, 100, 500), self.results.SENT)
        self.assertEqual(len(self.moves), 22)
        self.assertEqual(self.moves[-1], (200, 300))
        self.assertEqual([e[0] for e in self.events], [2, 4])
        self.assertAlmostEqual(sum(self.sleeps), 0.02 + 0.02 + 21 * 0.3 / 20 + 0.1)

    def test_input_waits_are_excluded_from_enclosing_state_work(self):
        with self.module.metrics.measure("state", state="test"):
            self.now += 0.05  # Simulated state work, not sleep.
            self.assertIs(self.mouse.click(100, 400), self.results.SENT)
        rows = self.module.metrics.snapshot()["series"]
        state = next(row for row in rows if row["name"] == "state")
        click = next(row for row in rows if row["name"] == "input"
                     and row["labels"] == {"operation": "click"})
        self.assertAlmostEqual(state["wall"]["mean"], 0.19)
        self.assertAlmostEqual(state["work"]["mean"], 0.05)
        self.assertAlmostEqual(click["wall"]["mean"], 0.14)
        self.assertAlmostEqual(click["work"]["mean"], 0.0)

    def test_input_outcome_counts_and_exception_metrics(self):
        self.mouse.click(100, 400)
        self.mouse.click(300, 100)
        self.stopped = True
        self.mouse.click(100, 400)
        self.stopped = False
        self.api.SetCursorPos.side_effect = RuntimeError("move")
        with self.assertRaisesRegex(RuntimeError, "move"):
            self.mouse.click(100, 400)
        rows = self.module.metrics.snapshot()["series"]
        outcomes = {row["labels"]["outcome"]: row["count"] for row in rows
                    if row["name"] == "input.outcome"
                    and row["labels"]["operation"] == "click"}
        self.assertEqual(outcomes, dict(sent=1, denied=1, cancelled=1, exception=1))
        click = next(row for row in rows if row["name"] == "input"
                     and row["labels"] == {"operation": "click"})
        self.assertEqual(click["count"], 4)
        self.assertEqual(click["errors"], 1)

    def test_pressed_at_metrics_exclude_caller_body(self):
        with self.mouse.pressed_at(100, 400) as result:
            self.assertIs(result, self.results.SENT)
            self.now += 2.0
        rows = self.module.metrics.snapshot()["series"]
        inputs = [row for row in rows if row["name"] == "input"]
        self.assertEqual({row["labels"]["operation"] for row in inputs},
                         {"pressed_at.prepare", "press.prepare", "press.down", "press.up"})
        self.assertAlmostEqual(sum(row["wall"]["mean"] for row in inputs), 0.02)

    def test_public_held_check_uses_original_origin_and_latches_failure(self):
        self.assertIs(self.mouse.check_held_input(), self.results.DENIED)
        with self.mouse.pressed_at(200, 200, relative=False) as result:
            self.assertIs(result, self.results.SENT)
            self.assertIs(self.mouse.check_held_input(), self.results.SENT)
            self.gui.ClientToScreen.return_value = (101, -200)
            self.assertIs(self.mouse.check_held_input(), self.results.INTERRUPTED)
            self.assertEqual(self.mouse.last_reason, "window moved")
            self.gui.ClientToScreen.return_value = (100, -200)
            self.assertIs(self.mouse.check_held_input(), self.results.INTERRUPTED)
        self.assertIs(self.mouse.check_held_input(), self.results.DENIED)
        with self.mouse.pressed_at(100, 400):
            self.assertIs(self.mouse.check_held_input(), self.results.SENT)
        self.assertEqual([e[0] for e in self.events], [2, 4, 2, 4])

    def test_public_held_check_focus_cancellation_and_resize(self):
        for change, reason in [
            (lambda: setattr(self.gui.GetForegroundWindow, "return_value", 99),
             "window is not foreground"),
            (lambda: setattr(self, "stopped", True), "cancelled"),
            (lambda: setattr(self.gui.GetClientRect, "return_value", (0, 0, 90, 300)),
             "outside client bounds"),
        ]:
            self.gui.GetForegroundWindow.return_value = 42
            self.gui.GetClientRect.return_value = (0, 0, 360, 780)
            self.stopped = False
            self.events.clear()
            with self.mouse.pressed_at(100, 400) as result:
                self.assertIs(result, self.results.SENT)
                change()
                self.assertIs(self.mouse.check_held_input(), self.results.INTERRUPTED)
                self.assertEqual(self.mouse.last_reason, reason)
            self.assertEqual([e[0] for e in self.events], [2, 4])

    def test_wait_deadline_counts_oversleep_instead_of_requested_intervals(self):
        waits = []
        def oversleep(seconds):
            waits.append(seconds)
            self.now += seconds + 0.03
        self.module.metrics.sleeper = oversleep
        self.mouse.click_delay = 0
        self.assertIs(self.mouse.hold_at(100, 400, duration=0.1), self.results.SENT)
        # One settle wait, two hold waits (each actually 50ms), and cooldown.
        self.assertEqual(waits, [0.02, 0.02, 0.02, 0])

    def test_guard_work_counts_toward_hold_deadline(self):
        original = self.mouse._check_point
        def slow_guard(*args):
            result = original(*args)
            if self.mouse._held is not None:
                self.now += 0.06
            return result
        self.mouse._check_point = slow_guard
        self.mouse.click_delay = 0
        self.assertIs(self.mouse.hold_at(100, 400, duration=0.1), self.results.SENT)
        self.assertEqual(self.sleeps, [0.02, 0.02, 0])

    def test_interrupted_is_recorded_as_distinct_outcome(self):
        def sleep(seconds):
            self.now += seconds
            if self.events:
                self.stopped = True
        self.module.metrics.sleeper = sleep
        self.assertIs(self.mouse.click(100, 400), self.results.INTERRUPTED)
        rows = self.module.metrics.snapshot()["series"]
        outcome = next(row for row in rows if row["name"] == "input.outcome"
                       and row["labels"] == {"operation": "click", "outcome": "interrupted"})
        self.assertEqual(outcome["count"], 1)


if __name__ == "__main__":
    unittest.main()
