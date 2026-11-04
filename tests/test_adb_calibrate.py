"""Offline capture CLI checks: no device or input is contacted."""

from unittest.mock import Mock

import cv2
import numpy as np
import pytest

import adb_calibrate


def arguments(path):
    return ['--serial', 'explicit-device', '--package', 'com.example.game',
            '--native-size', '720', '1560', '--crop', '0', '0', '720', '1560',
            '--logical-size', '360', '780', '--output', str(path)]


@pytest.fixture
def backend(monkeypatch):
    instance = Mock()
    instance.capture.return_value = np.zeros((780, 360, 3), dtype=np.uint8)
    factory = Mock(return_value=instance)
    monkeypatch.setattr(adb_calibrate, 'ADBBackend', factory)
    return factory, instance


def test_capture_and_preview(tmp_path, backend, capsys):
    factory, instance = backend
    output = tmp_path / 'capture.png'
    assert adb_calibrate.main(arguments(output)) == 0
    factory.assert_called_once_with(serial='explicit-device', package='com.example.game',
                                    native_size=[720, 1560], crop=[0, 0, 720, 1560],
                                    logical_size=[360, 780])
    instance.capture.assert_called_once_with()
    instance.close.assert_called_once_with()
    assert cv2.imread(str(output)).shape == (780, 360, 3)
    preview = cv2.imread(str(tmp_path / 'capture.annotated.png'))
    assert preview.any()
    assert 'Capture elapsed:' in capsys.readouterr().out


@pytest.mark.parametrize('sibling', [False, True])
def test_collision_never_captures_or_overwrites(tmp_path, backend, sibling):
    output = tmp_path / 'capture.png'
    existing = tmp_path / 'capture.annotated.png' if sibling else output
    existing.write_bytes(b'keep')
    with pytest.raises(SystemExit):
        adb_calibrate.main(arguments(output))
    assert existing.read_bytes() == b'keep'
    backend[0].assert_not_called()
    if sibling:
        assert not output.exists()


def test_failure_cleans_outputs_and_closes(tmp_path, backend):
    backend[1].capture.side_effect = RuntimeError('focus rejected')
    with pytest.raises(SystemExit):
        adb_calibrate.main(arguments(tmp_path / 'capture.png'))
    assert list(tmp_path.iterdir()) == []
    backend[1].close.assert_called_once_with()


@pytest.mark.parametrize('sibling', [False, True])
def test_replaced_reservation_does_not_follow_or_remove_symlink(tmp_path, backend, sibling):
    output = tmp_path / 'capture.png'
    preview = tmp_path / 'capture.annotated.png'
    replaced = preview if sibling else output
    unrelated = tmp_path / 'unrelated.txt'
    unrelated.write_bytes(b'leave unchanged')

    def capture():
        replaced.unlink()
        replaced.symlink_to(unrelated)
        return np.zeros((780, 360, 3), dtype=np.uint8)

    backend[1].capture.side_effect = capture
    with pytest.raises(SystemExit):
        adb_calibrate.main(arguments(output))
    assert unrelated.read_bytes() == b'leave unchanged'
    assert replaced.is_symlink()
    assert not (output if sibling else preview).exists()
    backend[1].close.assert_called_once_with()


@pytest.mark.parametrize('flag,count', [('--serial', 1), ('--package', 1),
    ('--native-size', 2), ('--crop', 4), ('--logical-size', 2), ('--output', 1)])
def test_all_calibration_arguments_required(tmp_path, backend, flag, count):
    args = arguments(tmp_path / 'capture.png')
    index = args.index(flag)
    del args[index:index + count + 1]
    with pytest.raises(SystemExit):
        adb_calibrate.main(args)
    backend[0].assert_not_called()
