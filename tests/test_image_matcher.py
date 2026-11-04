"""Synthetic detection invariants; not gameplay precision/recall evidence."""
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _has_cv2():
    try:
        import cv2  # noqa: F401
        return True
    except ModuleNotFoundError:
        return False


class FakeCv2:
    """Minimal cv2 stub so test_bot_runtime can import image_matcher."""
    IMREAD_UNCHANGED = 1
    TM_SQDIFF_NORMED = 0
    matchTemplate = None
    minMaxLoc = None
    resize = None
    bitwise_and = None
    calcHist = None
    normalize = None
    compareHist = None
    HISTCMP_CORREL = 0
    COLOR_BGRA2BGR = 1
    NORM_MINMAX = 1


# Skip the whole test class when cv2 is not installed.
@unittest.skipUnless(_has_cv2(), "cv2 (opencv-python) not available")
class MatcherTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        from image_matcher import ImageMatcher
        from runtime_metrics import RuntimeMetrics
        self.np = np
        self.ImageMatcher = ImageMatcher
        self.RuntimeMetrics = RuntimeMetrics
        rng = np.random.default_rng(1701)
        self.template = rng.integers(1, 255, (12, 10, 3), dtype=np.uint8)
        self.screen = rng.integers(1, 255, (80, 90, 3), dtype=np.uint8)
        self.screen[23:35, 31:41] = self.template
        self.matcher = ImageMatcher(.999)

    def test_exact_match_and_labels(self):
        metrics = self.RuntimeMetrics()
        with patch('image_matcher.metrics', metrics):
            found, confidence, x, y = self.matcher.find_template(
                self.screen, self.template, template_name='synthetic', check_color=True)
        self.assertTrue(found)
        self.assertAlmostEqual(confidence, 1, places=5)
        self.assertEqual((x, y), (36, 29))
        row = metrics.snapshot()['series'][0]
        self.assertEqual(row['labels'], {'template': 'synthetic', 'region': '(80, 90, 3)'})

    def test_masked_and_multiscale_wrappers_equal_original_path(self):
        mask = self.np.full(self.template.shape[:2], 255, dtype=self.np.uint8)
        mask[:2] = 0
        for mask_arg in (None, mask):
            args = (self.screen, self.template, mask_arg)
            self.assertEqual(self.matcher.find_template(*args, check_color=True),
                             self.matcher._find_template(*args, check_color=True))
            self.assertEqual(self.matcher.find_all_templates(*args, scales=[.8, 1., 1.2]),
                             self.matcher._find_all_templates(*args, scales=[.8, 1., 1.2]))

    def test_multiple_instances_and_oversized_template(self):
        self.screen[50:62, 60:70] = self.template
        matches = self.matcher.find_all_templates(self.screen, self.template)
        self.assertEqual({(x, y) for _, x, y in matches}, {(36, 29), (65, 56)})
        self.assertEqual(self.matcher.find_template(self.template, self.screen),
                         (False, 0.0, 0, 0))
        self.assertEqual(self.matcher.find_all_templates(self.template, self.screen), [])

    def test_suppression_preserves_stable_ties(self):
        candidates = [(1., 20, 20, 10, 10), (1., 21, 20, 10, 10),
                      (.99, 50, 50, 10, 10)]
        self.assertEqual(self.matcher._non_max_suppression(candidates, 15),
                         [candidates[0], candidates[2]])


if __name__ == '__main__':
    unittest.main()
