"""Opt-in Android adapter. Import/construction never contacts a device.

Calibration is supplied by the caller, not inferred from the discovered device.
Only portrait rotation zero is supported. Android dumpsys formats not recognized
here are rejected rather than guessed. Input is experimental and disabled by default.
"""
import math
from pathlib import Path
import re
import subprocess
import time

from adb_input import ExperimentalADBInput

import cv2
import numpy as np


DEFAULT_ADB_PATH = Path(__file__).resolve().parent / "platform-tools" / "adb"


def _command_failure_reason(exc):
    """Expose known adb diagnostics only, never arbitrary device/capture bytes."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timed out"
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if isinstance(stderr, str):
        for diagnostic in ("unauthorized", "device offline", "no devices/emulators found",
                           "permission denied"):
            if diagnostic in stderr.lower():
                return diagnostic
    if isinstance(exc, subprocess.CalledProcessError):
        return f"exit status {exc.returncode} (unrecognized stderr withheld)"
    return "could not execute project adb"


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


class ADBBackend(ExperimentalADBInput):
    def __init__(self, *, serial, package, native_size, crop, logical_size,
                 adb_path=DEFAULT_ADB_PATH, timeout=10, enable_input=False, cancel_check=None):
        if type(enable_input) is not bool or (cancel_check is not None and not callable(cancel_check)):
            raise ValueError("enable_input must be bool and cancel_check callable")
        self.enable_input, self.cancel_check = enable_input, cancel_check
        self._pressed = self._input_fault = False
        self._pointer = (0, 0)
        self._native_pointer = None
        self._input_deadline = None
        self._input_releasing = False
        self._gesture_signature = None
        self._gesture_deadline = None
        self._requested_gesture_deadline = None
        self._capture_signature = None
        self._capture_time = 0
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
        self._transport = (self.adb_path, "-s", self.serial)
        self.target_width, self.target_height = self.logical_size
        self.hwnd = None  # Android has no desktop window/input handle.
        self.last_capture_rect = None
        self._closed = False

    def _run(self, *args):
        if self._closed:
            raise RuntimeError("ADB backend is closed")
        try:
            timeout = self.timeout
            guarded = self._pressed or self._input_deadline is not None
            if guarded:
                timeout = min(timeout, 2)
                if self._input_releasing:
                    timeout = min(timeout, self._input_deadline - time.monotonic())
                else:
                    self._check_running_gesture()
                    if self._input_deadline is not None:
                        timeout = min(timeout, self._input_deadline - time.monotonic())
                    if self._pressed:
                        timeout = min(timeout, self._gesture_deadline - time.monotonic())
                if timeout <= 0:
                    raise subprocess.TimeoutExpired("adb gesture operation", 2)
            result = subprocess.run(
                [*self._transport, *args],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout, check=True, shell=False)
            if guarded and not self._input_releasing:
                self._check_running_gesture()
            return result.stdout
        except (OSError, subprocess.SubprocessError) as exc:
            if self._pressed:
                self._input_fault = True
            self.last_capture_rect = None
            # Chained subprocess exceptions can include raw stdout (a screenshot).
            operation = " ".join(args)
            raise RuntimeError(
                f"ADB {operation} failed: {_command_failure_reason(exc)}") from None
        except BaseException:
            if self._pressed:
                self._input_fault = True
                self.last_capture_rect = None
            raise

    def get_window_rect(self):
        if self._closed:
            raise RuntimeError("ADB backend is closed")
        return (0, 0, *self.logical_size)

    def validate(self, expected_rect=None):
        try:
            # Android 13 may omit mCurrentFocus from the narrower 'windows' dump.
            window = self._run("shell", "dumpsys", "window").decode("utf-8", errors="strict")
            focus_lines = re.findall(r"^\s*mCurrentFocus=(.*)$", window, re.MULTILINE)
            if len(focus_lines) != 1:
                raise RuntimeError("Cannot establish unique Android focus")
            focus = re.fullmatch(r"Window\{[0-9a-fA-F]+ u\d+ ([A-Za-z][\w.]*)/[^\s{}]+\}", focus_lines[0].strip())
            if focus is None or focus[1] != self.package:
                raise RuntimeError("Target package does not have verified focus")
            state = self._run("shell", "dumpsys", "input").decode("utf-8", errors="strict")
            rotations = re.findall(r"^[ \t]*SurfaceOrientation:[ \t]*(.*)$", state, re.MULTILINE)
            window_rotations = re.findall(
                r"^[ \t]*mCurrentRotation=[ \t]*(.*)$", window, re.MULTILINE)
            # Either recognized current-state source suffices; every available
            # source must agree, with no duplicate (possibly stale) entries.
            input_rotation_valid = not rotations or [value.strip() for value in rotations] == ["0"]
            window_rotation_valid = (
                not window_rotations
                or [value.strip() for value in window_rotations] == ["ROTATION_0"])
            if (not (rotations or window_rotations)
                    or not input_rotation_valid or not window_rotation_valid):
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
        except BaseException:
            self.last_capture_rect = None
            if self._pressed:
                self._input_fault = True
            raise

    def capture(self, max_y=None):
        self.last_capture_rect = None
        signature = self._signature()
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
        if signature != self._signature():
            raise RuntimeError("Capture calibration changed during capture")
        self.last_capture_rect = rect
        self._capture_signature = signature
        self._capture_time = time.monotonic()
        return result  # BGR, matching desktop backends.

    def is_window_active(self):
        try:
            self.validate()
            return True
        except Exception:
            return False

    def _map_point(self, x, y):
        return logical_to_native(x, y, native_size=self.native_size,
                                 crop=self.crop, logical_size=self.logical_size)

    def close(self):
        try:
            if self.enable_input:
                self.button_up()
        finally:
            self.last_capture_rect = None
            self._closed = True
