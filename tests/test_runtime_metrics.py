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

    def test_capture_wrapper_offline(self):
        import window_capture
        frame = object()
        backend = Mock()
        backend.capture.return_value = frame
        capture = window_capture.WindowCapture(backend=backend)
        with patch.object(window_capture, 'metrics', self.metrics):
            self.assertIs(capture.capture(400), frame)
        backend.capture.assert_called_once_with(400)
        self.assertEqual(self.rows()['capture']['labels'], {'max_y': '400'})

    def test_full_capture_latency_and_failure_accounted(self):
        import window_capture

        def capture_frame():
            self.clock.work(2)
            raise RuntimeError('capture failed')

        backend = Mock()
        backend.capture.side_effect = capture_frame
        capture = window_capture.WindowCapture(backend=backend)
        with patch.object(window_capture, 'metrics', self.metrics):
            with self.assertRaisesRegex(RuntimeError, 'capture failed'):
                capture.capture()
        backend.capture.assert_called_once_with()
        row = self.rows()['capture']
        self.assertEqual(row['labels'], {'max_y': 'None'})
        self.assertEqual(row['errors'], 1)
        self.assertEqual(row['wall']['mean'], 2)
        self.assertEqual(row['cpu']['mean'], 2)


if __name__ == '__main__':
    unittest.main()
