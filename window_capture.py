"""Platform-neutral capture facade; desktop dependencies load on construction."""
import sys


class WindowCapture:
    def __init__(self, window_title=None, target_width=800, target_height=600, *, backend=None):
        if backend is not None:
            self.backend = backend
            return
        if sys.platform == "win32":
            from windows_backend import WindowsBackend
            backend_type = WindowsBackend
        elif sys.platform.startswith("linux"):
            from x11_backend import X11Backend
            backend_type = X11Backend
        else:
            raise RuntimeError("Unsupported desktop platform")
        self.backend = backend_type(window_title, target_width, target_height)

    @property
    def supports_overlay(self):
        return type(self.backend).__module__ == 'windows_backend'

    @property
    def hwnd(self):
        return self.backend.hwnd

    @property
    def last_capture_rect(self):
        return self.backend.last_capture_rect

    def __getattr__(self, name):
        return getattr(self.backend, name)


class ForbiddenAreaOverlay:
    def __init__(self, target_hwnd, forbidden_zones):
        self._overlay = None
        if sys.platform == "win32":
            from windows_backend import ForbiddenAreaOverlay as WindowsOverlay
            self._overlay = WindowsOverlay(target_hwnd, forbidden_zones)

    def start(self):
        if self._overlay is None:
            raise RuntimeError("Forbidden-area overlay is unsupported on this platform")
        self._overlay.start()

    def stop(self):
        if self._overlay is not None:
            self._overlay.stop()
