"""scrcpy-X11 calibration contracts; no devices, X11, input or OCR models."""
from copy import deepcopy
import json
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

import learning.calibration as module
from learning.core import SIZE


SOURCE = {'backend': 'scrcpy-x11', 'serial': '192.0.2.1:5555',
          'package': 'approved.game', 'display_name': ':99', 'window_id': 123,
          'canonical_size': [360, 780], 'native_size': [360, 780],
          'coordinate_system': 'canonical-client-pixels', 'video_size': None}
GEMS = [230, 40, 42, 20]
INTERACTIONS = [[70, 285, 210, 295]]
ANIMATION = [[20, 80, 320, 541]]


class FakeCapture:
    def __init__(self, image):
        self.image = image
        self.identity = deepcopy(SOURCE)
        self.captures = 0
        self.identity_calls = 0
        self.last_capture_ok = False
        self.active = True

    def is_window_active(self):
        return self.active

    def capture(self):
        self.captures += 1
        self.last_capture_ok = False
        if isinstance(self.image, Exception):
            raise self.image
        self.last_capture_ok = True
        return self.image

    def calibration_identity(self):
        self.identity_calls += 1
        if not self.last_capture_ok or not self.active:
            raise RuntimeError('Successful capture required before calibration')
        assert self.identity_calls == self.captures
        return self.identity


@pytest.fixture
def setup(tmp_path, monkeypatch):
    image = np.full((SIZE[1], SIZE[0], 3), 100, np.uint8)
    capture = FakeCapture(image)
    vision = Mock()
    vision.supported.side_effect = lambda frame: bool(frame.any())
    vision.gems.return_value = (301, .99)
    monkeypatch.setattr(module, 'Vision', Mock(return_value=vision))
    monkeypatch.setattr(module, 'build_animation_profile',
                        Mock(return_value={'version': 1, 'test_profile': True}))
    references = []
    for i in range(8):
        path = tmp_path / f'reference-{i}.png'
        assert cv2.imwrite(str(path), image)
        references.append(path)
    negative = tmp_path / 'negative.png'
    assert cv2.imwrite(str(negative), np.zeros_like(image))
    path = tmp_path / 'new' / 'calibration.json'
    return capture, vision, references, negative, path


def create(setup):
    capture, _, references, _, path = setup
    return module.create_animation_calibration(capture, path, references,
                                              GEMS, INTERACTIONS, ANIMATION)


def verify(setup, approve=False):
    capture, _, _, negative, path = setup
    return module.verify_calibration(capture, path, checks=10,
                                     negative_paths=[negative], approve=approve,
                                     sleep_seconds=0)


def test_create_and_approve_pins_full_source_after_each_capture(setup):
    capture, _, _, _, path = setup
    data = create(setup)
    assert data['source'] == SOURCE
    assert capture.captures == capture.identity_calls == 1
    assert not data['ocr_verified'] and not data['readiness_verified']
    assert verify(setup, approve=True)
    saved = json.loads(path.read_text())
    assert saved['ocr_verified'] and saved['readiness_verified']
    assert saved['verification_report']['source'] == SOURCE
    assert capture.captures == capture.identity_calls == 11
    before = path.read_bytes()
    assert verify(setup)
    assert path.read_bytes() == before
    # The stored identity is a snapshot, not an alias to capture state.
    capture.identity['native_size'][0] = 1
    assert data['source'] == SOURCE
    assert json.loads(path.read_text())['source'] == SOURCE


MUTATIONS = [('backend', 'adb'), ('serial', '192.0.2.2:5555'),
             ('package', 'other.game'), ('display_name', ':100'),
             ('window_id', 124), ('canonical_size', [361, 780]),
             ('native_size', [360, 781]), ('coordinate_system', 'video-pixels'),
             ('video_size', [360, 780])]


@pytest.mark.parametrize('field,value', MUTATIONS)
def test_every_changed_identity_field_rejects_future_verification(setup, field, value):
    capture, vision, _, _, path = setup
    create(setup)
    assert verify(setup, approve=True)
    before = path.read_bytes()
    capture.identity[field] = value
    vision.gems.reset_mock()
    assert not verify(setup)
    assert path.read_bytes() == before
    vision.gems.assert_not_called()
    assert not verify(setup, approve=True)
    saved = json.loads(path.read_text())
    assert not saved['ocr_verified'] and not saved['readiness_verified']


INVALID = [('backend', None), ('backend', 'unknown'),
           ('serial', ''), ('serial', ' '), ('serial', 123),
           ('package', None), ('package', ' '), ('package', 123),
           ('display_name', ''), ('display_name', ' '), ('display_name', 99),
           ('window_id', 0), ('window_id', -1), ('window_id', True),
           ('window_id', 123.), ('window_id', '123'),
           ('canonical_size', None), ('canonical_size', (360, 780)),
           ('canonical_size', [360., 780]), ('canonical_size', [True, 780]),
           ('canonical_size', [360]), ('canonical_size', [360, 780, 1]),
           ('canonical_size', [0, 780]), ('canonical_size', [361, 780]),
           ('native_size', None), ('native_size', (360, 780)),
           ('native_size', [360, 780.]), ('native_size', [360, True]),
           ('native_size', [360]), ('native_size', [-360, 780]),
           ('native_size', [1080, 2340]),
           ('coordinate_system', None), ('coordinate_system', 'video-pixels'),
           ('video_size', []), ('video_size', False), ('video_size', 'None')]


@pytest.mark.parametrize('field,value', INVALID)
def test_invalid_fields_are_not_coerced_or_written(setup, field, value):
    capture, _, _, _, path = setup
    capture.identity[field] = value
    with pytest.raises(ValueError):
        create(setup)
    assert capture.captures == capture.identity_calls == 1
    assert not path.parent.exists()


@pytest.mark.parametrize('field', list(SOURCE))
def test_missing_fields_rejected(setup, field):
    capture, _, _, _, path = setup
    del capture.identity[field]
    with pytest.raises(ValueError):
        create(setup)
    assert not path.parent.exists()


def test_unknown_field_rejected(setup):
    capture, _, _, _, path = setup
    capture.identity['future_field'] = None
    with pytest.raises(ValueError):
        create(setup)
    assert not path.parent.exists()


@pytest.mark.parametrize('failure', ['error', 'none', 'cropped', 'dtype', 'focus'])
def test_creation_requires_successful_full_focused_capture(setup, failure):
    capture, _, _, _, path = setup
    if failure == 'focus':
        capture.active = False
    else:
        capture.image = {'error': RuntimeError('capture failed'), 'none': None,
                         'cropped': capture.image[:-1],
                         'dtype': capture.image.astype(np.float32)}[failure]
    with pytest.raises((RuntimeError, ValueError)):
        create(setup)
    assert capture.identity_calls == 0
    assert not path.parent.exists()


def test_failed_verification_captures_never_bind_identity(setup):
    capture, vision, _, _, _ = setup
    create(setup)
    capture.image = None
    vision.gems.reset_mock()
    assert not verify(setup, approve=True)
    assert capture.identity_calls == 1
    vision.gems.assert_not_called()


def test_restarted_window_mid_verification_requires_new_calibration(setup):
    capture, vision, _, _, path = setup
    create(setup)
    original = capture.capture

    def restarted():
        image = original()
        if capture.captures == 11:
            capture.identity['window_id'] += 1
        return image

    capture.capture = restarted
    vision.gems.reset_mock()
    assert not verify(setup, approve=True)
    assert vision.gems.call_count == 9
    saved = json.loads(path.read_text())
    assert saved['source']['window_id'] == SOURCE['window_id']
    assert not saved['readiness_verified']


def test_identity_without_successful_capture_rejected(setup):
    capture, _, _, _, _ = setup
    with pytest.raises(RuntimeError, match='Successful capture'):
        module._source(capture)
