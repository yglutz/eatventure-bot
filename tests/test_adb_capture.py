"""All device interactions are mocked; these tests never contact Android."""

import subprocess
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

import config
from adb_capture import ADBCapture, ADBError, ADBTransport

SERIAL = "192.0.2.10:5555"
PACKAGE = "com.example.game"
WINDOW = b"mCurrentFocus=Window{123 u0 com.example.game/.MainActivity}\n"
POLICY = b"mShowingLockscreen=false\nmKeyguardShowing=false\n"
POWER = b"mWakefulness=Awake\n"


def png(width=8, height=12):
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :width // 2] = (255, 0, 0)  # BGR blue, not RGB blue.
    image[:, width // 2:] = (0, 0, 255)
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


@pytest.fixture
def adb(monkeypatch):
    outputs = {
        ("get-state",): b"device\n",
        ("shell", "dumpsys", "window"): WINDOW,
        ("shell", "dumpsys", "window", "policy"): POLICY,
        ("shell", "dumpsys", "power"): POWER,
        ("exec-out", "screencap", "-p"): png(),
    }

    def execute(command, **kwargs):
        assert command[:3] == ["adb", "-s", SERIAL]
        assert kwargs == dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=10.0, check=False, shell=False)
        value = outputs[tuple(command[3:])]
        if isinstance(value, Exception):
            raise value
        return subprocess.CompletedProcess(command, 0, stdout=value, stderr=b"")

    mock = Mock(side_effect=execute)
    monkeypatch.setattr(subprocess, "run", mock)
    return outputs, mock


def test_binary_capture_canonical_bgr_crop_and_interface(adb):
    capture = ADBCapture(SERIAL, PACKAGE)
    assert not capture.last_capture_ok
    assert capture.native_size is None
    assert capture.calibrated_size == (int(config.WINDOW_WIDTH), int(config.WINDOW_HEIGHT))
    assert capture.get_window_rect() == (0, 0, *capture.calibrated_size)
    frame = capture.capture()
    assert frame.shape == (int(config.WINDOW_HEIGHT), int(config.WINDOW_WIDTH), 3)
    assert frame.dtype == np.uint8 and frame.flags.c_contiguous
    assert tuple(frame[0, 0]) == (255, 0, 0)
    assert capture.native_size == (8, 12)
    assert capture.last_capture_ok
    assert capture.capture(max_y=5).shape == (5, int(config.WINDOW_WIDTH), 3)
    assert capture.input_ready()


def test_native_size_change_is_latched(adb):
    outputs, mock = adb
    capture = ADBCapture(SERIAL, PACKAGE)
    capture.capture()
    outputs[("exec-out", "screencap", "-p")] = png(12, 8)
    with pytest.raises(ADBError, match="size changed"):
        capture.capture()
    assert capture.native_size == (8, 12)
    assert not capture.last_capture_ok
    outputs[("exec-out", "screencap", "-p")] = png()
    mock.reset_mock()
    assert not capture.input_ready()
    mock.assert_not_called()


@pytest.mark.parametrize("key,value", [
    (("shell", "dumpsys", "window"), b"mCurrentFocus=null\n"),
    (("shell", "dumpsys", "window"), WINDOW.replace(b"com.example.game", b"com.example.game.other")),
    (("shell", "dumpsys", "window"), WINDOW.replace(b"com.example.game", b"evil.com.example.game")),
    (("shell", "dumpsys", "window"), b"mFocusedApp=com.example.game/.MainActivity\n"),
    (("shell", "dumpsys", "window"), WINDOW + b"mCurrentFocus=Window{0 u0 com.android.systemui/.Dialog}\n"),
    (("shell", "dumpsys", "window", "policy"), b"mShowingLockscreen=true\n"),
    (("shell", "dumpsys", "window", "policy"), b"unknown format"),
    (("shell", "dumpsys", "window", "policy"), b"mShowingLockscreen=false\nmKeyguardShowing=true\n"),
    (("shell", "dumpsys", "window", "policy"), b"mShowingLockscreen=unknown\n"),
    (("shell", "dumpsys", "power"), b"mWakefulness=Asleep\n"),
    (("shell", "dumpsys", "power"), b"unknown format"),
    (("shell", "dumpsys", "window"), b"\xff"),
    (("shell", "dumpsys", "window"), subprocess.TimeoutExpired("adb", 10)),
])
def test_focus_unlock_unknown_states_fail_closed(adb, key, value):
    outputs, _ = adb
    capture = ADBCapture(SERIAL, PACKAGE)
    capture.capture()
    outputs[key] = value
    assert not capture.is_window_active()
    assert not capture.last_capture_ok
    assert not capture.input_ready()
    with pytest.raises(ADBError):
        capture.capture()


def test_aosp_delegate_unlock_and_conflicting_lock_flag(adb):
    outputs, _ = adb
    outputs[("shell", "dumpsys", "window", "policy")] = b"KeyguardServiceDelegate\n  showing=false\n  secure=true\n"
    capture = ADBCapture(SERIAL, PACKAGE)
    assert capture.is_window_active()  # Secure password configured is not locked.
    outputs[("shell", "dumpsys", "window", "policy")] += b"  mKeyguardShowing=true\n"
    assert not capture.is_window_active()


@pytest.mark.parametrize("data", [b"", b"not png", b"\x89PNG\r\n\x1a\ncorrupt"])
def test_bad_screenshot_clears_success(adb, data):
    outputs, _ = adb
    capture = ADBCapture(SERIAL, PACKAGE)
    capture.capture()
    outputs[("exec-out", "screencap", "-p")] = data
    with pytest.raises(ADBError):
        capture.capture()
    assert not capture.last_capture_ok


def test_uniform_screenshot_not_pinned(adb):
    outputs, _ = adb
    ok, encoded = cv2.imencode(".png", np.zeros((12, 8, 3), dtype=np.uint8))
    assert ok
    outputs[("exec-out", "screencap", "-p")] = encoded.tobytes()
    capture = ADBCapture(SERIAL, PACKAGE)
    assert not capture.input_ready()
    assert capture.native_size is None


def test_screenshot_timeout_clears_previous_success(adb):
    outputs, _ = adb
    capture = ADBCapture(SERIAL, PACKAGE)
    capture.capture()
    outputs[("exec-out", "screencap", "-p")] = subprocess.TimeoutExpired("adb", 10)
    with pytest.raises(ADBError):
        capture.capture()
    assert not capture.last_capture_ok
    assert not capture.input_ready()


def test_opencv_error_is_wrapped(adb, monkeypatch):
    capture = ADBCapture(SERIAL, PACKAGE)
    monkeypatch.setattr(cv2, "imdecode", Mock(side_effect=cv2.error("decode failure")))
    with pytest.raises(ADBError, match="decoding/resizing failed"):
        capture.capture()
    assert not capture.last_capture_ok


def test_focus_loss_during_capture(adb):
    outputs, mock = adb
    original = mock.side_effect

    def lose_focus(command, **kwargs):
        result = original(command, **kwargs)
        if command[3:] == ["exec-out", "screencap", "-p"]:
            outputs[("shell", "dumpsys", "window")] = b"mCurrentFocus=null"
        return result

    mock.side_effect = lose_focus
    capture = ADBCapture(SERIAL, PACKAGE)
    with pytest.raises(ADBError, match="changed during capture"):
        capture.capture()
    assert not capture.last_capture_ok and capture.native_size is None


@pytest.mark.parametrize("error", [FileNotFoundError("adb missing"),
                                      OSError("OS failure"),
                                      subprocess.TimeoutExpired("adb", 1)])
def test_transport_errors_wrapped(monkeypatch, error):
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=error))
    with pytest.raises(ADBError):
        ADBTransport(SERIAL).run(["get-state"])


def test_nonzero_exit_includes_stderr(monkeypatch):
    monkeypatch.setattr(subprocess, "run", Mock(return_value=subprocess.CompletedProcess(
        [], 1, b"", b"device unauthorized")))
    with pytest.raises(ADBError, match="unauthorized"):
        ADBTransport(SERIAL).run(["get-state"])
    assert not ADBCapture(SERIAL, PACKAGE).input_ready()


@pytest.mark.parametrize("args", ["shell dumpsys window", ["shell", "input", "tap", "1", "1"],
                                  ["shell", "am", "start", "com.example.game"],
                                  ["shell", "dumpsys", "window; reboot"],
                                  ["reboot"], ["connect", SERIAL], [1], None])
def test_transport_rejects_non_readonly_commands(adb, args):
    _, mock = adb
    with pytest.raises(ValueError):
        ADBTransport(SERIAL).run(args)
    mock.assert_not_called()


def test_optional_connect_is_explicit_and_checks_authorization(monkeypatch):
    mock = Mock(side_effect=[subprocess.CompletedProcess([], 0, f"connected to {SERIAL}\n".encode(), b""),
                             subprocess.CompletedProcess([], 0, b"device\n", b"")])
    monkeypatch.setattr(subprocess, "run", mock)
    ADBCapture(SERIAL, PACKAGE, connect=True, adb_path="C:/tools/adb.exe", timeout=2)
    assert mock.call_args_list[0].args[0] == ["C:/tools/adb.exe", "connect", SERIAL]
    assert mock.call_args_list[1].args[0] == ["C:/tools/adb.exe", "-s", SERIAL, "get-state"]
    assert mock.call_args.kwargs["timeout"] == 2


@pytest.mark.parametrize("reply,state", [(b"failed to connect", b"device"),
                                          (f"connected to {SERIAL}".encode(), b"offline"),
                                          (b"connected to another-device", b"device")])
def test_connect_failure_even_with_zero_exit(monkeypatch, reply, state):
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=[
        subprocess.CompletedProcess([], 0, reply, b""),
        subprocess.CompletedProcess([], 0, state, b"")]))
    with pytest.raises(ADBError):
        ADBTransport(SERIAL, connect=True)


@pytest.mark.parametrize("kwargs", [dict(serial=""), dict(serial=None), dict(serial="-d"),
                                    dict(serial="two devices"), dict(timeout=0),
                                    dict(timeout=float("nan")), dict(timeout=True),
                                    dict(adb_path=""), dict(serial="USB123", connect=True)])
def test_transport_validation_no_subprocess(adb, kwargs):
    _, mock = adb
    options = dict(serial=SERIAL)
    options.update(kwargs)
    with pytest.raises(ValueError):
        ADBTransport(**options)
    mock.assert_not_called()


@pytest.mark.parametrize("package", [None, "", "com.game;reboot", "com.game/.Activity"])
def test_package_required(adb, package):
    _, mock = adb
    with pytest.raises(ValueError):
        ADBCapture(SERIAL, package)
    mock.assert_not_called()


@pytest.mark.parametrize("max_y", [0, -1, True, 1.5])
def test_invalid_crop_fails_closed(adb, max_y):
    capture = ADBCapture(SERIAL, PACKAGE)
    capture.capture()
    with pytest.raises(ValueError):
        capture.capture(max_y=max_y)
    assert not capture.last_capture_ok


def test_readonly_devices_and_no_implicit_connection(adb):
    outputs, mock = adb
    outputs[("devices", "-l")] = b"List of devices attached\n192.0.2.10:5555 device\n"
    capture = ADBCapture(SERIAL, PACKAGE)
    mock.assert_not_called()
    assert b"device" in capture.transport.run(["devices", "-l"])
    assert capture.transport.run(["get-state"]) == b"device\n"
