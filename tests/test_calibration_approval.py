"""No device, transport, mouse, or OCR model calls: lifecycle contract tests."""
import json
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

import learning.calibration as module
from learning.core import SIZE, fingerprint

GEMS = [230,40,42,20]
INTERACTIONS = [[70,285,210,295]]
ANIMATION = [[20,80,320,541]]
SOURCE = {'backend': 'adb', 'serial': 'approved-device',
          'package': 'approved.game', 'native_size': [1080,2400]}


@pytest.fixture
def image():
    return np.full((SIZE[1],SIZE[0],3), 100, np.uint8)


@pytest.fixture
def vision(monkeypatch):
    instance = Mock()
    instance.supported.return_value = True
    instance.gems.return_value = (301,.99)
    constructor = Mock(return_value=instance)
    monkeypatch.setattr(module, 'Vision', constructor)
    return instance


@pytest.fixture
def capture(image):
    instance = Mock()
    instance.capture.return_value = image
    instance.calibration_identity.return_value = SOURCE.copy()
    return instance


@pytest.fixture
def references(tmp_path, image, monkeypatch):
    paths = []
    for i in range(8):
        path = tmp_path/f'approved-{i}.png'
        assert cv2.imwrite(str(path), image)
        paths.append(path)
    builder = Mock(return_value={'version': 1, 'test_profile': True})
    monkeypatch.setattr(module, 'build_animation_profile', builder)
    return paths


def candidate(tmp_path, enabled=False, profile=True):
    path = tmp_path/'candidate.json'
    data = {'size': list(SIZE), 'config': fingerprint(), 'source': SOURCE.copy(),
            'gems_roi': GEMS, 'interaction_rois': INTERACTIONS,
            'views': ['view-000.png'], 'ocr_verified': enabled,
            'readiness_verified': enabled, 'operator_note': 'preserve this'}
    if profile:
        data['animation_profile'] = {'version': 1}
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def negative(tmp_path, image):
    path = tmp_path/'negative.png'
    assert cv2.imwrite(str(path), image)
    return path


def enable_check(capture, path, negative_path, **kwargs):
    return module.verify_calibration(capture,path,negative_paths=[negative_path],
                                     approve=True,sleep_seconds=0,**kwargs)


def test_creation_stays_disabled_and_binds_after_capture(tmp_path, references, capture, vision):
    path = tmp_path/'new-directory'/'calibration.json'
    def identity():
        assert capture.capture.call_count == 1
        return SOURCE
    capture.calibration_identity.side_effect = identity
    result = module.create_animation_calibration(capture,path,references,GEMS,INTERACTIONS,
                                                ANIMATION,cash_roi=[75,38,80,20])
    data = json.loads(path.read_text())
    assert result == data
    assert data['ocr_verified'] is False and data['readiness_verified'] is False
    assert data['creation_report']['passed'] is True
    assert data['source'] == SOURCE
    assert data['interaction_rois'] == INTERACTIONS
    assert data['views'] == [f'view-{i:03d}.png' for i in range(8)]
    assert all((path.parent/p).is_file() for p in data['views'])
    assert vision.gems.call_count == vision.supported.call_count == 8
    module.Vision.assert_called_once_with(path,calibration_check=True)
    assert module.build_animation_profile.call_args.kwargs['cash_roi'] == [75,38,80,20]


def test_creation_refuses_existing_directory_without_touching_old(tmp_path, references, capture, vision):
    old = candidate(tmp_path,enabled=True)
    original = old.read_bytes()
    with pytest.raises(FileExistsError):
        module.create_animation_calibration(capture,old,references,GEMS,INTERACTIONS,ANIMATION)
    with pytest.raises(FileExistsError):
        module.create_animation_calibration(capture,tmp_path/'another.json',references,GEMS,INTERACTIONS,ANIMATION)
    assert old.read_bytes() == original
    capture.capture.assert_not_called()


@pytest.mark.parametrize('roi', [[70,285,0,295], [70,285,291,295], [70,285,210,384],
                                [280,270,20,10], [60,280,10,10], [145,110,10,10],
                                [70,285,210.,295], [True,285,210,295]])
def test_creation_rejects_unsafe_inclusive_or_invalid_geometry(tmp_path,references,capture,vision,roi):
    path = tmp_path/'unsafe'/'calibration.json'
    with pytest.raises(ValueError):
        module.create_animation_calibration(capture,path,references,GEMS,[roi],ANIMATION)
    assert not path.parent.exists()


@pytest.mark.parametrize('kind', ['unsupported','low_confidence','unknown','no_triple','decrease'])
def test_creation_failed_validation_leaves_disabled_file(tmp_path,references,capture,vision,kind):
    if kind == 'unsupported':
        vision.supported.return_value = False
    elif kind == 'low_confidence':
        vision.gems.return_value = (301,.94)
    elif kind == 'unknown':
        vision.gems.return_value = (None,1.)
    elif kind == 'no_triple':
        vision.gems.side_effect = [(i,.99) for i in range(8)]
    else:
        vision.gems.side_effect = [(301,.99)]*3 + [(300,.99)]*5
    path = tmp_path/'failed-candidate'/'calibration.json'
    with pytest.raises(ValueError):
        module.create_animation_calibration(capture,path,references,GEMS,INTERACTIONS,ANIMATION)
    data = json.loads(path.read_text())
    assert data['ocr_verified'] is False and data['readiness_verified'] is False
    if kind == 'unsupported':
        vision.gems.assert_not_called()


def test_checked_write_and_fresh_capture_failure(tmp_path,references,capture,vision,monkeypatch):
    path = tmp_path/'capture-failed'/'calibration.json'
    capture.capture.side_effect = RuntimeError('capture failure')
    with pytest.raises(RuntimeError):
        module.create_animation_calibration(capture,path,references,GEMS,INTERACTIONS,ANIMATION)
    assert not path.parent.exists()
    capture.calibration_identity.assert_not_called()
    capture.capture.side_effect = None
    monkeypatch.setattr(module.cv2,'imwrite',Mock(return_value=False))
    path = tmp_path/'write-failed'/'calibration.json'
    with pytest.raises(RuntimeError,match='write approved reference'):
        module.create_animation_calibration(capture,path,references,GEMS,INTERACTIONS,ANIMATION)
    assert not path.exists()
    module.Vision.assert_not_called()


def test_creation_count_and_invalid_png_contract(tmp_path,references,capture,vision):
    path = tmp_path/'invalid-png'/'calibration.json'
    with pytest.raises(ValueError,match='8..32'):
        module.create_animation_calibration(capture,path,references[:7],GEMS,INTERACTIONS,ANIMATION)
    references[0].write_bytes(b'not a png')
    with pytest.raises(ValueError,match='signature'):
        module.create_animation_calibration(capture,path,references,GEMS,INTERACTIONS,ANIMATION)
    assert not path.parent.exists()


def test_creation_malformed_source_never_writes(tmp_path,references,capture,vision):
    capture.calibration_identity.return_value = {'backend':'adb','serial':'device'}
    path = tmp_path/'bad-source'/'calibration.json'
    with pytest.raises(ValueError):
        module.create_animation_calibration(capture,path,references,GEMS,INTERACTIONS,ANIMATION)
    assert not path.parent.exists()


def test_overlapping_safe_interaction_rectangles_are_preserved(tmp_path,references,capture,vision):
    rois = INTERACTIONS + [[100,300,100,100]]
    path = tmp_path/'overlap'/'calibration.json'
    result = module.create_animation_calibration(capture,path,references,GEMS,rois,ANIMATION)
    assert result['interaction_rois'] == rois


def test_successful_approval_preserves_metadata_and_records_counts(tmp_path,image,capture,vision):
    path = candidate(tmp_path,enabled=True)
    neg = negative(tmp_path,image)
    vision.supported.side_effect = [False]+[True]*20
    def sample():
        data = json.loads(path.read_text())
        assert data['ocr_verified'] is False and data['readiness_verified'] is False
        return image
    capture.capture.side_effect = sample
    assert enable_check(capture,path,neg)
    data = json.loads(path.read_text())
    assert data['ocr_verified'] is True and data['readiness_verified'] is True
    assert data['operator_note'] == 'preserve this'
    assert data['interaction_rois'] == INTERACTIONS
    report = data['verification_report']
    assert report['high_confidence'] == report['supported'] == report['sampled'] == 20
    assert report['required_high_confidence'] == 15
    assert report['confirmed_readings'] == 18
    assert report['source'] == SOURCE and report['profile_version'] == 1
    assert report['negatives_rejected'] == 1
    capture.calibration_identity.assert_called()


@pytest.mark.parametrize('enabled', [False,True])
def test_verify_only_is_byte_for_byte_read_only_without_negative(tmp_path,capture,vision,enabled):
    path = candidate(tmp_path,enabled=enabled)
    before = path.read_bytes()
    assert module.verify_calibration(capture,path,sleep_seconds=0)
    assert path.read_bytes() == before
    module.Vision.assert_called_once_with(path,calibration_check=True)


def test_approval_requires_explicit_negative_and_disables(tmp_path,capture,vision):
    path = candidate(tmp_path,enabled=True)
    assert not module.verify_calibration(capture,path,approve=True,sleep_seconds=0)
    data = json.loads(path.read_text())
    assert data['ocr_verified'] is False and data['readiness_verified'] is False
    capture.capture.assert_not_called()


def test_legacy_with_negative_still_cannot_be_approved(tmp_path,image,capture,vision):
    path = candidate(tmp_path,enabled=True,profile=False)
    before = path.read_bytes()
    assert not enable_check(capture,path,negative(tmp_path,image))
    assert path.read_bytes() == before
    vision.supported.assert_not_called()


def test_legacy_without_negative_is_also_never_mutated(tmp_path,capture,vision):
    path = candidate(tmp_path,enabled=True,profile=False)
    before = path.read_bytes()
    assert not module.verify_calibration(capture,path,approve=True,sleep_seconds=0)
    assert path.read_bytes() == before
    capture.capture.assert_not_called()


@pytest.mark.parametrize('good,passed', [(7,False),(8,True)])
def test_ceil_75_percent_supported_high_confidence(tmp_path,image,capture,vision,good,passed):
    path = candidate(tmp_path)
    neg = negative(tmp_path,image)
    vision.supported.side_effect = [False]+[True]*good+[False]*(10-good)
    assert enable_check(capture,path,neg,checks=10) is passed
    assert vision.gems.call_count == good  # unsupported NEVER goes to OCR
    report = json.loads(path.read_text())['verification_report']
    assert report['required_high_confidence'] == 8
    assert report['high_confidence'] == good


@pytest.mark.parametrize('problem', ['no_triple','decrease','low_confidence','nan','unknown'])
def test_ocr_and_confirmed_decrease_fail_approval(tmp_path,image,capture,vision,problem):
    path = candidate(tmp_path,enabled=True)
    neg = negative(tmp_path,image)
    vision.supported.side_effect = [False]+[True]*10
    if problem == 'no_triple':
        vision.gems.side_effect = [(100+i,.99) for i in range(10)]
    elif problem == 'decrease':
        vision.gems.side_effect = [(301,.99)]*3+[(300,.99)]*7
    elif problem == 'low_confidence':
        vision.gems.return_value = (301,.94)
    elif problem == 'nan':
        vision.gems.return_value = (301,float('nan'))
    else:
        vision.gems.return_value = (None,.99)
    assert not enable_check(capture,path,neg,checks=10)
    data = json.loads(path.read_text())
    assert data['ocr_verified'] is False and data['readiness_verified'] is False
    assert data['verification_report']['decreased'] is (problem == 'decrease')


@pytest.mark.parametrize('failure', ['unsupported','capture','low_confidence'])
def test_unknown_breaks_triple_streak_not_zero(tmp_path,image,capture,vision,failure):
    path = candidate(tmp_path)
    neg = negative(tmp_path,image)
    vision.supported.side_effect = [False]+[True,True,False,True,True,False,True,True,False,True]
    if failure == 'capture':
        capture.capture.side_effect = [image,image,RuntimeError('failed'),image,image,
                                       RuntimeError('failed'),image,image,RuntimeError('failed'),image]
        vision.supported.side_effect = [False]+[True]*7
    if failure == 'low_confidence':
        vision.supported.side_effect = [False]+[True]*10
        vision.gems.side_effect = [(0,.99),(0,.99),(None,0.)]*3+[(0,.99)]
    tracker = module.Gems()
    tracker.read = Mock(wraps=tracker.read)
    original = module.Gems
    module.Gems = Mock(return_value=tracker)
    try:
        assert not enable_check(capture,path,neg,checks=10)
    finally:
        module.Gems = original
    assert sum(call.args[:2] == (None,0.) for call in tracker.read.call_args_list) == 3
    assert json.loads(path.read_text())['verification_report']['confirmed_readings'] == 0


def test_capture_failures_counted_without_ocr(tmp_path,image,capture,vision):
    path = candidate(tmp_path,enabled=True)
    capture.capture.side_effect = RuntimeError('offline')
    vision.supported.return_value = False  # negative rejected
    assert not enable_check(capture,path,negative(tmp_path,image),checks=10)
    report = json.loads(path.read_text())['verification_report']
    assert report['sampled'] == report['capture_failures'] == 10
    vision.gems.assert_not_called()
    capture.calibration_identity.assert_not_called()


@pytest.mark.parametrize('identity', [None, {}, {'backend':'other'},
                                    dict(SOURCE,serial='different-device'),
                                    dict(SOURCE,native_size=None)])
def test_source_mismatch_or_malformed_is_fatal(tmp_path,image,capture,vision,identity):
    path = candidate(tmp_path,enabled=True)
    neg = negative(tmp_path,image)
    vision.supported.return_value = False
    def changed():
        assert capture.capture.call_count == 1
        return identity
    capture.calibration_identity.side_effect = changed
    assert not enable_check(capture,path,neg,checks=10)
    data = json.loads(path.read_text())
    assert data['ocr_verified'] is False and data['readiness_verified'] is False
    vision.gems.assert_not_called()
    assert capture.capture.call_count == 1


def test_source_change_mid_burst_invalidates_even_sufficient_rate(tmp_path,image,capture,vision):
    path = candidate(tmp_path,enabled=True)
    neg = negative(tmp_path,image)
    vision.supported.side_effect = [False]+[True]*9
    capture.calibration_identity.side_effect = [SOURCE]*9+[dict(SOURCE,serial='changed')]
    assert not enable_check(capture,path,neg,checks=10)
    assert vision.gems.call_count == 9


def test_verification_only_source_failure_preserves_enabled_metadata(tmp_path,capture,vision):
    path = candidate(tmp_path,enabled=True)
    before = path.read_bytes()
    capture.calibration_identity.return_value = dict(SOURCE,serial='wrong')
    assert not module.verify_calibration(capture,path,checks=10,sleep_seconds=0)
    assert path.read_bytes() == before
    vision.gems.assert_not_called()


@pytest.mark.parametrize('kind', ['none','size','dtype','gray'])
def test_invalid_capture_image_counts_failure_without_identity_or_ocr(tmp_path,image,capture,vision,kind):
    path = candidate(tmp_path)
    neg = negative(tmp_path,image)
    bad = {'none':None,'size':image[:-1], 'dtype':image.astype(np.float32),'gray':image[:,:,0]}[kind]
    capture.capture.return_value = bad
    vision.supported.return_value = False
    assert not enable_check(capture,path,neg,checks=10)
    assert json.loads(path.read_text())['verification_report']['capture_failures'] == 10
    capture.calibration_identity.assert_not_called()
    vision.gems.assert_not_called()


def test_ocr_exception_counts_unknown_and_can_still_meet_rate(tmp_path,image,capture,vision):
    path = candidate(tmp_path)
    neg = negative(tmp_path,image)
    vision.supported.side_effect = [False]+[True]*10
    vision.gems.side_effect = [(0,.99)]*4+[RuntimeError('OCR failed')]+[(0,.99)]*5
    assert enable_check(capture,path,neg,checks=10)
    report = json.loads(path.read_text())['verification_report']
    assert report['high_confidence'] == 9 and report['sample_errors'] == 1
    assert report['confirmed_readings'] == 5


def test_confirmed_decrease_survives_tracker_timeout_in_verification(tmp_path,image,capture,vision,monkeypatch):
    path = candidate(tmp_path,enabled=True)
    neg = negative(tmp_path,image)
    vision.supported.side_effect = [False]+[True]*10
    vision.gems.side_effect = [(301,.99)]*3+[(300,.99)]*7
    # The core tracker correctly resets its baseline on timeout; the verification
    # report still must not approve a drop across the two confirmed baselines.
    clock = iter([0,1,2,100,101,102,103,104,105,106])
    monkeypatch.setattr(module.time,'monotonic',lambda: next(clock))
    assert not enable_check(capture,path,neg,checks=10)
    assert json.loads(path.read_text())['verification_report']['decreased'] is True


@pytest.mark.parametrize('kind', ['accepted','corrupt','size','gray','depth','alpha'])
def test_negative_must_decode_strictly_and_be_rejected(tmp_path,image,capture,vision,kind):
    path = candidate(tmp_path,enabled=True)
    neg = negative(tmp_path,image)
    vision.supported.return_value = kind == 'accepted'
    if kind == 'corrupt':
        neg.write_bytes(b'bad png')
    elif kind == 'size':
        assert cv2.imwrite(str(neg), image[:-1])
    elif kind == 'gray':
        assert cv2.imwrite(str(neg), image[:,:,0])
    elif kind == 'depth':
        assert cv2.imwrite(str(neg), image.astype(np.uint16))
    elif kind == 'alpha':
        assert cv2.imwrite(str(neg), np.dstack([image,image[:,:,0]]))
    assert not enable_check(capture,path,neg,checks=10)
    assert json.loads(path.read_text())['ocr_verified'] is False
    capture.capture.assert_not_called()
    vision.gems.assert_not_called()


@pytest.mark.parametrize('checks,sleep', [(9,0),(True,0),(10,float('nan')),(10,-1),(10,True)])
def test_invalid_verification_parameters_disable_on_approval(tmp_path,image,capture,vision,checks,sleep):
    path = candidate(tmp_path,enabled=True)
    assert not module.verify_calibration(capture,path,checks=checks,negative_paths=[negative(tmp_path,image)],
                                         approve=True,sleep_seconds=sleep)
    data = json.loads(path.read_text())
    assert data['ocr_verified'] is False and data['readiness_verified'] is False


def test_truthy_nonboolean_is_not_explicit_approval(tmp_path,capture,vision):
    path = candidate(tmp_path,enabled=True)
    before = path.read_bytes()
    assert not module.verify_calibration(capture,path,approve='yes',sleep_seconds=0)
    assert path.read_bytes() == before
    capture.capture.assert_not_called()


def test_window_identity_fallback_and_sample_sleep(tmp_path,image,vision,monkeypatch):
    class WindowCapture:
        def is_window_active(self):
            return True
        def capture(self):
            return image
    path = candidate(tmp_path)
    data = json.loads(path.read_text())
    data['source'] = {'backend':'window'}
    path.write_text(json.dumps(data))
    sleep = Mock()
    monkeypatch.setattr(module.time,'sleep',sleep)
    assert module.verify_calibration(WindowCapture(),path,checks=10,sleep_seconds=.5)
    assert sleep.call_count == 9


def test_pending_file_is_not_overwritten_or_deleted(tmp_path):
    path = tmp_path/'calibration.json'
    pending = path.with_name(path.name+'.pending')
    pending.write_text('other writer')
    with pytest.raises(FileExistsError):
        module._save(path,{'ocr_verified':False})
    assert pending.read_text() == 'other writer' and not path.exists()


def test_focus_loss_blocks_verification_and_ocr(tmp_path, capture, vision):
    capture.is_window_active.return_value = False
    assert not module.verify_calibration(capture, candidate(tmp_path), checks=10, sleep_seconds=0)
    capture.capture.assert_not_called()
    vision.gems.assert_not_called()


def test_focus_changes_during_capture_blocks_verification(tmp_path, capture, vision):
    capture.is_window_active.side_effect = [True, False] * 10
    assert not module.verify_calibration(capture, candidate(tmp_path), checks=10, sleep_seconds=0)
    vision.gems.assert_not_called()


def test_focus_loss_blocks_creation_before_writes(tmp_path, references, capture, vision):
    capture.is_window_active.return_value = False
    path = tmp_path/'new-focus'/'calibration.json'
    with pytest.raises(RuntimeError, match='focused'):
        module.create_animation_calibration(capture, path, references, GEMS, INTERACTIONS, ANIMATION)
    assert not path.parent.exists()
