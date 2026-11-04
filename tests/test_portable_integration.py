"""Offline orchestration checks: no desktop, capture, or notifications."""
import importlib
import sys
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch


class PortableIntegrationTests(unittest.TestCase):
    def setUp(self):
        modules = {
            "image_matcher": SimpleNamespace(ImageMatcher=Mock()),
            "telegram_notifier": SimpleNamespace(TelegramNotifier=Mock()),
        }
        self.modules = patch.dict(sys.modules, modules)
        self.modules.start()
        self.bot_module = importlib.import_module("bot")
        self.bot = self.bot_module.EatventureBot.__new__(self.bot_module.EatventureBot)
        self.bot.running = True
        self.bot.window_capture = Mock()
        self.bot.window_capture.backend.last_capture_rect = (0, 0, 400, 800)
        self.bot.mouse_controller = Mock()

    def tearDown(self):
        self.modules.stop()

    def test_failed_input_aborts_handler_before_capture(self):
        self.bot.mouse_controller.click.return_value = False
        with self.assertRaisesRegex(RuntimeError, "Input failed"):
            self.bot.handle_open_boxes(None)
        self.bot.window_capture.capture.assert_not_called()

    def test_initial_input_requires_capture(self):
        self.bot.window_capture.backend.last_capture_rect = None
        self.bot.window_capture.capture.return_value = None
        with self.assertRaisesRegex(RuntimeError, "Capture failed"):
            self.bot._click(1, 2)
        self.bot.mouse_controller.click.assert_not_called()

    def test_pause_blocks_input(self):
        self.bot.pause()
        with self.assertRaisesRegex(RuntimeError, "paused"):
            self.bot._click(1, 2)
        self.bot.mouse_controller.click.assert_not_called()

    def test_hold_cancellation_releases_without_followup_click(self):
        released = []

        @contextmanager
        def pressed(*args, **kwargs):
            try:
                yield
            finally:
                released.append(True)

        self.bot.upgrade_station_pos = (1, 2)
        self.bot.mouse_controller.pressed_at = pressed
        self.bot.mouse_controller.validate.side_effect = RuntimeError("cancelled")
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.bot.handle_hold_upgrade_station(None)
        self.assertEqual(released, [True])
        self.bot.mouse_controller.click.assert_not_called()

    def test_cleanup_closes_capture_even_if_input_close_fails(self):
        self.bot.overlay = Mock()
        self.bot.mouse_controller.close.side_effect = RuntimeError("release failed")
        with self.assertRaises(RuntimeError):
            self.bot.stop()
        self.bot.overlay.stop.assert_called_once()
        self.bot.window_capture.close.assert_called_once()

    def test_main_import_does_not_require_pynput_or_win32(self):
        with patch.dict(sys.modules, {"pynput": None, "win32api": None, "win32gui": None}):
            importlib.import_module("main")

    def test_platform_overlay_and_shared_backend_wiring(self):
        for platform, overlay_enabled in (("linux", False), ("win32", True)):
            with self.subTest(platform=platform), \
                    patch.object(self.bot_module.sys, "platform", platform), \
                    patch.object(self.bot_module.config, "ShowForbiddenArea", True), \
                    patch.object(self.bot_module, "WindowCapture") as capture, \
                    patch.object(self.bot_module, "MouseController") as mouse, \
                    patch.object(self.bot_module, "ForbiddenAreaOverlay") as overlay, \
                    patch.object(self.bot_module.EatventureBot, "load_templates", return_value={}):
                instance = self.bot_module.EatventureBot()
                kwargs = mouse.call_args.kwargs
                self.assertIs(kwargs["backend"], capture.return_value.backend)
                self.assertTrue(kwargs["cancel_check"]())
                instance.running = True
                self.assertFalse(kwargs["cancel_check"]())
                self.assertEqual(overlay.called, overlay_enabled)
                if overlay_enabled:
                    overlay.return_value.start.assert_called_once()

    def test_constructor_failure_after_capture_closes_capture(self):
        failure = RuntimeError("matcher initialization failed")
        with patch.object(self.bot_module, "WindowCapture") as capture, \
                patch.object(self.bot_module, "ImageMatcher", side_effect=failure):
            with self.assertRaises(RuntimeError) as raised:
                self.bot_module.EatventureBot()
            self.assertIs(raised.exception, failure)
            capture.return_value.close.assert_called_once()

    def test_constructor_overlay_failure_releases_all_acquired_resources(self):
        failure = RuntimeError("overlay startup failed")
        with patch.object(self.bot_module.sys, "platform", "win32"), \
                patch.object(self.bot_module.config, "ShowForbiddenArea", True), \
                patch.object(self.bot_module, "WindowCapture") as capture, \
                patch.object(self.bot_module, "MouseController") as mouse, \
                patch.object(self.bot_module, "ForbiddenAreaOverlay") as overlay, \
                patch.object(self.bot_module.EatventureBot, "load_templates", return_value={}):
            overlay.return_value.start.side_effect = failure
            mouse.return_value.close.side_effect = RuntimeError("cleanup failed")
            with self.assertLogs(self.bot_module.logger, level="ERROR"), \
                    self.assertRaises(RuntimeError) as raised:
                self.bot_module.EatventureBot()
            self.assertIs(raised.exception, failure)
            mouse.return_value.close.assert_called_once()
            overlay.return_value.stop.assert_called_once()
            capture.return_value.close.assert_called_once()

    def test_constructor_capture_failure_does_not_mask_original_error(self):
        failure = RuntimeError("capture construction failed")
        with patch.object(self.bot_module, "WindowCapture", side_effect=failure):
            with self.assertRaises(RuntimeError) as raised:
                self.bot_module.EatventureBot()
            self.assertIs(raised.exception, failure)


if __name__ == "__main__":
    unittest.main()
