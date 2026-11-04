"""Offline seeded instrumentation-cost comparison, not a gameplay benchmark.

Run: python tools/benchmark_runtime.py --iterations 100
Compares unchanged uninstrumented matching bodies to their measured wrappers.
No capture, input, bot import, network, or notifications.
"""
import argparse
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cv2
import numpy as np
from image_matcher import ImageMatcher
from runtime_metrics import metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iterations', type=int, default=100)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error('iterations must be positive')
    cv2.setNumThreads(1)
    rng = np.random.default_rng(1701)
    screen = rng.integers(1, 255, (660, 360, 3), dtype=np.uint8)
    template = screen[200:224, 100:124].copy()
    matcher = ImageMatcher(.98)
    report = dict(python=platform.python_version(), opencv=cv2.__version__,
                  numpy=np.__version__, seed=1701, shape=screen.shape,
                  iterations=args.iterations, opencv_threads=1, workloads={})
    for name, baseline, measured, kwargs in (
        ('single_color', matcher._find_template, matcher.find_template, {'check_color': True}),
        ('all', matcher._find_all_templates, matcher.find_all_templates, {}),
    ):
        for _ in range(5):
            baseline(screen, template, **kwargs)
            measured(screen, template, **kwargs)
        samples = {'baseline': [], 'instrumented': []}
        for iteration in range(args.iterations):
            paths = [('baseline', baseline), ('instrumented', measured)]
            if iteration % 2:
                paths.reverse()
            outputs = []
            for label, fn in paths:
                start = time.perf_counter()
                outputs.append(fn(screen, template, **kwargs))
                samples[label].append(time.perf_counter() - start)
            assert outputs[0] == outputs[1], 'Detection changed'
        report['workloads'][name] = {
            label: dict(median_ms=statistics.median(values) * 1000,
                        p95_ms=sorted(values)[math.ceil(len(values) * .95) - 1] * 1000)
            for label, values in samples.items()}
    report['metrics'] = metrics.snapshot()
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
