"""Offline backend contract tests: never connect to a desktop."""
import os
import ctypes
import importlib.util
from pathlib import Path
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import numpy as np

from window_capture import WindowCapture, ForbiddenAreaOverlay
from x11_backend import X11Backend


class PlatformCaptureTests(unittest.TestCase):
    def test_nonzero_client_translation_direction(self):
        backend = X11Backend.__new__(X11Backend)
        backend.root = Mock()
        window = Mock()
        window.get_geometry.return_value = NS(width=20, height=10)
        backend.root.translate_coords.return_value = NS(x=137, y=249)
        self.assertEqual(backend._rect(window), (137, 249, 20, 10))
        backend.root.translate_coords.assert_called_once_with(window, 0, 0)
        window.translate_coords.assert_not_called()

    def test_windows_capture_failure_cleanup_and_provenance(self):
        gui, ui, api, constants = Mock(), Mock(), Mock(), Mock()
        windll = Mock()
        modules = dict(win32gui=gui, win32ui=ui, win32api=api, win32con=constants)
        spec = importlib.util.spec_from_file_location(
            "offline_windows_backend", Path(__file__).resolve().parents[1] / "windows_backend.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict("sys.modules", modules), patch.object(ctypes, "windll", windll, create=True):
            spec.loader.exec_module(module)
            backend = module.WindowsBackend.__new__(module.WindowsBackend)
            backend.hwnd = 42
            backend.get_window_rect = Mock(return_value=(137, 249, 2, 1))
            source = ui.CreateDCFromHandle.return_value
            capture = source.CreateCompatibleDC.return_value
            bitmap = ui.CreateBitmap.return_value
            bitmap.GetBitmapBits.return_value = bytes([1, 2, 3, 255] * 2)
            for failure in ("print", "pixels", "geometry", "success"):
                with self.subTest(failure=failure):
                    gui.reset_mock()
                    source.DeleteDC.reset_mock()
                    capture.DeleteDC.reset_mock()
                    capture.SelectObject.reset_mock()
                    windll.user32.PrintWindow.return_value = failure != "print"
                    bitmap.GetBitmapBits.side_effect = RuntimeError("pixels") if failure == "pixels" else None
                    backend.get_window_rect.side_effect = (
                        [(137, 249, 2, 1), (138, 249, 2, 1)] if failure == "geometry" else None)
                    backend.last_capture_rect = (0, 0, 9, 9)
                    if failure == "success":
                        self.assertEqual(backend.capture().shape, (1, 2, 3))
                        self.assertEqual(backend.last_capture_rect, (137, 249, 2, 1))
                    else:
                        with self.assertRaises(RuntimeError):
                            backend.capture()
                        self.assertIsNone(backend.last_capture_rect)
                    self.assertEqual(capture.SelectObject.call_count, 2)
                    gui.DeleteObject.assert_called_once_with(bitmap.GetHandle())
                    capture.DeleteDC.assert_called_once()
                    source.DeleteDC.assert_called_once()
                    gui.ReleaseDC.assert_called_once_with(42, gui.GetWindowDC.return_value)
            gui.reset_mock()
            source.DeleteDC.reset_mock()
            source.CreateCompatibleDC.side_effect = RuntimeError("allocation")
            backend.get_window_rect.side_effect = None
            backend.last_capture_rect = (0, 0, 9, 9)
            with self.assertRaises(RuntimeError):
                backend.capture()
            self.assertIsNone(backend.last_capture_rect)
            source.DeleteDC.assert_called_once()
            gui.ReleaseDC.assert_called_once_with(42, gui.GetWindowDC.return_value)

    def backend(self):
        backend = X11Backend.__new__(X11Backend)
        backend.last_capture_rect = None
        backend.grabber = Mock()
        backend.grabber.grab.return_value = np.zeros((10, 20, 4), dtype=np.uint8)
        backend.validate = Mock(return_value=(2, 3, 20, 10))
        return backend

    def test_capture_bgr_and_full_geometry(self):
        backend = self.backend()
        backend.grabber.grab.return_value[:, :, :] = [1, 2, 3, 255]
        image = backend.capture()
        self.assertEqual(image[0, 0].tolist(), [1, 2, 3])
        self.assertTrue(image.flags.c_contiguous)
        self.assertEqual(backend.last_capture_rect, (2, 3, 20, 10))
        backend.validate.assert_called_with(expected_rect=(2, 3, 20, 10))

    def test_crop_and_stale_capture(self):
        backend = self.backend()
        backend.grabber.grab.return_value = np.zeros((5, 20, 4), dtype=np.uint8)
        self.assertEqual(backend.capture(5).shape, (5, 20, 3))
        self.assertEqual(backend.last_capture_rect, (2, 3, 20, 10))
        backend.validate.side_effect = [(2, 3, 20, 10), RuntimeError("moved")]
        with self.assertRaises(RuntimeError):
            backend.capture(5)
        self.assertIsNone(backend.last_capture_rect)

    def test_invalid_capture(self):
        backend = self.backend()
        with self.assertRaises(ValueError):
            backend.capture(0)
        backend.grabber.grab.return_value = np.zeros((1, 1, 4), dtype=np.uint8)
        with self.assertRaises(RuntimeError):
            backend.capture()

    def test_reject_wayland_and_missing_display_before_imports(self):
        for env in ({"WAYLAND_DISPLAY": "wayland-0"}, {}):
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaises(RuntimeError):
                    X11Backend("scrcpy")

    def test_facade_shares_backend(self):
        backend = Mock(hwnd=42, last_capture_rect=None)
        with patch("window_capture.sys.platform", "linux"), patch("x11_backend.X11Backend", return_value=backend):
            capture = WindowCapture("scrcpy", 20, 10)
        self.assertIs(capture.backend, backend)
        self.assertEqual(capture.hwnd, 42)
        capture.capture()
        backend.capture.assert_called_once_with()

    def test_facade_preserves_legacy_no_argument_backend(self):
        frame = object()

        class LegacyBackend:
            def capture(self):
                return frame

        capture = WindowCapture(backend=LegacyBackend())
        self.assertIs(capture.capture(), frame)
        self.assertIs(capture.capture(None), frame)

    def test_facade_explicit_crop_uses_backend_validation(self):
        # Synthetic X11 contract coverage, not live cropped-capture evidence.
        backend = self.backend()
        capture = WindowCapture(backend=backend)
        backend.grabber.grab.return_value = np.zeros((5, 20, 4), dtype=np.uint8)
        self.assertEqual(capture.capture(5).shape, (5, 20, 3))
        backend.grabber.grab.assert_called_once_with(
            dict(left=2, top=3, width=20, height=5))
        self.assertEqual(capture.last_capture_rect, (2, 3, 20, 10))
        backend.grabber.grab.reset_mock()
        with self.assertRaises(ValueError):
            capture.capture(0)
        backend.grabber.grab.assert_not_called()
        self.assertIsNone(capture.last_capture_rect)

    def test_overlay_explicitly_unsupported(self):
        with patch("window_capture.sys.platform", "linux"):
            overlay = ForbiddenAreaOverlay(42, [])
        with self.assertRaises(RuntimeError):
            overlay.start()
        overlay.stop()

    def test_release_does_not_require_focus(self):
        backend = self.backend()
        backend.X = NS(ButtonRelease=5)
        backend.xtest = Mock()
        backend.display = Mock()
        backend.validate.side_effect = RuntimeError("lost focus")
        backend.button_up()
        backend.validate.assert_not_called()
        backend.xtest.fake_input.assert_called_once_with(backend.display, 5, 1)

    def test_validation_focus_mapping_geometry_and_occlusion(self):
        backend = X11Backend.__new__(X11Backend)
        backend.X = NS(IsViewable=2, InputOnly=2, AnyPropertyType=0)
        backend.hwnd = 42
        backend.target_width, backend.target_height = 20, 10
        backend.root = Mock(id=1)
        backend.root.get_geometry.return_value = NS(width=100, height=100)
        backend.window = Mock(id=42)
        backend.window.get_attributes.return_value = NS(map_state=2)
        backend.window.query_tree.return_value = NS(parent=backend.root)
        backend.root.query_tree.return_value = NS(children=[backend.window])
        backend.display = Mock()
        backend.display.get_input_focus.return_value = NS(focus=NS(id=42))
        backend._property = Mock(return_value=[42])
        backend.get_window_rect = Mock(return_value=(2, 3, 20, 10))
        self.assertEqual(backend.validate(), (2, 3, 20, 10))
        with self.assertRaises(RuntimeError):
            backend.validate((3, 3, 20, 10))
        backend.display.get_input_focus.return_value = NS(focus=NS(id=99))
        with self.assertRaises(RuntimeError):
            backend.validate()
        backend.display.get_input_focus.return_value = NS(focus=NS(id=42))
        backend.window.get_attributes.return_value = NS(map_state=0)
        with self.assertRaises(RuntimeError):
            backend.validate()
        backend.window.get_attributes.return_value = NS(map_state=2)
        backend.get_window_rect.return_value = (2, 3, 21, 10)
        with self.assertRaises(RuntimeError):
            backend.validate()
        backend.get_window_rect.return_value = (2, 3, 20, 10)
        popup = Mock(id=99)
        popup.get_attributes.return_value = NS(map_state=2, win_class=1)
        backend.root.query_tree.return_value = NS(children=[backend.window, popup])
        backend._rect = Mock(return_value=(5, 5, 10, 10))
        with self.assertRaises(RuntimeError):
            backend.validate()

    def test_close_resources_even_when_capture_close_fails(self):
        backend = self.backend()
        display = backend.display = Mock()
        backend.grabber.close.side_effect = RuntimeError("failed")
        with self.assertRaises(RuntimeError):
            backend.close()
        display.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
