"""Opt-in, capture-only Android adapter. Import/construction never contacts a device.

Calibration is supplied by the caller, not inferred from the discovered device.
Only portrait rotation zero is supported. Android dumpsys formats not recognized
here are rejected rather than guessed. No Android input command is implemented.
"""
import math
from pathlib import Path
import re
import subprocess

import cv2
import numpy as np


DEFAULT_ADB_PATH = Path(__file__).resolve().parent / "platform-tools" / "adb"


def _positive_size(value, name):
    if (not isinstance(value, (tuple, list)) or len(value) != 2
            or any(type(v) is not int or v <= 0 for v in value)):
        raise ValueError(f"{name} must contain two positive integers")
    return tuple(value)


def _calibration(native_size, crop, logical_size):
    native_size = _positive_size(native_size, "native_size")
    logical_size = _positive_size(logical_size, "logical_size")
    if (not isinstance(crop, (tuple, list)) or len(crop) != 4
            or any(type(v) is not int for v in crop)):
        raise ValueError("crop must be explicit native (x, y, width, height)")
    x, y, width, height = crop
    if (x < 0 or y < 0 or width <= 0 or height <= 0
            or x + width > native_size[0] or y + height > native_size[1]):
        raise ValueError("crop is outside native bounds")
    return native_size, tuple(crop), logical_size


def logical_to_native(x, y, *, native_size, crop, logical_size):
    """Map logical pixel centers to native pixels, without performing input.

    Coordinates are bounded in the logical frame; rounding uses the containing
    native pixel after center scaling (the same crop convention used by capture).
    """
    _, (left, top, width, height), (lw, lh) = _calibration(
        native_size, crop, logical_size)
    if (isinstance(x, bool) or isinstance(y, bool)
            or not isinstance(x, (int, float)) or not isinstance(y, (int, float))
            or not math.isfinite(x) or not math.isfinite(y)
            or not 0 <= x < lw or not 0 <= y < lh):
        raise ValueError("point is outside logical bounds")
    return (left + min(width - 1, math.floor((x + 0.5) * width / lw)),
            top + min(height - 1, math.floor((y + 0.5) * height / lh)))


class ADBBackend:
    def __init__(self, *, serial, package, native_size, crop, logical_size,
                 adb_path=DEFAULT_ADB_PATH, timeout=10):
        if not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", serial):
            raise ValueError("An explicit device serial is required")
        if not isinstance(package, str) or not re.fullmatch(r"[A-Za-z][\w]*(?:\.[A-Za-z][\w]*)+", package):
            raise ValueError("An explicit Android package is required")
        path = Path(adb_path)
        # Never fall back to PATH's adb or another platform-tools installation.
        if path.resolve() != DEFAULT_ADB_PATH.resolve():
            raise ValueError("Only the project platform-tools/adb is permitted")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.native_size, self.crop, self.logical_size = _calibration(native_size, crop, logical_size)
        self.serial, self.package = serial, package
        self.adb_path, self.timeout = str(path.resolve()), timeout
        self.target_width, self.target_height = self.logical_size
        self.hwnd = None  # Android has no desktop window/input handle.
        self.last_capture_rect = None
        self._closed = False

    def _run(self, *args):
        if self._closed:
            raise RuntimeError("ADB backend is closed")
        try:
            result = subprocess.run(
                [self.adb_path, "-s", self.serial, *args],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=self.timeout, check=True, shell=False)
            return result.stdout
        except (OSError, subprocess.SubprocessError) as exc:
            self.last_capture_rect = None
            raise RuntimeError("ADB command failed") from exc

    def get_window_rect(self):
        if self._closed:
            raise RuntimeError("ADB backend is closed")
        return (0, 0, *self.logical_size)

    def validate(self, expected_rect=None):
        try:
            window = self._run("shell", "dumpsys", "window", "windows").decode("utf-8", errors="strict")
            focus_lines = re.findall(r"^\s*mCurrentFocus=(.*)$", window, re.MULTILINE)
            if len(focus_lines) != 1:
                raise RuntimeError("Cannot establish unique Android focus")
            focus = re.fullmatch(r"Window\{[0-9a-fA-F]+ u\d+ ([A-Za-z][\w.]*)/[^\s{}]+\}", focus_lines[0].strip())
            if focus is None or focus[1] != self.package:
                raise RuntimeError("Target package does not have verified focus")
            state = self._run("shell", "dumpsys", "input").decode("utf-8", errors="strict")
            rotations = re.findall(r"^\s*SurfaceOrientation:\s*(\d+)\s*$", state, re.MULTILINE)
            if rotations != ["0"]:
                raise RuntimeError("Unverified or unsupported Android rotation")
            size = self._run("shell", "wm", "size").decode("utf-8", errors="strict")
            size_lines = [line.strip() for line in size.splitlines() if line.strip()]
            sizes = [re.fullmatch(r"(Physical|Override) size: (\d+)x(\d+)", line)
                     for line in size_lines]
            if (not sizes or any(match is None for match in sizes)
                    or sizes[0][1] != "Physical" or len(sizes) > 2
                    or (len(sizes) == 2 and sizes[1][1] != "Override")
                    or any((int(match[2]), int(match[3])) != self.native_size for match in sizes)):
                raise RuntimeError("Unverified or changed Android dimensions")
            rect = self.get_window_rect()
            if expected_rect is not None and expected_rect != rect:
                raise RuntimeError("Capture calibration changed")
            return rect
        except Exception:
            self.last_capture_rect = None
            raise

    def capture(self, max_y=None):
        self.last_capture_rect = None
        if max_y is not None and (type(max_y) is not int or max_y <= 0):
            raise ValueError("max_y must be a positive integer")
        rect = self.validate()
        png = self._run("exec-out", "screencap", "-p")
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("ADB capture is not PNG")
        frame = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None or frame.shape != (self.native_size[1], self.native_size[0], 3):
            raise RuntimeError("Unexpected native capture dimensions")
        self.validate(expected_rect=rect)
        x, y, width, height = self.crop
        result = cv2.resize(frame[y:y + height, x:x + width], self.logical_size,
                            interpolation=cv2.INTER_AREA)
        if max_y is not None:
            result = result[:max_y]
        result = np.ascontiguousarray(result)
        self.last_capture_rect = rect
        return result  # BGR, matching desktop backends.

    def is_window_active(self):
        try:
            self.validate()
            return True
        except Exception:
            return False

    def get_cursor_pos(self):
        raise NotImplementedError("ADB capture-only stage has no pointer or input support")

    def move_pointer(self, screen_x, screen_y):
        raise NotImplementedError("ADB input is blocked pending device gesture validation")

    def button_down(self):
        raise NotImplementedError("ADB input is blocked pending device gesture validation")

    def button_up(self):
        raise NotImplementedError("ADB input is blocked pending device gesture validation")

    def close(self):
        self.last_capture_rect = None
        self._closed = True
