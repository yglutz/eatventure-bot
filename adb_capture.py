"""Read-only, serial-bound Android capture. No Android input belongs here.

ADB coordinates are virtual canonical coordinates, not Windows desktop points.
This is a capture/guard interface, NOT a replacement for MouseController's HWND.
Unknown dumpsys formats deliberately fail closed. Native-size changes latch a
failure until a new capture instance is created after operator recalibration.
"""

import logging
import math
import re
import subprocess
from collections.abc import Sequence

import cv2
import numpy as np

import config

logger = logging.getLogger(__name__)


class ADBError(RuntimeError):
    """ADB execution, state or capture failure."""


class ADBTransport:
    """Bounded binary subprocess transport with a strict read-only allowlist.

    ``connect=True`` is the sole opt-in transport-changing operation; it connects
    the explicitly supplied WiFi endpoint, never selects the first device.
    ``run`` returns stdout bytes and rejects arbitrary shell commands.
    """

    READ_ONLY = frozenset({
        ("get-state",),
        ("devices", "-l"),
        ("exec-out", "screencap", "-p"),
        ("shell", "dumpsys", "window"),
        ("shell", "dumpsys", "window", "policy"),
        ("shell", "dumpsys", "power"),
    })

    def __init__(self, serial: str, *, adb_path: str = "adb",
                 timeout: float = 10.0, connect: bool = False):
        if not isinstance(serial, str) or not serial or serial.startswith("-") or any(c.isspace() for c in serial):
            raise ValueError("An explicit, nonempty ADB serial is required")
        if not isinstance(adb_path, str) or not adb_path.strip():
            raise ValueError("adb_path must be nonempty")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        self.serial = serial
        self.adb_path = adb_path
        self.timeout = timeout
        if connect:
            if not re.fullmatch(r"(?:[A-Za-z0-9_.-]+|\[[0-9a-fA-F:]+\]):[0-9]+", serial):
                raise ValueError("WiFi connect requires an explicit host:port serial")
            result = self._execute([adb_path, "connect", serial]).decode("utf-8", errors="replace").strip()
            if result not in (f"connected to {serial}", f"already connected to {serial}"):
                raise ADBError(f"ADB connect did not confirm requested endpoint: {result}")
            if self.run(["get-state"]).strip() != b"device":
                raise ADBError("Connected device is not authorized/online")

    def _execute(self, command: list[str]) -> bytes:
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=self.timeout,
                                    check=False, shell=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("ADB execution failed: %s", exc)
            raise ADBError(f"ADB execution failed: {exc}") from exc
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", errors="replace").strip()
            raise ADBError(f"ADB exited {result.returncode}: {message}")
        return result.stdout

    def run(self, args: Sequence[str]) -> bytes:
        if isinstance(args, (str, bytes)) or not isinstance(args, Sequence):
            raise ValueError("args must be a sequence of read-only command tokens")
        tokens = tuple(args)
        if any(not isinstance(token, str) for token in tokens) or tokens not in self.READ_ONLY:
            raise ValueError("ADB command is not on the read-only allowlist")
        return self._execute([self.adb_path, "-s", self.serial, *tokens])


class ADBCapture:
    """WindowCapture-shaped BGR capture with fresh foreground/unlock guards.

    ``calibrated_size`` and ``get_window_rect`` describe the canonical image;
    ``native_size`` pins the first successful screenshot's (width, height).
    ``capture`` raises on failure, whereas boolean guards return False.
    Construction does not contact a device unless ``connect=True``.
    """

    def __init__(self, serial: str, expected_package: str, *,
                 adb_path: str = "adb", timeout: float = 10.0,
                 connect: bool = False):
        if not isinstance(expected_package, str) or not re.fullmatch(
                r"[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+", expected_package, flags=re.ASCII):
            raise ValueError("An explicit Android expected_package is required")
        self.transport = ADBTransport(serial, adb_path=adb_path, timeout=timeout, connect=connect)
        self.expected_package = expected_package
        self.target_width = int(config.WINDOW_WIDTH)
        self.target_height = int(config.WINDOW_HEIGHT)
        if self.target_width <= 0 or self.target_height <= 0:
            raise ValueError("Canonical dimensions must be positive")
        self.calibrated_size = (self.target_width, self.target_height)
        self.native_size = None
        self.last_capture_ok = False
        self._size_changed = False

    def get_window_rect(self):
        """Virtual image origin and canonical width/height (not a desktop rect)."""
        return (0, 0, *self.calibrated_size)

    def calibration_identity(self):
        if self.native_size is None:
            raise ADBError('Capture required before binding calibration')
        return {'backend': 'adb', 'serial': self.transport.serial,
                'package': self.expected_package, 'native_size': list(self.native_size)}

    def is_window_active(self):
        try:
            window = self.transport.run(["shell", "dumpsys", "window"]).decode("utf-8", errors="strict")
            # CurrentFocus identifies the actual input recipient, not merely a
            # resumed activity behind a system dialog or notification shade.
            focuses = re.findall(r"mCurrentFocus\s*=\s*([^\r\n]+)", window)
            if not focuses or any(not re.search(
                    r"(?:^|\s)" + re.escape(self.expected_package) + r"/[A-Za-z0-9_.$]+(?=[\s}])", focus)
                    for focus in focuses):
                self.last_capture_ok = False
                return False
            policy = self.transport.run(["shell", "dumpsys", "window", "policy"]).decode("utf-8", errors="strict")
            lock_flags = re.findall(
                r"\b(?:mShowingLockscreen|mKeyguardShowing|isStatusBarKeyguard|keyguardShowing)\s*=\s*(\w+)", policy)
            # AOSP KeyguardServiceDelegate dumps its own indented showing flag.
            delegate = re.search(r"KeyguardServiceDelegate[^\r\n]*\r?\n((?:[ \t]+[^\r\n]*\r?\n?)*)", policy)
            if delegate:
                lock_flags += re.findall(r"\bshowing\s*=\s*(\w+)", delegate.group(1))
            power = self.transport.run(["shell", "dumpsys", "power"]).decode("utf-8", errors="strict")
            wake_states = re.findall(r"\bmWakefulness\s*=\s*(\w+)", power)
            active = bool(lock_flags and all(flag == "false" for flag in lock_flags)
                          and wake_states and all(state == "Awake" for state in wake_states)
                          and not self._size_changed)
        except (ADBError, UnicodeError):
            active = False
        if not active:
            self.last_capture_ok = False
        return active

    def capture(self, max_y=None):
        self.last_capture_ok = False
        if max_y is not None and (isinstance(max_y, bool) or not isinstance(max_y, int) or max_y <= 0):
            raise ValueError("max_y must be a positive integer or None")
        if self._size_changed or not self.is_window_active():
            raise ADBError("Device not awake/unlocked with expected package focused, or size changed")
        raw = self.transport.run(["exec-out", "screencap", "-p"])
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ADBError("Screenshot is not a PNG")
        try:
            image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None or image.size == 0:
                raise ADBError("PNG screenshot could not be decoded")
            size = (image.shape[1], image.shape[0])
            if self.native_size is not None and size != self.native_size:
                self._size_changed = True
                raise ADBError("Native screenshot size changed; operator recalibration required")
            if image.max() == image.min():
                raise ADBError("Empty/uniform screenshot")
            image = cv2.resize(image, self.calibrated_size, interpolation=cv2.INTER_AREA)
        except cv2.error as exc:
            raise ADBError(f"Screenshot decoding/resizing failed: {exc}") from exc
        # Reject focus/lock changes during capture too; never return a stale frame.
        if not self.is_window_active():
            raise ADBError("Foreground/unlock state changed during capture")
        self.native_size = size
        self.last_capture_ok = True
        return np.ascontiguousarray(image[:max_y] if max_y is not None else image)

    def input_ready(self):
        """Only a capture guard; does not authorize/bypass MouseController safety."""
        try:
            self.capture()
            return self.last_capture_ok
        except (RuntimeError, OSError, ValueError):
            self.last_capture_ok = False
            return False
