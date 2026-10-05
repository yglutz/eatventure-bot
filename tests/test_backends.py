"""Backend/CLI contracts without desktop input, OCR models, or an Android device."""

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from learning import cli
from learning.core import POINTS, SCROLLS, SIZE, WAIT, fingerprint
from learning.environment import Controls, LiveInterface


def test_simulated_cli_help_without_windows_imports():
    # A fresh interpreter catches eager platform imports hidden by pytest's cache.
    # Raising AssertionError also prevents optional-import fallbacks hiding a leak.
    script = """
import importlib.abc
import runpy
import sys

class BlockDesktop(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split('.')[0]
        if root.startswith('win32') or root in {'pynput', 'pythoncom', 'pywintypes'}:
            raise AssertionError('Desktop import attempted: ' + fullname)

sys.meta_path.insert(0, BlockDesktop())
sys.argv = ['main.py', 'train', '--simulate', '--help']
runpy.run_path('main.py', run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, '-c', script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'usage:' in result.stdout
    assert '--simulate' in result.stdout
    assert '--backend {window,adb}' in result.stdout
    assert 'Desktop import attempted' not in result.stderr


@pytest.mark.parametrize(
    'arguments, message',
    [
        ([], 'ADB requires --adb-serial and --adb-package'),
        (['--adb-serial', 'test-device'], 'ADB requires --adb-serial and --adb-package'),
        (['--adb-package', 'com.example.game'], 'ADB requires --adb-serial and --adb-package'),
        (['--adb-serial', 'test-device', '--adb-package', 'com.example.game'],
         'Headless ADB calibration requires --gems-roi and --interaction-roi'),
        (['--adb-serial', 'test-device', '--adb-package', 'com.example.game',
          '--gems-roi', '220', '30', '60', '25'],
         'Headless ADB calibration requires --gems-roi and --interaction-roi'),
        (['--adb-serial', 'test-device', '--adb-package', 'com.example.game',
          '--interaction-roi', '70', '200', '200', '300'],
         'Headless ADB calibration requires --gems-roi and --interaction-roi'),
    ],
)
def test_adb_cli_rejects_incomplete_calibration_before_device_calls(
    arguments, message, monkeypatch, capsys,
):
    constructor = Mock(side_effect=AssertionError('Device must not be constructed'))
    device_call = Mock(side_effect=AssertionError('Device must not be contacted'))
    monkeypatch.setitem(sys.modules, 'adb_capture', SimpleNamespace(ADBCapture=constructor))
    monkeypatch.setattr(subprocess, 'run', device_call)
    with pytest.raises(SystemExit) as exc:
        cli.main(['calibrate', '--backend', 'adb', '--adb-connect', *arguments])
    assert exc.value.code == 2
    assert message in capsys.readouterr().err
    constructor.assert_not_called()
    device_call.assert_not_called()


def test_live_interface_tap_only_capabilities_mask_holds_and_drags():
    capture = SimpleNamespace(is_window_active=lambda: True)
    mouse = SimpleNamespace(
        supports_action=lambda kind: kind == 'click', release=Mock(),
    )
    vision = SimpleNamespace(path_supported=lambda start, end: True)
    interface = LiveInterface(capture, mouse, vision, Controls(paused=False))
    mask = interface.mask()
    assert mask.dtype == np.bool_
    assert mask.shape == (WAIT + 1,)
    assert len(SCROLLS) > 0
    assert mask[:len(POINTS)].all()
    assert not mask[len(POINTS):2 * len(POINTS)].any()
    assert not mask[2 * len(POINTS):WAIT].any()
    assert mask[WAIT]


def test_live_interface_capabilities_do_not_override_vision_restrictions():
    blocked = POINTS[0]
    capture = SimpleNamespace(is_window_active=lambda: True)
    mouse = SimpleNamespace(supports_action=lambda kind: kind == 'click', release=Mock())
    vision = SimpleNamespace(
        path_supported=lambda start, end: start != blocked and end != blocked,
    )
    interface = LiveInterface(capture, mouse, vision, Controls(paused=False))
    mask = interface.mask()
    assert not mask[0]
    assert mask[1:len(POINTS)].all()
    assert not mask[len(POINTS):WAIT].any()
    assert mask[WAIT]


def test_console_controls_pause_resume_generation_release_and_stop(monkeypatch):
    clock = Mock(return_value=100.)
    monkeypatch.setattr('learning.environment.time.monotonic', clock)
    controls = Controls(paused=False)
    release = Mock()
    controls.release = release

    controls.command('unknown')
    assert not controls.paused and not controls.stopped
    assert controls.generation == 0
    release.assert_not_called()

    controls.command(' Z\n')
    assert controls.paused and controls.pause_start == 100.
    assert controls.generation == 1
    release.assert_called_once_with()
    controls.pause()  # Repeated pause must not reset the baseline again.
    assert controls.generation == 1
    assert release.call_count == 1

    clock.return_value = 107.
    controls.command('z')
    assert not controls.paused and controls.pause_start is None
    assert controls.pause_seconds == 7.
    assert controls.active_time() == 100.
    assert controls.generation == 1
    assert release.call_count == 1

    clock.return_value = 110.
    controls.command('z')
    assert controls.paused and controls.generation == 2
    assert release.call_count == 2
    controls.command(' P\n')
    assert controls.stopped and controls.stop_reason == 'operator stop'
    assert release.call_count == 3
    with pytest.raises(KeyboardInterrupt):
        controls.wait()
    controls.close()
    assert release.call_count == 4


@pytest.mark.parametrize(
    'old_source, new_source',
    [
        (None, {'backend': 'adb', 'serial': 'device-a'}),
        ({'backend': 'adb', 'serial': 'device-a'}, {'backend': 'window'}),
        ({'backend': 'adb', 'serial': 'device-a'}, {'backend': 'adb', 'serial': 'device-b'}),
    ],
)
def test_calibrate_rejects_source_mismatch_without_overwriting(
    old_source, new_source, tmp_path, monkeypatch,
):
    from learning import vision as module

    path = tmp_path / 'calibration.json'
    data = {'config': fingerprint(), 'views': ['existing.png'], 'ocr_verified': True}
    if old_source is not None:
        data['source'] = old_source
    path.write_text(json.dumps(data), encoding='utf-8')
    original = path.read_bytes()
    capture = SimpleNamespace(
        capture=Mock(return_value=np.zeros((SIZE[1], SIZE[0], 3), np.uint8)),
        calibration_identity=lambda: new_source,
    )
    engine = Mock()
    save_image = Mock()
    monkeypatch.setattr(module, 'Vision', engine)
    monkeypatch.setattr(module.cv2, 'imwrite', save_image)
    with pytest.raises(ValueError, match='Capture source changed'):
        module.calibrate(capture, path, [220, 30, 60, 25], [[70, 200, 200, 300]])
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]
    engine.assert_not_called()
    save_image.assert_not_called()


def test_calibrate_matching_source_preserves_views_and_verifies_mocked_ocr(tmp_path, monkeypatch):
    from learning import vision as module

    source = {'backend': 'adb', 'serial': 'device-a', 'package': 'com.example.game'}
    path = tmp_path / 'calibration.json'
    path.write_text(json.dumps({
        'source': source, 'config': fingerprint(), 'views': ['view-000.png'],
    }), encoding='utf-8')
    image = np.zeros((SIZE[1], SIZE[0], 3), np.uint8)
    capture = SimpleNamespace(capture=lambda: image, calibration_identity=lambda: source)
    engine = Mock()
    engine.gems.return_value = (123, .99)
    factory = Mock(return_value=engine)
    monkeypatch.setattr(module, 'Vision', factory)
    module.calibrate(capture, path, [220, 30, 60, 25], [[70, 200, 200, 300]])
    data = json.loads(path.read_text(encoding='utf-8'))
    assert data['source'] == source
    assert data['views'] == ['view-000.png', 'view-001.png']
    assert data['ocr_verified'] is True
    assert (tmp_path / 'view-001.png').is_file()
    factory.assert_called_once_with(path, calibration_check=True)
    engine.gems.assert_called_once()
    assert engine.gems.call_args.args[0] is image
