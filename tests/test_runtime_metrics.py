import importlib.util
from pathlib import Path
import threading
import types
import unittest
from unittest.mock import patch, Mock

from runtime_metrics import RuntimeMetrics


class FakeClock:
    def __init__(self):
        self.wall = 0.0
        self.cpu = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.wall += seconds

    def work(self, seconds):
        self.wall += seconds
        self.cpu += seconds


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.metrics = RuntimeMetrics(clock=lambda: self.clock.wall,
                                      cpu_clock=lambda: self.clock.cpu,
                                      sleeper=self.clock.sleep)

    def rows(self):
        return {r['name']: r for r in self.metrics.snapshot()['series']}

    def test_nested_wait_excluded_once_from_each_scope(self):
        with self.metrics.measure('state'):
            self.clock.work(2)
            with self.metrics.measure('input'):
                self.clock.work(1)
                self.metrics.wait(5)
            self.metrics.wait(3)
        rows = self.rows()
        self.assertEqual(rows['state']['wall']['mean'], 11)
        self.assertEqual(rows['state']['work']['mean'], 3)
        self.assertEqual(rows['state']['cpu']['mean'], 3)
        self.assertEqual(rows['input']['work']['mean'], 1)
        self.assertEqual(rows['wait']['work']['mean'], 0)

    def test_exception_recorded_and_propagated(self):
        with self.assertRaisesRegex(ValueError, 'failure'):
            with self.metrics.measure('broken'):
                self.clock.work(2)
                raise ValueError('failure')
        self.assertEqual(self.rows()['broken']['errors'], 1)

    def test_interrupted_wait_accounted_and_propagated(self):
        def interrupted(seconds):
            self.clock.wall += 2
            raise InterruptedError()
        self.metrics.sleeper = interrupted
        with self.assertRaises(InterruptedError):
            with self.metrics.measure('state'):
                self.metrics.wait(10)
        self.assertEqual(self.rows()['state']['work']['mean'], 0)
        self.assertEqual(self.rows()['wait']['wall']['mean'], 2)
        self.assertEqual(self.rows()['wait']['errors'], 1)

    def test_memory_bounds_and_rolling_percentiles(self):
        self.metrics.max_series = 2
        self.metrics.max_samples = 3
        for duration in range(1, 6):
            with self.metrics.measure('work', region='small'):
                self.clock.work(duration)
        with self.metrics.measure('second'):
            pass
        with self.metrics.measure('overflow'):
            pass
        snapshot = self.metrics.snapshot()
        row = self.rows()['work']
        self.assertEqual((row['count'], row['samples']), (5, 3))
        self.assertEqual(row['wall'], dict(mean=4, p50=4, p95=5, max=5))
        self.assertEqual(snapshot['dropped_samples'], 1)
        self.assertEqual(len(snapshot['series']), 2)

    def test_cycle_wait_is_remaining_not_additive(self):
        started = self.metrics.clock()
        self.clock.work(.10)
        self.metrics.wait_remaining(started, .15)
        self.assertAlmostEqual(self.clock.wall, .15)
        started = self.metrics.clock()
        self.clock.work(.30)
        self.metrics.wait_remaining(started, .15)
        self.assertEqual(len(self.clock.sleeps), 1)

    def test_other_thread_wait_not_subtracted(self):
        with self.metrics.measure('parent'):
            thread = threading.Thread(target=lambda: self.metrics.wait(5))
            thread.start()
            thread.join()
        self.assertEqual(self.rows()['parent']['work']['mean'], 5)

    def test_state_timing_does_not_change_transition(self):
        import state_machine
        machine = state_machine.StateMachine()
        def handler(state):
            self.metrics.wait(1)
            return state_machine.State.SCROLL
        machine.register_handler(state_machine.State.FIND_RED_ICONS, handler)
        with patch.object(state_machine, 'metrics', self.metrics):
            self.assertTrue(machine.update())
        self.assertEqual(machine.get_state(), state_machine.State.SCROLL)
        self.assertEqual(self.rows()['state']['labels']['state'], 'FIND_RED_ICONS')
        self.assertEqual(self.rows()['state']['work']['mean'], 0)

    def test_main_keys_offline(self):
        # Import with inert platform/bot modules: no Win32, listener or notifications.
        pynput = types.ModuleType('pynput')
        pynput.keyboard = Mock()
        bot_module = types.ModuleType('bot')
        bot_module.EatventureBot = Mock()
        modules = {'win32api': Mock(), 'win32gui': Mock(),
                   'pynput': pynput, 'bot': bot_module}
        spec = importlib.util.spec_from_file_location(
            'offline_main', Path(__file__).resolve().parents[1] / 'main.py')
        main = importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules', modules):
            spec.loader.exec_module(main)
        main.bot_instance = Mock(running=False)
        with patch.object(main.time, 'monotonic', return_value=123.0):
            main.on_press(types.SimpleNamespace(char='z'))
        self.assertEqual(main.bot_instance.current_level_start_time, 123.0)
        self.assertTrue(main.bot_instance.running)
        main.on_press(types.SimpleNamespace(char='p'))
        self.assertFalse(main.bot_instance.running)
        self.assertTrue(main.should_exit)

        # One inert main-loop iteration verifies stopped backoff remains intact.
        main.should_exit = False
        idle_bot = Mock(running=False)
        def stop_after_sleep(seconds):
            self.clock.sleep(seconds)
            main.should_exit = True
        self.metrics.sleeper = stop_after_sleep
        with patch.object(main, 'EatventureBot', return_value=idle_bot), \
                patch.object(main, 'metrics', self.metrics), \
                patch.object(main, 'setup_logging'), patch('builtins.print'):
            self.assertEqual(main.main(), 0)
        self.assertEqual(self.clock.sleeps, [.5])
        idle_bot.state_machine.update.assert_not_called()

    def test_capture_wrapper_offline(self):
        # Exercise only wrapper; never allocate Win32 resources or call capture.
        import ctypes
        modules = {name: Mock() for name in ('win32api', 'win32gui', 'win32ui',
                                             'win32con', 'numpy', 'PIL')}
        spec = importlib.util.spec_from_file_location(
            'offline_capture', Path(__file__).resolve().parents[1] / 'window_capture.py')
        capture_module = importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules', modules), patch.object(ctypes, 'windll', Mock(), create=True):
            spec.loader.exec_module(capture_module)
        capture = capture_module.WindowCapture.__new__(capture_module.WindowCapture)
        frame = object()
        capture._capture = Mock(return_value=frame)
        with patch.object(capture_module, 'metrics', self.metrics):
            self.assertIs(capture.capture(400), frame)
        capture._capture.assert_called_once_with(400)
        self.assertEqual(self.rows()['capture']['labels'], {'max_y': '400'})


if __name__ == '__main__':
    unittest.main()
