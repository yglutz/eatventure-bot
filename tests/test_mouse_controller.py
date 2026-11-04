"""Offline input tests: this fake never imports or connects to a desktop."""
import unittest
from unittest.mock import patch

from mouse_controller import MouseController


class Backend:
    def __init__(self):
        self.rect = (100, 200, 400, 800)
        self.last_capture_rect = self.rect
        self.events = []
        self.focus = True
        self.down_error = False
        self.move_error = False
        self.cursor = (0, 0)

    def get_cursor_pos(self):
        return self.cursor

    def get_window_rect(self):
        return self.rect

    def validate(self, expected_rect=None):
        if not self.focus:
            raise RuntimeError("Target lost focus")
        if expected_rect is not None and self.rect != expected_rect:
            raise RuntimeError("Geometry changed")
        return self.rect

    def move_pointer(self, x, y):
        self.events.append(("move", x, y))
        if self.move_error:
            raise RuntimeError("Move failed")
        self.cursor = (x, y)

    def button_down(self):
        self.events.append("down")
        if self.down_error:
            raise RuntimeError("Down failed")

    def button_up(self):
        self.events.append("up")


class MouseTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.mouse = MouseController(backend=self.backend, click_delay=0)

    def test_relative_and_absolute_transforms(self):
        self.assertTrue(self.mouse.hold_at(100, 300, duration=0))
        self.assertEqual(self.backend.events, [("move", 200, 500), "down", "up"])
        self.backend.events.clear()
        self.assertTrue(self.mouse.hold_at(200, 500, duration=0, relative=False))
        self.assertEqual(self.backend.events, [("move", 200, 500), "down", "up"])

    def test_forbidden_and_bounds_in_both_coordinate_systems(self):
        for x, y in [(290, 93), (350, 270), (0, 50), (280, 799),
                     (-0.1, 300), (400, 300), (100, 800), (float("nan"), 300)]:
            for relative in (True, False):
                with self.subTest(x=x, y=y, relative=relative):
                    px, py = (x, y) if relative else (x + 100, y + 200)
                    result = self.mouse.hold_at(px, py, duration=0, relative=relative)
                    self.assertFalse(result)
                    self.assertTrue(result.reason)
        self.assertEqual(self.backend.events, [])

    def test_current_stats_target_is_not_zone_one(self):
        # Configuration currently places zone 1 at x=290..350,y=93..270.
        self.assertFalse(self.mouse.is_in_forbidden_zone(270, 304))

    def test_missing_capture_focus_and_geometry_fail_closed(self):
        self.backend.last_capture_rect = None
        self.assertIn("captured", self.mouse.click(100, 300).reason)
        self.backend.last_capture_rect = (100, 200, 400, 800)
        self.backend.focus = False
        self.assertIn("focus", self.mouse.click(100, 300).reason)
        self.backend.focus = True
        self.backend.rect = (101, 200, 400, 800)
        self.assertIn("Geometry", self.mouse.click(100, 300).reason)
        self.assertEqual(self.backend.events, [])

    def test_drag_rejects_full_path_and_tangent(self):
        # Safe endpoints, but the intervening segment intersects zone 1.
        for y in (93, 150, 270):
            for relative in (True, False):
                offset_x, offset_y = (0, 0) if relative else (100, 200)
                result = self.mouse.drag(280 + offset_x, y + offset_y,
                                         360 + offset_x, y + offset_y,
                                         duration=0, relative=relative)
                self.assertFalse(result)
                self.assertIn("path", result.reason)
        self.assertEqual(self.backend.events, [])

    def test_safe_drag(self):
        self.assertTrue(self.mouse.drag(100, 300, 200, 400, duration=0))
        self.assertEqual(self.backend.events[-1], "up")
        self.assertIn(("move", 300, 600), self.backend.events)

    def test_exception_after_down_releases(self):
        self.backend.down_error = True
        self.assertFalse(self.mouse.click(100, 300))
        self.assertEqual(self.backend.events[-2:], ["down", "up"])

    def test_context_body_exception_and_base_exception_release(self):
        for exception in (ValueError, KeyboardInterrupt):
            with self.assertRaises(exception):
                with self.mouse.pressed_at(100, 300):
                    raise exception()
            self.assertEqual(self.backend.events[-1], "up")

    def test_hold_focus_loss_geometry_and_cancellation_release(self):
        for failure in ("focus", "geometry", "cancel"):
            self.setUp()
            cancelled = [False]
            self.mouse.cancel_check = lambda: cancelled[0]

            def sleep(seconds):
                self.assertLessEqual(seconds, .05)
                if failure == "focus":
                    self.backend.focus = False
                elif failure == "geometry":
                    self.backend.rect = (101, 200, 400, 800)
                else:
                    cancelled[0] = True

            with patch("mouse_controller.time.sleep", side_effect=sleep):
                result = self.mouse.hold_at(100, 300, duration=1)
            self.assertFalse(result)
            self.assertEqual(self.backend.events[-1], "up")

    def test_hold_callback_and_pre_cancel(self):
        self.assertFalse(self.mouse.hold_at(100, 300, cancel_check=lambda: True))
        self.assertEqual(self.backend.events, [])

    def test_drag_move_exception_releases(self):
        original = self.backend.move_pointer

        def move(x, y):
            if "down" in self.backend.events:
                raise RuntimeError("move failed while pressed")
            original(x, y)

        self.backend.move_pointer = move
        self.assertFalse(self.mouse.drag(100, 300, 200, 400, duration=0))
        self.assertEqual(self.backend.events[-1], "up")

    def test_wait_exception_releases_and_reports_failure(self):
        with patch("mouse_controller.time.sleep", side_effect=OSError("timer failure")):
            result = self.mouse.hold_at(100, 300, duration=1)
        self.assertFalse(result)
        self.assertIn("timer failure", result.reason)
        self.assertEqual(self.backend.events[-1], "up")

    def test_focus_loss_between_move_and_press_never_presses(self):
        def move(x, y):
            self.backend.events.append(("move", x, y))
            self.backend.focus = False

        self.backend.move_pointer = move
        self.assertFalse(self.mouse.click(100, 300))
        self.assertNotIn("down", self.backend.events)

    def test_geometry_change_during_drag_releases(self):
        original = self.backend.move_pointer

        def move(x, y):
            original(x, y)
            if "down" in self.backend.events:
                self.backend.rect = (101, 200, 400, 800)

        self.backend.move_pointer = move
        self.assertFalse(self.mouse.drag(100, 300, 200, 400, duration=0))
        self.assertEqual(self.backend.events[-1], "up")

    def test_move_to_rejected_while_pressed_even_with_safe_endpoints(self):
        # Segment between these safe endpoints crosses zone 1.
        for relative in (True, False):
            with self.subTest(relative=relative):
                self.setUp()
                with self.mouse.pressed_at(280, 150):
                    before = list(self.backend.events)
                    target = (360, 150) if relative else (460, 350)
                    result = self.mouse.move_to(*target, relative=relative)
                    self.assertFalse(result)
                    self.assertIn("active press", result.reason)
                    self.assertEqual(self.backend.events, before)
                self.assertEqual(self.backend.events[-1], "up")
        self.assertTrue(self.mouse.move_to(100, 300))

    def test_external_pointer_displacement_rejected_and_released(self):
        for position in ((201, 500), (400, 350), (0, 0)):
            with self.subTest(position=position):
                self.setUp()
                with self.assertRaisesRegex(RuntimeError, "Pointer displaced"):
                    with self.mouse.pressed_at(100, 300) as pressed:
                        self.backend.cursor = position
                        pressed.validate()
                self.assertEqual(self.backend.events[-1], "up")

    def test_hold_detects_displacement_on_poll(self):
        def sleep(seconds):
            self.backend.cursor = (201, 500)

        with patch("mouse_controller.time.sleep", side_effect=sleep):
            result = self.mouse.hold_at(100, 300, duration=1)
        self.assertFalse(result)
        self.assertIn("Pointer displaced", result.reason)
        self.assertEqual(self.backend.events[-1], "up")

    def test_pointer_query_exception_releases(self):
        with self.assertRaisesRegex(OSError, "query failed"):
            with self.mouse.pressed_at(100, 300) as pressed:
                with patch.object(self.backend, "get_cursor_pos",
                                  side_effect=OSError("query failed")):
                    pressed.validate()
        self.assertEqual(self.backend.events[-1], "up")

    def test_custom_loop_validates_focus(self):
        with self.assertRaisesRegex(RuntimeError, "focus"):
            with self.mouse.pressed_at(100, 300) as pressed:
                self.backend.focus = False
                pressed.validate()
        self.assertEqual(self.backend.events[-1], "up")


if __name__ == "__main__":
    unittest.main()
