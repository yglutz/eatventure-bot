"""Offline tests only. Run this file with uv run python for screenshot metrics."""
import json
import time
from unittest.mock import Mock
from pathlib import Path

import cv2
import numpy as np
import pytest

from learning.core import SIZE, fingerprint
from learning.vision import Vision, build_animation_profile, _AnimationProfile

GEMS = [230, 40, 42, 20]
ROIS = [[20, 80, 320, 541]]
CASH = [75, 38, 80, 20]


def engine(refs, metadata=None):
    v = Vision.__new__(Vision)
    v.references = refs
    v.data = {'gems_roi': GEMS}
    v.data['animation_profile'] = metadata or build_animation_profile(
        refs, ROIS, gems_roi=GEMS, cash_roi=CASH)
    return v


def synthetic():
    base = np.full((SIZE[1], SIZE[0], 3), 170, np.uint8)
    base[:80] = (40, 60, 80)
    base[621:] = (60, 130, 80)
    # Distributed fixed structure, with bounded moving sprite positions.
    for x in range(40, 340, 60):
        cv2.line(base, (x,80), (x,620), (60,80,100), 4)
    refs = []
    for i in range(12):
        im = base.copy()
        cv2.circle(im, (80+i*4,240), 10, (40,100,220), -1)
        refs.append(im)
    return base, refs


def test_animation_and_whole_view_negatives():
    base, refs = synthetic()
    v = engine(refs)
    assert all(v.supported(im) for im in refs)
    moving = base.copy()
    cv2.circle(moving, (102,240), 10, (40,100,220), -1)
    assert v.supported(moving)
    for rect in [(100,180,150,160), (80,220,180,120), (20,650,160,80)]:
        changed = moving.copy()
        x,y,w,h = rect
        changed[y:y+h,x:x+w] = 240
        assert not v.supported(changed)
    assert not v.supported((moving*.65).astype(np.uint8))
    assert not v.supported((moving*.90).astype(np.uint8))
    assert not v.supported(np.zeros_like(moving))
    assert not v.supported(moving[:-1])
    assert not v.supported(moving.astype(np.float32))
    assert not v.supported(None)


@pytest.mark.parametrize('mutation', ['version', 'extra', 'mask', 'cash', 'gems', 'hash', 'size'])
def test_invalid_profiles_fail_closed(mutation):
    base, refs = synthetic()
    p = build_animation_profile(refs, ROIS, gems_roi=GEMS, cash_roi=CASH)
    if mutation == 'version':
        p['version'] = True
    elif mutation == 'extra':
        p['threshold'] = 255
    elif mutation == 'mask':
        p['animation_rois'] = [[0,0,360,780]]
    elif mutation == 'cash':
        p['cash_roi'] = [20,30,300,30]
    elif mutation == 'gems':
        p['gems_roi'] = [0,0,360,780]
    elif mutation == 'hash':
        p['reference_hashes'][0] = 'corrupt'
    elif mutation == 'size':
        p['size'] = [780,360]
    with pytest.raises(ValueError):
        _AnimationProfile(p, refs, GEMS)
    assert not engine(refs,p).supported(base)


def test_corrupt_references_and_insufficient_anchors():
    base, refs = synthetic()
    p = build_animation_profile(refs, ROIS, gems_roi=GEMS)
    changed = [im.copy() for im in refs]
    changed[0][100,100] = 0
    assert not engine(changed,p).supported(base)
    with pytest.raises(ValueError):
        build_animation_profile(refs[:3], ROIS, gems_roi=GEMS)
    rng = np.random.default_rng(4)
    with pytest.raises(ValueError, match='anchors'):
        build_animation_profile([rng.integers(0,256,base.shape,dtype=np.uint8)
                                 for _ in range(12)], ROIS, gems_roi=GEMS)


def test_json_roundtrip_and_legacy_opt_in_only():
    base, refs = synthetic()
    p = json.loads(json.dumps(build_animation_profile(refs, ROIS, gems_roi=GEMS)))
    assert engine(refs,p).supported(refs[0])
    legacy = Vision.__new__(Vision)
    legacy.data = {'gems_roi': GEMS}
    legacy.references = refs
    assert legacy.supported(refs[0])
    legacy.data['animation_profile'] = None
    assert not legacy.supported(refs[0])


@pytest.mark.parametrize('metadata', [None, [], {'version': 1}, {'source': 'untrusted'}])
def test_malformed_profile_never_falls_back(metadata):
    _, refs = synthetic()
    v = Vision.__new__(Vision)
    v.data = {'gems_roi': GEMS, 'animation_profile': metadata}
    v.references = refs
    assert not v.supported(refs[0])


def test_source_mismatch_still_rejected_before_calibration_writes(tmp_path):
    from learning.vision import calibrate
    path = tmp_path/'calibration.json'
    path.write_text(json.dumps({'source': {'backend': 'adb', 'serial': 'approved'}}))
    capture = Mock()
    capture.capture.return_value = synthetic()[0]
    capture.calibration_identity.return_value = {'backend': 'adb', 'serial': 'different'}
    with pytest.raises(ValueError, match='source changed'):
        calibrate(capture, path, GEMS, [[70,285,210,295]])
    assert json.loads(path.read_text()) == {'source': {'backend': 'adb', 'serial': 'approved'}}


@pytest.mark.parametrize('report', [{}, None, {'passed': False},
                                  {'passed': True, 'profile_version': 999},
                                  {'passed': True, 'profile_version': 1, 'negatives_rejected': 0}])
def test_runtime_requires_explicit_live_and_negative_approval(tmp_path, report):
    path = tmp_path/'calibration.json'
    path.write_text(json.dumps({'size': list(SIZE), 'config': fingerprint(),
                               'ocr_verified': True, 'readiness_verified': True,
                               'animation_profile': {}, 'verification_report': report}))
    with pytest.raises(ValueError, match='approved live/negative'):
        Vision(path)


def test_legacy_recalibration_cannot_strip_animation_profile(tmp_path):
    from learning.vision import calibrate
    path = tmp_path/'calibration.json'
    data = {'source': {'backend': 'window'}, 'animation_profile': {'version': 1}}
    path.write_text(json.dumps(data))
    capture = Mock()
    capture.capture.return_value = synthetic()[0]
    capture.calibration_identity.return_value = {'backend': 'window'}
    with pytest.raises(ValueError, match='replace an animation profile'):
        calibrate(capture, path, GEMS, [[70,285,210,295]])
    assert json.loads(path.read_text()) == data


def test_local_screenshot_validation():
    root = Path(__file__).resolve().parents[1] / 'screenshots'
    if not (root/'adb-candidates'/'0.png').exists():
        pytest.skip('Approved screenshots are local and intentionally not in Git')
    refs = [cv2.imread(str(root/'adb-candidates'/f'{i}.png')) for i in range(12)]
    v = engine(refs)
    assert all(v.supported(im) for im in refs)
    # Holdout is evaluation ONLY; profile is already frozen above.
    heldout = [cv2.imread(str(root/'adb-heldout'/f'{i}.png')) for i in range(20)]
    assert sum(v.supported(im) for im in heldout) >= 15
    popup = cv2.imread(str(root/'adb-connection-test.png'))
    assert not v.supported(popup)
    # Remove the banner confound: transplant only the station panel into a
    # positive approved frame. Its entire extent is inside the animation ROI.
    panel = refs[0].copy()
    panel[220:401,195:340] = popup[220:401,195:340]
    assert not v.supported(panel)
    assert not v.supported((refs[0]*.7).astype(np.uint8))
    assert not v.supported((refs[0]*.9).astype(np.uint8))
    layout = refs[0].copy()
    layout[80:621,20:340] = refs[0][80:621,20:340][:,::-1]
    assert not v.supported(layout)


def screenshot_metrics():
    root = Path(__file__).resolve().parents[1] / 'screenshots'
    refs = [cv2.imread(str(root/'adb-candidates'/f'{i}.png')) for i in range(12)]
    if any(im is None for im in refs):
        raise FileNotFoundError('Local approved screenshot burst unavailable')
    v = engine(refs)
    start = time.perf_counter()
    approved = [v.supported(im) for im in refs]
    heldout = [v.supported(cv2.imread(str(root/'adb-heldout'/f'{i}.png'))) for i in range(20)]
    popup = v.supported(cv2.imread(str(root/'adb-connection-test.png')))
    print({'approved': f'{sum(approved)}/12', 'heldout': f'{sum(heldout)}/20',
           'heldout_rejected_indices': [i for i,ok in enumerate(heldout) if not ok],
           'popup_supported': popup,
           'mean_seconds_per_frame_including_io': round((time.perf_counter()-start)/33,3)})
    return approved, heldout, popup


if __name__ == '__main__':
    screenshot_metrics()
