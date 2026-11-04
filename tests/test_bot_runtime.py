"""Offline bot-runtime invariants that do not require cv2, numpy, or Win32.

Verifies the runtime improvements in bot.py:
- Stats purchases fail closed without control identity
- _invalidate_targets clears red_icons and red_observed_at
- _wait uses monotonic deadline (no negative sleep race)
- hold_at calls check_held_input during station hold
- _click invalidates targets on non-DENIED results
"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name, filename):
    """Load a module from disk under an alias; caller patches sys.modules."""
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeClock:
    """Fake monotonic clock plus a callable that advances it."""

    def __init__(self):
        self.t = 0.0

    def tick(self, seconds):
        self.t += seconds

    def value(self):
        return self.t


class BotRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

        # Inert dependencies – stubs only, never executed code.
        self.cv2 = types.ModuleType("cv2")
        self.cv2.IMREAD_UNCHANGED = 1
        self.cv2.TM_SQDIFF_NORMED = 0
        self.cv2.matchTemplate = Mock(return_value=Mock(minMaxLoc=Mock(
            return_value=(0, 1, (0, 0), (0, 0)))))
        self.cv2.resize = Mock(side_effect=lambda img, *a, **kw: img)
        self.cv2.bitwise_and = Mock(side_effect=lambda a, b, *args, **kw: a)
        self.cv2.calcHist = Mock(return_value=Mock(normalize=Mock()))
        self.cv2.normalize = Mock()
        self.cv2.compareHist = Mock(return_value=0.9)
        self.cv2.HISTCMP_CORREL = 0
        self.cv2.COLOR_BGRA2BGR = 1
        self.cv2.NORM_MINMAX = 1

        self.np = types.ModuleType("numpy")
        self.np.random = types.ModuleType("numpy.random")
        self.np.random.default_rng = Mock(return_value=Mock(
            integers=Mock(return_value=Mock(shape=(1,)))))
        self.np.full = Mock(return_value=Mock(shape=(1,)))
        self.np.uint8 = int
        self.np.where = Mock(return_value=(Mock(), Mock()))
        self.np.argsort = Mock(return_value=Mock())
        self.np.clip = Mock(side_effect=lambda a, *args: a)
        self.np.max = Mock(side_effect=lambda a, *args: (max(a) if hasattr(a, "__iter__") else a))

        self.pynput = types.ModuleType("pynput")
        self.pynput.keyboard = types.ModuleType("pynput.keyboard")
        self.pynput.keyboard.Listener = Mock
        self.pynput.keyboard.Key = types.SimpleNamespace(esc=None)
        self.pynput.keyboard.KeyCode = types.SimpleNamespace(char=Mock)

        self.win32api = types.ModuleType("win32api")
        self.win32gui = types.ModuleType("win32gui")
        self.win32con = types.ModuleType("win32con")
        self.win32con.MOUSEEVENTF_LEFTDOWN = 2
        self.win32con.MOUSEEVENTF_LEFTUP = 4
        self.telegram = types.ModuleType("telegram_notifier")
        self.telegram.TelegramNotifier = Mock

        # Patch time to use our fake clock everywhere bot.py depends on it.
        self.time_mod = types.ModuleType("time")
        self.time_mod.monotonic = self.clock.value
        self.time_mod.sleep = self.clock.tick

        # Real metrics module (no cv2/numpy/Win32 deps).
        self.real_metrics = _load_module("_real_metrics", "runtime_metrics.py")

        # Stub runtime_metrics module to hold the real metrics instance.
        self.rm_mod = types.ModuleType("runtime_metrics")
        self.rm_mod.metrics = self.real_metrics.metrics
        self.rm_mod.RuntimeMetrics = self.real_metrics.RuntimeMetrics

        # Stub window_capture, image_matcher, mouse_controller – no
        # real code is executed; bot.py only instantiates them and calls
        # their methods, which we mock on the bot instance.
        self.stub_window_capture = types.ModuleType("window_capture")
        self.stub_window_capture.WindowCapture = Mock()
        self.stub_window_capture.ForbiddenAreaOverlay = Mock()

        self.stub_image_matcher = types.ModuleType("image_matcher")
        self.stub_image_matcher.ImageMatcher = Mock()

        self.stub_mouse_controller = types.ModuleType("mouse_controller")
        self.stub_mouse_controller.MouseController = Mock()

        self.modules = {
            "cv2": self.cv2,
            "numpy": self.np,
            "win32api": self.win32api,
            "win32gui": self.win32gui,
            "win32con": self.win32con,
            "pynput": self.pynput,
            "telegram_notifier": self.telegram,
            "time": self.time_mod,
            "runtime_metrics": self.rm_mod,
            "window_capture": self.stub_window_capture,
            "image_matcher": self.stub_image_matcher,
            "mouse_controller": self.stub_mouse_controller,
        }

        # Load bot module with all patches active.
        with patch.dict(sys.modules, self.modules):
            self.bot_module = _load_module("bot_under_test", "bot.py")

    # ------------------------------------------------------------------
    # Test 1: Stats purchases fail closed
    # ------------------------------------------------------------------
    def test_stats_purchase_fails_closed_without_panel_identity(self):
        """handle_upgrade_stats returns OPEN_BOXES even when running=True."""
        bot = Mock(spec=self.bot_module.EatventureBot)
        bot.running = True
        bot.mouse_controller = Mock()
        bot.mouse_controller.is_in_forbidden_zone = Mock(return_value=False)

        with patch.object(self.bot_module.EatventureBot, "__init__", lambda s: None):
            real = self.bot_module.EatventureBot.handle_upgrade_stats(bot, "UPGRADE_STATS")
        self.assertIs(real, self.bot_module.State.OPEN_BOXES)
        bot.mouse_controller.is_in_forbidden_zone.assert_called_once()

    # ------------------------------------------------------------------
    # Test 2: _invalidate_targets clears red_icons and red_observed_at
    # ------------------------------------------------------------------
    def test_invalidate_targets_clears_red_targets(self):
        """Calling _invalidate_targets resets red_icons and red_observed_at."""
        bot = Mock(spec=self.bot_module.EatventureBot)
        bot.red_icons = [(1.0, 100, 200), (0.9, 300, 400)]
        bot.red_observed_at = 42.0

        self.bot_module.EatventureBot._invalidate_targets(bot)
        self.assertEqual(bot.red_icons, [])
        self.assertIsNone(bot.red_observed_at)

    # ------------------------------------------------------------------
    # Test 3: _wait uses monotonic deadline (no negative sleep race)
    # ------------------------------------------------------------------
    def test_wait_uses_monotonic_deadline(self):
        """The _wait deadline is computed once with monotonic; work between
        polls does not cause a negative sleep request."""
        clock = FakeClock()
        time_mod = types.ModuleType("time")
        time_mod.monotonic = clock.value
        time_mod.sleep = clock.tick
        self.modules["time"] = time_mod

        with patch.dict(sys.modules, self.modules):
            bot_mod = _load_module("bot_deadline", "bot.py")

        bot = Mock(spec=self.bot_module.EatventureBot)
        bot.running = True
        bot.mouse_controller = Mock()
        bot.mouse_controller.check_held_input = Mock(
            return_value=Mock(value="sent"))

        with patch.object(bot_mod.metrics, "wait") as mock_wait:
            mock_wait.side_effect = clock.tick
            with patch.object(bot_mod.metrics, "measure"):
                bot_mod.EatventureBot._wait(bot, 10)

        calls = [a.args[0] for a in mock_wait.call_args_list]
        for c in calls:
            self.assertGreaterEqual(c, 0, f"Negative wait duration: {c}")

    # ------------------------------------------------------------------
    # Test 4: hold_at calls check_held_input during station hold
    # ------------------------------------------------------------------
    def test_hold_upgrade_station_polls_check_held_input(self):
        """handle_hold_upgrade_station calls check_held_input during the hold loop."""
        bot = Mock(spec=self.bot_module.EatventureBot)
        bot.running = True
        bot.window_capture = MagicMock()
        bot.mouse_controller = Mock()
        bot.mouse_controller.is_in_forbidden_zone = Mock(return_value=False)

        sent = self.bot_module.ActionResult.SENT
        bot._click = Mock(return_value=sent)
        bot._detect = Mock(side_effect=lambda *a, **kw: (True, 1.0, 320, 726))

        ctx = Mock()
        ctx.__enter__ = Mock(return_value=sent)
        ctx.__exit__ = Mock(return_value=False)
        bot.mouse_controller.pressed_at = Mock(return_value=ctx)
        bot.mouse_controller.check_held_input = Mock(return_value=sent)
        bot._invalidate_targets = Mock()
        bot.red_icon_processed_count = 0
        bot._wait = lambda *a, **kw: self.bot_module.EatventureBot._wait(bot, *a, **kw)

        with patch.object(self.bot_module.metrics, "measure"):
            with patch.object(self.bot_module.metrics, "wait") as mock_wait:
                mock_wait.side_effect = self.clock.tick
                result = self.bot_module.EatventureBot.handle_hold_upgrade_station(
                    bot, "HOLD_UPGRADE_STATION")

        self.assertIs(result, self.bot_module.State.OPEN_BOXES)
        bot.mouse_controller.check_held_input.assert_called()

    # ------------------------------------------------------------------
    # Test 5: _click invalidates targets on non-DENIED results
    # ------------------------------------------------------------------
    def test_click_invalidates_targets_on_sent_result(self):
        """_click calls _invalidate_targets when the mouse click is SENT."""
        bot = Mock(spec=self.bot_module.EatventureBot)
        bot.running = True
        bot.mouse_controller = Mock()
        click_sent = types.SimpleNamespace(value="sent")
        bot.mouse_controller.click = Mock(return_value=click_sent)

        self.bot_module.EatventureBot._click(bot, 100, 200)

        bot.mouse_controller.click.assert_called_once_with(100, 200, relative=True)
        bot._invalidate_targets.assert_called_once()

    def test_click_invalidates_targets_on_exception(self):
        """_click calls _invalidate_targets when mouse_controller.click raises."""
        bot = Mock(spec=self.bot_module.EatventureBot)
        bot.running = True
        bot.mouse_controller = Mock()
        bot.mouse_controller.click = Mock(side_effect=RuntimeError("fail"))

        with self.assertRaises(RuntimeError):
            self.bot_module.EatventureBot._click(bot, 100, 200)

        bot._invalidate_targets.assert_called_once()

    def test_click_does_not_invalidate_on_denied(self):
        """_click does NOT call _invalidate_targets when the result is DENIED."""
        bot = Mock(spec=self.bot_module.EatventureBot)
        bot.running = True
        bot.mouse_controller = Mock()
        click_denied = self.bot_module.ActionResult.DENIED
        bot.mouse_controller.click = Mock(return_value=click_denied)

        self.bot_module.EatventureBot._click(bot, 100, 200)

        bot._invalidate_targets.assert_not_called()


if __name__ == "__main__":
    unittest.main()
