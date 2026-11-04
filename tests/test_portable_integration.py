"""Offline orchestration checks: no desktop, capture, or notifications."""
import importlib
import sys
import unittest
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch


class PortableIntegrationTests(unittest.TestCase):
    def test_desktop_restart_waits_for_handler_unwind_on_action_thread(self):
        main = importlib.import_module('main')
        bot = Mock(running=False)
        entered = threading.Event()
        requests_sent = threading.Event()
        unwound = []
        resume_threads = []
        errors = []
        updates = []

        def resume():
            resume_threads.append(threading.get_ident())
            if len(resume_threads) == 2:
                self.assertEqual(unwound, [True])
            bot.running = True

        bot.resume.side_effect = resume
        bot.pause.side_effect = lambda: setattr(bot, 'running', False)

        def key(char):
            main.on_press(SimpleNamespace(char=char))

        def reader():
            try:
                key('z')  # initial start is queued, never resumed here
                self.assertTrue(entered.wait(2))
                key('z')  # immediate pause during blocked handler
                key('z')  # request restart while handler still owns old state
                self.assertFalse(bot.running)
                self.assertEqual(len(resume_threads), 1)
            except BaseException as exc:
                errors.append(exc)
            finally:
                requests_sent.set()

        thread = threading.Thread(target=reader)

        def update():
            updates.append(True)
            if len(updates) == 1:
                entered.set()
                self.assertTrue(requests_sent.wait(2))
                unwound.append(True)
                raise RuntimeError('paused handler unwound')
            key('p')

        bot.state_machine.update.side_effect = update
        listener = Mock()
        listener.start.side_effect = thread.start
        keyboard = SimpleNamespace(Listener=Mock(return_value=listener))
        try:
            with patch.dict('os.environ', {}, clear=True), \
                    patch.dict(sys.modules, {'pynput': SimpleNamespace(keyboard=keyboard)}), \
                    patch.object(main, 'setup_logging'), \
                    patch.object(main, 'EatventureBot', return_value=bot), \
                    patch.object(main, 'should_exit', False), \
                    patch.object(main.time, 'sleep'):
                self.assertEqual(main.main(['--backend', 'desktop']), 0)
        finally:
            requests_sent.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(resume_threads, [threading.get_ident()] * 2)

    def test_adb_startup_capture_only_without_pynput_on_windows(self):
        main = importlib.import_module('main')
        backend = Mock()
        backend.capture.return_value = SimpleNamespace(size=1)
        bot = Mock(running=False)
        controls = Mock()
        controls.commands.get_nowait.side_effect = [('start', 0), ('quit', 1)]
        with patch.dict(sys.modules, {'pynput': None}), \
                patch.object(main.sys, 'platform', 'win32'), \
                patch.object(main, 'setup_logging'), \
                patch('adb_backend.ADBBackend', return_value=backend) as constructor, \
                patch('terminal_controls.TerminalControls', return_value=controls), \
                patch.object(main, 'EatventureBot', return_value=bot), \
                patch.object(main, 'should_exit', False):
            self.assertEqual(main.main(['--backend', 'adb', '--adb-serial', 'offline']), 0)
        self.assertFalse(constructor.call_args.kwargs['enable_input'])
        self.assertEqual(constructor.call_args.kwargs['serial'], 'offline')
        backend.capture.assert_called_once_with()
        backend.button_down.assert_not_called()
        bot.resume.assert_not_called()
        controls.close.assert_called_once()
        bot.stop.assert_called_once()

    def test_auto_wayland_selects_adb(self):
        main = importlib.import_module('main')
        with patch.object(main.sys, 'platform', 'linux'), \
                patch.dict('os.environ', {'XDG_SESSION_TYPE': 'wayland'}, clear=True):
            self.assertEqual(main.select_backend('auto'), 'adb')
            self.assertEqual(main.select_backend('desktop'), 'desktop')

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
        deadlines = []

        @contextmanager
        def pressed(*args, **kwargs):
            deadlines.append(kwargs['deadline'])
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
        self.assertEqual(len(deadlines), 1)
        self.assertIsInstance(deadlines[0], float)
        self.bot.mouse_controller.click.assert_not_called()

    def test_main_none_argv_uses_process_arguments_and_cleanup_survives_terminal_error(self):
        main = importlib.import_module('main')
        backend = Mock()
        backend.capture.return_value = SimpleNamespace(size=1)
        bot = Mock(running=False)
        controls = Mock()
        controls.commands.get_nowait.side_effect = RuntimeError('original failure')
        controls.close.side_effect = RuntimeError('terminal cleanup failure')
        with patch.object(sys, 'argv', ['main.py', '--backend', 'adb']), \
                patch.object(main, 'setup_logging'), \
                patch('adb_backend.ADBBackend', return_value=backend), \
                patch('terminal_controls.TerminalControls', return_value=controls), \
                patch.object(main, 'EatventureBot', return_value=bot), \
                patch.object(main, 'should_exit', False), \
                self.assertLogs(level='ERROR') as logs:
            self.assertEqual(main.main(), 1)
        self.assertIn('Fatal error: original failure', '\n'.join(logs.output))
        bot.stop.assert_called_once_with()

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

    def test_main_bot_startup_failure_never_imports_listener(self):
        main = importlib.import_module("main")
        with patch.dict("os.environ", {}, clear=True), \
                patch.dict(sys.modules, {"pynput": None}), \
                patch.object(main, "setup_logging"), \
                patch.object(main, "EatventureBot", side_effect=RuntimeError("startup failed")), \
                self.assertLogs(level="ERROR") as logs:
            self.assertEqual(main.main([]), 1)
        self.assertIn("startup failed", "\n".join(logs.output))
        self.assertNotIn("import of pynput", "\n".join(logs.output))

    def test_main_partial_listener_failure_preserves_startup_error_and_cleans_bot(self):
        main = importlib.import_module("main")
        keyboard = Mock()
        listener = keyboard.Listener.return_value
        listener.start.side_effect = RuntimeError("listener startup failed")
        listener.stop.side_effect = AttributeError("_display_record")
        bot = Mock()
        bot.stop.side_effect = RuntimeError("bot cleanup failed")
        with patch.dict("os.environ", {}, clear=True), \
                patch.dict(sys.modules, {"pynput": SimpleNamespace(keyboard=keyboard)}), \
                patch.object(main, "setup_logging"), \
                patch.object(main, "EatventureBot", return_value=bot), \
                self.assertLogs(level="ERROR") as logs:
            self.assertEqual(main.main([]), 1)
        self.assertIn("Fatal error: listener startup failed", "\n".join(logs.output))
        keyboard.Listener.assert_called_once_with(on_press=main.on_press)
        listener.stop.assert_called_once()
        bot.stop.assert_called_once()
        self.assertIsNone(main.bot_instance)

    def test_main_wayland_message_before_bot_or_listener_startup(self):
        main = importlib.import_module("main")
        keyboard = Mock()
        with patch.object(main.sys, "platform", "linux"), \
                patch.dict("os.environ", {"XDG_SESSION_TYPE": "wayland"}, clear=True), \
                patch.dict(sys.modules, {"pynput": SimpleNamespace(keyboard=keyboard)}), \
                patch.object(main, "setup_logging"), \
                patch.object(main, "EatventureBot") as constructor, \
                self.assertLogs(level="ERROR") as logs:
            self.assertEqual(main.main(['--backend', 'desktop']), 1)
        constructor.assert_not_called()
        keyboard.Listener.assert_not_called()
        message = "\n".join(logs.output)
        self.assertIn("Wayland/XWayland desktop automation is unsupported", message)
        self.assertIn("capture-only", message)
        self.assertIn("adb_calibrate.py", message)
        self.assertIn("--backend adb", message)

    def test_main_listener_import_failure_cleans_initialized_bot(self):
        main = importlib.import_module("main")
        bot = Mock()
        with patch.dict("os.environ", {}, clear=True), \
                patch.dict(sys.modules, {"pynput": None}), \
                patch.object(main, "setup_logging"), \
                patch.object(main, "EatventureBot", return_value=bot), \
                self.assertLogs(level="ERROR"):
            self.assertEqual(main.main([]), 1)
        bot.stop.assert_called_once()
        self.assertIsNone(main.bot_instance)

    def test_main_windows_keeps_hotkey_listener_after_bot_initialization(self):
        main = importlib.import_module("main")
        keyboard = Mock()
        bot = Mock()

        def initialized_listener(**kwargs):
            self.assertIs(main.bot_instance, bot)
            self.assertIs(kwargs["on_press"], main.on_press)
            return listener

        listener = Mock()
        keyboard.Listener.side_effect = initialized_listener
        with patch.object(main.sys, "platform", "win32"), \
                patch.dict("os.environ", {"WAYLAND_DISPLAY": "ignored-on-windows"}), \
                patch.dict(sys.modules, {"pynput": SimpleNamespace(keyboard=keyboard)}), \
                patch.object(main, "setup_logging"), \
                patch.object(main, "EatventureBot", return_value=bot), \
                patch.object(main, "should_exit", True):
            self.assertEqual(main.main([]), 0)
        listener.start.assert_called_once()
        listener.stop.assert_called_once()
        bot.stop.assert_called_once()

    def test_platform_overlay_and_shared_backend_wiring(self):
        for platform, overlay_enabled in (("linux", False), ("win32", True)):
            with self.subTest(platform=platform), \
                    patch.object(self.bot_module.sys, "platform", platform), \
                    patch.object(self.bot_module.config, "ShowForbiddenArea", True), \
                    patch.object(self.bot_module, "WindowCapture") as capture, \
                    patch.object(self.bot_module, "MouseController") as mouse, \
                    patch.object(self.bot_module, "ForbiddenAreaOverlay") as overlay, \
                    patch.object(self.bot_module.EatventureBot, "load_templates", return_value={}):
                capture.return_value.supports_overlay = overlay_enabled
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
