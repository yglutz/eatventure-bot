"""Offline only: every subprocess call is mocked; no attached device needed."""
import subprocess
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from adb_backend import ADBBackend, DEFAULT_ADB_PATH, logical_to_native


class ADBBackendTests(unittest.TestCase):
    def setUp(self):
        self.backend = ADBBackend(serial="ZT5TIJLVAQH6LJ7T", package="com.example.game",
                                  native_size=(12, 24), crop=(2, 4, 8, 16),
                                  logical_size=(4, 8), timeout=3)
        self.frame = np.zeros((24, 12, 3), dtype=np.uint8)
        self.frame[:] = (20, 40, 60)
        self.png = cv2.imencode(".png", self.frame)[1].tobytes()
        self.focus = b"  mCurrentFocus=Window{ab12 u0 com.example.game/.MainActivity}\n"
        self.rotation = b" SurfaceOrientation: 0\n"
        self.size = b"Physical size: 12x24\n"
        self.mock = patch("adb_backend.subprocess.run", side_effect=self.run_command)
        self.run = self.mock.start()
        self.addCleanup(self.mock.stop)

    def run_command(self, argv, **kwargs):
        self.assertEqual(argv[:3], [str(DEFAULT_ADB_PATH), "-s", "ZT5TIJLVAQH6LJ7T"])
        self.assertEqual(kwargs, dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     timeout=3, check=True, shell=False))
        responses = {("shell", "dumpsys", "window", "windows"): self.focus,
                     ("shell", "dumpsys", "input"): self.rotation,
                     ("shell", "wm", "size"): self.size,
                     ("exec-out", "screencap", "-p"): self.png}
        return subprocess.CompletedProcess(argv, 0, stdout=responses[tuple(argv[3:])])

    def test_capture_bgr_crop_resize_and_full_rect(self):
        result = self.backend.capture(max_y=3)
        self.assertEqual(result.shape, (3, 4, 3))
        self.assertTrue(result.flags.c_contiguous)
        self.assertEqual(result[0, 0].tolist(), [20, 40, 60])
        self.assertEqual(self.backend.last_capture_rect, (0, 0, 4, 8))
        self.assertEqual(self.run.call_count, 7)

    def test_input_is_unsupported_without_subprocess(self):
        for method, args in [("get_cursor_pos", ()), ("move_pointer", (1, 2)),
                             ("button_down", ()), ("button_up", ())]:
            with self.subTest(method=method), self.assertRaises(NotImplementedError):
                getattr(self.backend, method)(*args)
        self.run.assert_not_called()

    def test_bad_focus_fails_closed(self):
        for focus in [b"", b"mCurrentFocus=null", self.focus * 2,
                      self.focus.replace(b"com.example.game", b"com.example.game.other"),
                      b"mCurrentFocus=Window{garbage}", b"\xff"]:
            self.focus = focus
            self.backend.last_capture_rect = (0, 0, 4, 8)
            self.assertFalse(self.backend.is_window_active())
            self.assertIsNone(self.backend.last_capture_rect)

    def test_rotation_rejected(self):
        for rotation in [b"", b"SurfaceOrientation: 1", b"SurfaceOrientation: 0\nSurfaceOrientation: 0"]:
            self.rotation = rotation
            with self.assertRaises(RuntimeError):
                self.backend.capture()
            self.assertIsNone(self.backend.last_capture_rect)

    def test_size_rejected(self):
        for size in [b"", b"Physical size: 24x12", b"Physical size: 12x24\nOverride size: 6x12",
                     b"Physical size: 12x24\nOverride size: unknown"]:
            self.size = size
            with self.assertRaises(RuntimeError):
                self.backend.capture()
            self.assertIsNone(self.backend.last_capture_rect)

    def test_bad_png_and_dimensions(self):
        for png in [b"not png", b"\x89PNG\r\n\x1a\ninvalid",
                    cv2.imencode(".png", np.zeros((12, 24, 3), np.uint8))[1].tobytes()]:
            self.png = png
            with self.assertRaises(RuntimeError):
                self.backend.capture()
            self.assertIsNone(self.backend.last_capture_rect)

    def test_changes_after_capture_rejected(self):
        original = self.run_command
        for field, changed in [("rotation", b"SurfaceOrientation: 1"),
                               ("size", b"Physical size: 24x12"),
                               ("focus", b"mCurrentFocus=null")]:
            with self.subTest(field=field):
                self.set_valid_state()
                def change(argv, **kwargs):
                    result = original(argv, **kwargs)
                    if argv[3] == "exec-out":
                        setattr(self, field, changed)
                    return result
                self.run.side_effect = change
                with self.assertRaises(RuntimeError):
                    self.backend.capture()
                self.assertIsNone(self.backend.last_capture_rect)

    def set_valid_state(self):
        self.focus = b"mCurrentFocus=Window{ab12 u0 com.example.game/.MainActivity}\n"
        self.rotation = b"SurfaceOrientation: 0\n"
        self.size = b"Physical size: 12x24\n"

    def test_timeout_and_command_failure_clear_capture(self):
        for error in [subprocess.TimeoutExpired("adb", 3),
                      subprocess.CalledProcessError(1, "adb"), FileNotFoundError()]:
            self.run.side_effect = error
            self.backend.last_capture_rect = (0, 0, 4, 8)
            with self.assertRaises(RuntimeError):
                self.backend.capture()
            self.assertIsNone(self.backend.last_capture_rect)

    def test_close_is_idempotent_and_blocks_capture(self):
        self.backend.close()
        self.backend.close()
        self.assertIsNone(self.backend.last_capture_rect)
        self.assertFalse(self.backend.is_window_active())
        with self.assertRaises(RuntimeError):
            self.backend.capture()
        self.run.assert_not_called()

    def test_max_y_and_expected_rect(self):
        for invalid in [0, -1, True, 1.5]:
            with self.assertRaises(ValueError):
                self.backend.capture(invalid)
        self.run.assert_not_called()
        with self.assertRaises(RuntimeError):
            self.backend.validate((1, 0, 4, 8))

    def test_explicit_calibration_and_project_adb_required(self):
        base = dict(serial="serial", package="com.example.game", native_size=(12, 24),
                    crop=(0, 0, 12, 24), logical_size=(4, 8))
        for override in [dict(serial=""), dict(package=""), dict(adb_path="/usr/bin/adb"),
                         dict(crop=None), dict(crop=(0, 0, 13, 24)),
                         dict(logical_size=(0, 8)), dict(native_size=(True, 24)),
                         dict(timeout=0), dict(timeout=float("nan"))]:
            with self.subTest(override=override), self.assertRaises(ValueError):
                ADBBackend(**(base | override))
        self.run.assert_not_called()

    def test_mapping_is_pure_and_bounded(self):
        calibration = dict(native_size=(1080, 2400), crop=(30, 60, 900, 1800),
                           logical_size=(300, 600))
        self.assertEqual(logical_to_native(0, 0, **calibration), (31, 61))
        self.assertEqual(logical_to_native(299, 599, **calibration), (928, 1858))
        for point in [(-1, 0), (300, 0), (0, 600), (float("nan"), 0), (True, 0)]:
            with self.assertRaises(ValueError):
                logical_to_native(*point, **calibration)
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
