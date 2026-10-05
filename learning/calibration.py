"""Operator-approved calibration lifecycle; capture/OCR only, never input.

Creation requires a new directory and leaves both readiness flags false.
Verification is read-only unless approve=True; explicit approval revokes any
previous readiness before attempting checks. Neither operation trains a model.
"""
import json
import math
import os
import time
from pathlib import Path

import cv2
import numpy as np

from learning.core import Gems, SIZE, fingerprint
from learning.vision import Vision, build_animation_profile
from safety import zones


def _image(image):
    if (not isinstance(image, np.ndarray) or image.dtype != np.uint8 or
            image.shape != (SIZE[1], SIZE[0], 3)):
        raise ValueError('Expected calibrated client-sized uint8 BGR image')
    return image


def _png(path):
    path = Path(path)
    if path.suffix.lower() != '.png':
        raise ValueError('Reference/negative must be a PNG')
    with path.open('rb') as stream:
        if stream.read(8) != b'\x89PNG\r\n\x1a\n':
            raise ValueError('Invalid PNG signature')
    return _image(cv2.imread(str(path), cv2.IMREAD_UNCHANGED))


def _source(capture):
    identity = (capture.calibration_identity() if hasattr(capture, 'calibration_identity')
                else {'backend': 'window'})
    if not isinstance(identity, dict) or identity.get('backend') not in ('window', 'adb'):
        raise ValueError('Invalid capture source identity')
    expected = ({'backend', 'serial', 'package', 'native_size'}
                if identity['backend'] == 'adb' else {'backend'})
    if set(identity) != expected:
        raise ValueError('Malformed capture source identity fields')
    if identity['backend'] == 'adb':
        if any(not isinstance(identity.get(k), str) or not identity[k].strip()
               for k in ('serial', 'package')):
            raise ValueError('ADB source requires serial and package')
        size = identity.get('native_size')
        if (not isinstance(size, list) or len(size) != 2 or
                any(type(v) is not int or v <= 0 for v in size)):
            raise ValueError('ADB source requires captured native size')
    # Freeze the identity, reject non-JSON/nonfinite values rather than coercing.
    return json.loads(json.dumps(identity, allow_nan=False))


def _roi(rect):
    if (not isinstance(rect, (list, tuple)) or len(rect) != 4 or
            any(type(v) is not int for v in rect)):
        raise ValueError('Rectangles must have four integer coordinates')
    x, y, w, h = rect
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x+w > SIZE[0] or y+h > SIZE[1]:
        raise ValueError('Calibration rectangle outside client area')
    return list(rect)


def _interaction(rois):
    rois = [_roi(rect) for rect in rois]
    if not rois:
        raise ValueError('Safe interaction areas required')
    # These new candidates use wholly safe integer-pixel rectangles. Inclusive
    # zone edges remain forbidden; overlapping interaction rectangles are fine.
    # Runtime MouseController/static masks are still binding, not replaced here.
    for x, y, w, h in rois:
        if any(x <= b and x+w-1 >= a and y <= d and y+h-1 >= c
               for a,b,c,d in zones()):
            raise ValueError('Interaction rectangle intersects an inclusive forbidden zone')
    return rois


def _reading(value, confidence):
    if (not isinstance(value, (int, np.integer)) or isinstance(value, (bool, np.bool_))
            or value < 0 or not isinstance(confidence, (int, float, np.floating))
            or isinstance(confidence, bool) or not math.isfinite(confidence)
            or not .95 <= confidence <= 1):
        return None, 0.
    return int(value), float(confidence)


def _save(path, data):
    # Atomic metadata replacement: readers see either a disabled candidate or a
    # complete approval report, never partially written enabled flags.
    temporary = path.with_name(path.name + '.pending')
    owned = False
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            owned = True
            json.dump(data, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if owned and temporary.exists():
            temporary.unlink()


def create_animation_calibration(capture, path, reference_paths, gems_roi,
                                 interaction_rois, animation_rois, cash_roi=None):
    """Return saved candidate metadata, always disabled, or raise on failure.

    path is a JSON file in a NEW directory (even an existing empty directory is
    rejected). reference_paths are 8..32 operator-approved PNGs, ordered by time;
    approval cannot be inferred by this API. A fresh valid capture precedes
    source binding. OCR must pass on EVERY reference, with a confirmed Gems
    triple and no confirmed decrease. Failures after writing retain the disabled
    candidate for inspection. No files in old calibration directories are touched.
    """
    path = Path(path)
    if path.exists() or path.parent.exists():
        raise FileExistsError('Use a separate NEW calibration directory; no overwrites')
    paths = list(reference_paths)
    if not 8 <= len(paths) <= 32:
        raise ValueError('Requires 8..32 operator-approved references')
    gems_roi = _roi(gems_roi)
    interactions = _interaction(interaction_rois)
    images = [_png(p) for p in paths]
    profile = build_animation_profile(images, animation_rois, gems_roi=gems_roi, cash_roi=cash_roi)
    if not capture.is_window_active():
        raise RuntimeError('Capture source is not focused/awake')
    _image(capture.capture())
    if not capture.is_window_active():
        raise RuntimeError('Focus changed during capture')
    source = _source(capture)
    # mkdir(exist_ok=False) also closes the existing-directory race.
    path.parent.mkdir(parents=True, exist_ok=False)
    data = {'size': list(SIZE), 'config': fingerprint(), 'source': source,
            'gems_roi': gems_roi, 'interaction_rois': interactions,
            'animation_profile': profile, 'views': [],
            'ocr_verified': False, 'readiness_verified': False}
    for i, image in enumerate(images):
        name = f'view-{i:03d}.png'
        if not cv2.imwrite(str(path.parent/name), image):
            raise RuntimeError(f'Could not write approved reference {name}; candidate not enabled')
        data['views'].append(name)
    _save(path, data)
    vision = Vision(path, calibration_check=True)
    tracker = Gems()
    confirmed = 0
    highest_confirmed = None
    for image in images:
        if not vision.supported(image):
            tracker.read(None, 0., time.monotonic())
            raise ValueError('Approved reference is unsupported; candidate remains disabled')
        value, confidence = _reading(*vision.gems(image))
        _, triple = tracker.read(value, confidence, time.monotonic())
        confirmed += int(triple)
        decreased = triple and highest_confirmed is not None and value < highest_confirmed
        if value is None or tracker.decreased or decreased:
            raise ValueError('Reference Gems OCR uncertain/decreased; candidate remains disabled')
        if triple:
            highest_confirmed = value if highest_confirmed is None else max(value, highest_confirmed)
    if not confirmed:
        raise ValueError('No confirmed reference Gems triple; candidate remains disabled')
    data['creation_report'] = {'version': 1, 'references': len(images),
                               'high_confidence': len(images), 'confirmed_readings': confirmed,
                               'passed': True, 'status': 'disabled pending explicit verification approval'}
    _save(path, data)
    print('Animation candidate created: references/OCR passed; DISABLED pending explicit approval.', flush=True)
    return data


def verify_calibration(capture, path, checks=20, negative_paths=(), approve=False, sleep_seconds=.5):
    """Return bool; inspect candidate without input, learning, or implicit approval.

    checks must be an integer in [10,1000], sleep_seconds finite in [0,10].
    Pass requires ceil(.75*checks) supported high-confidence samples, at least
    one identical high-confidence Gems triple, no confirmed decrease, stable
    source identity, and rejection of every supplied strict client-sized PNG.
    approve=True additionally requires an animation profile and >=1 negative.
    Legacy metadata without an animation_profile is never mutated by approval.
    For animation candidates, approval revokes BOTH flags first, even on failure.
    Verification-only never writes metadata. Sample capture/OCR errors count as failures and break
    the Gems streak. Capture implementations must enforce their own I/O timeout.
    """
    if type(approve) is not bool:
        print('Calibration verification failed: approve must be an explicit boolean.', flush=True)
        return False
    path = Path(path)
    report = {'version': 1, 'checks': checks if type(checks) is int else None,
              'sampled': 0, 'supported': 0,
              'high_confidence': 0, 'confirmed_readings': 0, 'capture_failures': 0,
              'sample_errors': 0, 'negative_count': 0, 'negatives_rejected': 0,
              'decreased': False, 'source': None, 'passed': False}
    data = None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('Invalid calibration metadata')
        if approve:
            if 'animation_profile' not in data:
                print('Calibration approval refused: legacy metadata is read-only; create a separate animation candidate.',
                      flush=True)
                return False
            data['ocr_verified'] = data['readiness_verified'] = False
            _save(path, data)
        if type(checks) is not int or not 10 <= checks <= 1000:
            raise ValueError('checks must be an integer in [10,1000]')
        if (isinstance(sleep_seconds, bool) or not isinstance(sleep_seconds, (int, float))
                or not math.isfinite(sleep_seconds) or not 0 <= sleep_seconds <= 10):
            raise ValueError('sleep_seconds must be finite in [0,10]')
        negatives = list(negative_paths)
        report['negative_count'] = len(negatives)
        if approve and (not data.get('animation_profile') or not negatives):
            raise ValueError('Approval requires an animation profile and explicit negative PNGs')
        _interaction(data['interaction_rois'])
        _roi(data['gems_roi'])
        vision = Vision(path, calibration_check=True)
        for negative in negatives:
            if vision.supported(_png(negative)):
                raise ValueError('Negative frame was accepted')
            report['negatives_rejected'] += 1
        tracker = Gems()
        highest_confirmed = None
        for i in range(checks):
            report['sampled'] += 1
            value, confidence = None, 0.
            try:
                if not capture.is_window_active():
                    raise RuntimeError('Capture source is not focused/awake')
                image = _image(capture.capture())
                if not capture.is_window_active():
                    raise RuntimeError('Focus changed during capture')
            except (RuntimeError, OSError, ValueError, cv2.error):
                report['capture_failures'] += 1
                image = None
            if image is not None:
                # Bind/compare only AFTER the fresh capture, on every sample.
                # Any source error is fatal, not a tolerable sample failure.
                try:
                    source = _source(capture)
                    report['source'] = source
                    if source != data.get('source', {'backend': 'window'}):
                        raise ValueError('Capture source changed')
                except (RuntimeError, OSError, ValueError, TypeError):
                    tracker.read(None, 0., time.monotonic())
                    raise
                try:
                    if vision.supported(image):
                        report['supported'] += 1
                        value, confidence = _reading(*vision.gems(image))
                        report['high_confidence'] += int(value is not None)
                except (RuntimeError, OSError, ValueError, TypeError, cv2.error):
                    report['sample_errors'] += 1
                    value, confidence = None, 0.
            _, triple = tracker.read(value, confidence, time.monotonic())
            report['confirmed_readings'] += int(triple)
            if triple:
                if highest_confirmed is not None and value < highest_confirmed:
                    report['decreased'] = True
                highest_confirmed = value if highest_confirmed is None else max(value, highest_confirmed)
            report['decreased'] |= tracker.decreased
            if i+1 < checks and sleep_seconds:
                time.sleep(sleep_seconds)
        report['required_high_confidence'] = math.ceil(.75*checks)
        report['profile_version'] = (data.get('animation_profile') or {}).get('version')
        report['passed'] = (report['high_confidence'] >= report['required_high_confidence'] and
                            report['confirmed_readings'] > 0 and not report['decreased'])
    except (RuntimeError, OSError, ValueError, TypeError, KeyError, cv2.error) as exc:
        report['error'] = str(exc)
    if approve and isinstance(data, dict):
        data['ocr_verified'] = data['readiness_verified'] = bool(report['passed'])
        data['verification_report'] = report
        _save(path, data)
    print('Calibration verification:', report, flush=True)
    return bool(report['passed'])
