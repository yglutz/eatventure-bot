"""Bounded, offline-testable latency telemetry; durations are seconds.

Wall/CPU samples include nested operations and must not be summed across scopes.
Work excludes only waits made through this collector, not arbitrary blocking I/O
or uninstrumented input sleeps. CPU is calling-thread CPU, not OpenCV workers.
"""
from collections import deque
from contextlib import contextmanager
import math
import threading
import time


class RuntimeMetrics:
    def __init__(self, max_series=256, max_samples=256, clock=time.perf_counter,
                 cpu_clock=time.thread_time, sleeper=time.sleep):
        if max_series < 1 or max_samples < 1:
            raise ValueError("metric bounds must be positive")
        self.max_series = max_series
        self.max_samples = max_samples
        self.clock = clock
        self.cpu_clock = cpu_clock
        self.sleeper = sleeper
        self._local = threading.local()
        self._lock = threading.Lock()
        self._series = {}
        self._dropped = 0

    def _wait_total(self):
        return getattr(self._local, 'wait_total', 0.0)

    def _record(self, name, labels, wall, work, cpu, failed):
        # Bound label size as well as cardinality and sample count.
        key = (str(name)[:128], tuple(sorted(
            (str(k)[:64], str(v)[:128]) for k, v in labels.items())[:16]))
        with self._lock:
            if key not in self._series:
                if len(self._series) >= self.max_series:
                    self._dropped += 1
                    return
                self._series[key] = [0, 0, deque(maxlen=self.max_samples)]
            series = self._series[key]
            series[0] += 1
            series[1] += int(failed)
            series[2].append((wall, work, cpu))

    @contextmanager
    def measure(self, name, **labels):
        start, cpu_start, waited = self.clock(), self.cpu_clock(), self._wait_total()
        failed = True
        try:
            yield
            failed = False
        finally:
            wall = max(0.0, self.clock() - start)
            cpu = max(0.0, self.cpu_clock() - cpu_start)
            work = max(0.0, wall - (self._wait_total() - waited))
            self._record(name, labels, wall, work, cpu, failed)

    def wait(self, seconds):
        """Sleep with normal time.sleep semantics and account actual elapsed wait."""
        start, cpu_start = self.clock(), self.cpu_clock()
        failed = True
        try:
            self.sleeper(seconds)
            failed = False
        finally:
            elapsed = max(0.0, self.clock() - start)
            self._local.wait_total = self._wait_total() + elapsed
            self._record('wait', {}, elapsed, 0.0,
                         max(0.0, self.cpu_clock() - cpu_start), failed)

    def wait_remaining(self, started, interval):
        """Enforce a minimum cycle interval using this collector's wall clock."""
        remaining = interval - (self.clock() - started)
        if remaining > 0:
            self.wait(remaining)

    def snapshot(self):
        """Lifetime counts and rolling-window latency distributions, bounded in size."""
        with self._lock:
            copied = [(key, count, errors, list(samples))
                      for key, (count, errors, samples) in self._series.items()]
            dropped = self._dropped
        result = []
        for (name, labels), count, errors, samples in copied:
            row = dict(name=name, labels=dict(labels), count=count, errors=errors,
                       samples=len(samples))
            for index, field in enumerate(('wall', 'work', 'cpu')):
                values = sorted(sample[index] for sample in samples)
                row[field] = dict(mean=sum(values) / len(values),
                                  p50=values[math.ceil(len(values) * .5) - 1],
                                  p95=values[math.ceil(len(values) * .95) - 1],
                                  max=values[-1])
            result.append(row)
        return dict(series=result, dropped_samples=dropped)


metrics = RuntimeMetrics()
