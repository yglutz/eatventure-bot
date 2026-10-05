import json
import re
import shutil
import hashlib
from pathlib import Path

import cv2
import numpy as np
from learning.core import SIZE, fingerprint
from safety import zones


ANIMATION_VERSION = 1
_PROFILE_KEYS = {'version', 'size', 'animation_rois', 'gems_roi', 'cash_roi',
                 'reference_hashes'}


def _frame(image):
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.shape != (SIZE[1], SIZE[0], 3):
        raise ValueError('Invalid animation reference/frame')
    return cv2.GaussianBlur(cv2.resize(image, (180, 390), interpolation=cv2.INTER_AREA), (3, 3), 0)


def _rectangle(roi, bounds):
    if not isinstance(roi, (list, tuple)) or len(roi) != 4 or any(type(v) is not int for v in roi):
        raise ValueError('Invalid animation rectangle')
    x, y, w, h = roi
    a, b, c, d = bounds
    if w <= 0 or h <= 0 or x < a or y < b or x+w > c or y+h > d:
        raise ValueError('Animation rectangle outside reviewed bounds')
    return list(roi)


def _hash(image):
    _frame(image)
    return hashlib.sha256(image.tobytes()).hexdigest()


def build_animation_profile(images, animation_rois, *, gems_roi, cash_roi=None):
    """Build JSON-serializable opt-in metadata from 8..32 *approved* BGR uint8 frames.

    Rectangles are (x, y, width, height), in the calibrated 360x780 client.
    Restaurant rectangles must lie within [20,340) x [80,621); optional cash
    numeric rectangle within [65,160) x [30,61). Gems remains OCR-only, never
    an animation ROI. Store returned dict as calibration['animation_profile']
    and the SAME approved images (same order) as calibration['views']. Vision
    reconstructs all masks from those references, verifies their hashes and
    rejects unsupported versions/fields. No thresholds/masks are caller-tunable.
    No online fitting, input authorization or source identity changes occur.
    """
    images = list(images)
    if not 8 <= len(images) <= 32:
        raise ValueError('Animation profile requires 8..32 approved references')
    profile = dict(version=ANIMATION_VERSION, size=list(SIZE),
                   animation_rois=list(animation_rois), gems_roi=list(gems_roi),
                   cash_roi=list(cash_roi) if cash_roi is not None else None,
                   reference_hashes=[_hash(im) for im in images])
    _AnimationProfile(profile, images, gems_roi)
    return profile


class _AnimationProfile:
    """Fixed bounded temporal-color matcher, with whole-view residual monitoring."""
    def __init__(self, metadata, references, gems_roi):
        if (not isinstance(metadata, dict) or set(metadata) != _PROFILE_KEYS or
                type(metadata['version']) is not int or metadata['version'] != ANIMATION_VERSION or
                metadata['size'] != list(SIZE) or metadata['gems_roi'] != list(gems_roi)):
            raise ValueError('Invalid animation profile schema/version')
        if not 8 <= len(references) <= 32 or metadata['reference_hashes'] != [_hash(im) for im in references]:
            raise ValueError('Animation approved references corrupted/mismatched')
        rois = metadata['animation_rois']
        if not isinstance(rois, list) or not 1 <= len(rois) <= 8:
            raise ValueError('Explicit reviewed animation rectangles required')
        rois = [_rectangle(r, (20, 80, 340, 621)) for r in rois]
        gem = _rectangle(metadata['gems_roi'], (225, 30, 280, 65))
        if gem[2] > 50 or gem[3] > 24:
            raise ValueError('Gems exclusion must contain only the numeric counter')
        cash = metadata['cash_roi']
        if cash is not None:
            cash = _rectangle(cash, (65, 30, 160, 61))
            if cash[2] > 85 or cash[3] > 24:
                raise ValueError('Cash animation must contain only the numeric counter')
        self.samples = np.stack([_frame(im) for im in references]).astype(np.int16)
        self.median = np.median(self.samples, axis=0)
        self.dynamic = np.zeros((390, 180), bool)
        self.ignored = np.zeros_like(self.dynamic)
        def mark(mask, rect):
            x,y,w,h = rect
            mask[y//2:(y+h+1)//2, x//2:(x+w+1)//2] = True
        for roi in rois:
            mark(self.dynamic, roi)
        if cash is not None:
            mark(self.dynamic, cash)
        mark(self.ignored, gem)
        # Stable evidence is required in distributed tiles, including restaurant
        # structure, not merely the top/bottom UI. No learned mask is serialized.
        spread = np.ptp(self.samples, axis=0).max(axis=2)
        self.anchors = (spread <= 12) & ~self.ignored
        # In reviewed motion areas, flat floor is not a reliable anchor: a new
        # sprite can legitimately cross it. Use stable structural edges instead.
        gray = cv2.cvtColor(self.median.astype(np.uint8), cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 35, 70)
        structure = cv2.dilate(edges, np.ones((3,3),np.uint8)) > 0
        stable_neighborhood = cv2.erode((spread <= 12).astype(np.uint8), np.ones((3,3),np.uint8)) > 0
        self.anchors &= ~self.dynamic | (structure & stable_neighborhood)
        self.anchor_tiles = []
        for y in range(0, 390, 30):
            for x in range(0, 180, 30):
                sl = (slice(y,y+30), slice(x,x+30))
                anchor = self.anchors[sl]
                if anchor.sum() >= 40:
                    self.anchor_tiles.append((sl, anchor))
        if len(self.anchor_tiles) < 30 or self.anchors.sum() < self.anchors.size * .15:
            raise ValueError('Insufficient distributed stable animation anchors')
        for by in range(3):
            for bx in range(3):
                if self.anchors[by*130:(by+1)*130, bx*60:(bx+1)*60].sum() < 250:
                    raise ValueError('Missing distributed stable animation anchors')

    def supported(self, image):
        try:
            current = _frame(image).astype(np.int16)
        except (ValueError, cv2.error):
            return False
        median_distance = np.abs(current-self.median).max(axis=2)
        # Coherent exposure changes (modal backdrop/dimming) are not sprite
        # animation, even when each individual pixel is within its tolerance.
        signed = (current-self.median).mean(axis=2)[self.anchors]
        if ((np.median(signed) < -4 and np.mean(signed < -3) > .65) or
                (np.median(signed) > 4 and np.mean(signed > 3) > .65)):
            return False
        # Static pixels remain strict. Dynamic pixels must match an actually
        # observed RGB tuple AND remain within a fixed maximum variation cap.
        distance = self.color_distance(current)
        residual = np.where(self.dynamic, (distance > 28) | (median_distance > 140),
                            median_distance > 18)
        residual[self.ignored] = False
        # Small sprites may occlude anchors, but large contiguous changes may not.
        for sl, anchor in self.anchor_tiles:
            bad = (median_distance[sl] > 22) & anchor
            if bad.sum() / anchor.sum() > .28:
                return False
        if residual.mean() > .10:
            return False
        for y in range(0,390,20):
            for x in range(0,180,20):
                if residual[y:y+20,x:x+20].mean() > .60:
                    return False
        # Morphological closure joins text/panel borders, not isolated sprite noise.
        joined = cv2.morphologyEx(residual.astype(np.uint8), cv2.MORPH_CLOSE,
                                 np.ones((3,3),np.uint8))
        _, _, stats, _ = cv2.connectedComponentsWithStats(joined, 8)
        if len(stats) > 1 and stats[1:,cv2.CC_STAT_AREA].max() > 650:
            return False
        return True

    def color_distance(self, current):
        # Bounded +/-8-client-pixel displacement accounts for sub-frame sprite
        # motion. Compare actual color tuples, never channelwise extrema. Static
        # pixels do not receive this spatial/temporal tolerance.
        distance = np.full(current.shape[:2], 255, np.int16)
        padded = np.pad(self.samples, ((0,0),(4,4),(4,4),(0,0)), mode='edge')
        for dy in (-4,-2,0,2,4):
            for dx in (-4,-2,0,2,4):
                samples = padded[:,4+dy:394+dy,4+dx:184+dx]
                distance = np.minimum(distance, np.abs(samples-current).max(axis=3).min(axis=0))
        return distance


class Vision:
    def __init__(self, calibration, calibration_check=False):
        self.path = Path(calibration)
        self.data = json.loads(self.path.read_text())
        if not calibration_check and not self.data.get('ocr_verified', False):
            raise ValueError('Calibration has no confident OCR reading; recalibrate')
        if 'animation_profile' in self.data and not calibration_check:
            report = self.data.get('verification_report', {})
            if (not isinstance(report, dict) or self.data.get('readiness_verified') is not True or
                    report.get('passed') is not True or
                    report.get('profile_version') != ANIMATION_VERSION or
                    report.get('negatives_rejected', 0) < 1 or
                    report.get('confirmed_readings', 0) < 1):
                raise ValueError('Animation calibration has no approved live/negative verification; verify first')
        if self.data['config'] != fingerprint() or tuple(self.data['size']) != SIZE:
            raise ValueError('Calibration/configuration mismatch; recalibrate')
        self.references = [cv2.imread(str(self.path.parent / p)) for p in self.data['views']]
        if not self.references or any(p is None for p in self.references):
            raise ValueError('Supported view references missing')
        self.animation_profile = None
        if 'animation_profile' in self.data:
            try:
                self.animation_profile = _AnimationProfile(self.data['animation_profile'], self.references,
                                                           self.data['gems_roi'])
            except (ValueError, TypeError, KeyError, cv2.error) as exc:
                raise ValueError('Invalid animation calibration; recalibrate') from exc
        if not self.data.get('interaction_rois'):
            raise ValueError('Safe gameplay interaction areas missing; recalibrate')
        # Copy the bundled ONNX models into the project for explicit offline reuse.
        import rapidocr_onnxruntime
        from rapidocr_onnxruntime import RapidOCR
        model_dir = self.path.parent / 'ocr'
        model_dir.mkdir(exist_ok=True)
        bundled = Path(rapidocr_onnxruntime.__file__).parent / 'models'
        paths = {}
        for kind in ('det', 'rec', 'cls'):
            source = next(bundled.glob(f'*{kind}*.onnx'))
            target = model_dir / source.name
            if not target.exists():
                shutil.copyfile(source, target)
            paths[f'{kind}_model_path'] = str(target)
        self.ocr = RapidOCR(**paths)

    def path_supported(self, start, end):
        # Both endpoints in the same convex, explicitly calibrated gameplay area.
        return any(all(x <= p[0] < x+w and y <= p[1] < y+h for p in (start, end))
                   for x, y, w, h in self.data['interaction_rois'])

    def supported(self, image):
        if 'animation_profile' in self.data:
            # Also supports legacy __new__ tests: explicit metadata never falls
            # back to legacy matching on missing/invalid profile initialization.
            try:
                profile = getattr(self, 'animation_profile', None)
                if profile is None:
                    profile = _AnimationProfile(self.data['animation_profile'], self.references,
                                                self.data['gems_roi'])
                    self.animation_profile = profile
                return profile.supported(image)
            except (ValueError, TypeError, KeyError, cv2.error):
                return False
        if image.shape[:2] != (SIZE[1], SIZE[0]):
            return False
        # Conservative whole-view matching: changed dialogs/loading fail closed.
        # Calibration is a whitelist; OCR alone never authorizes a click.
        x, y, w, h = self.data['gems_roi']
        # Suppress small moving sprites/text while preserving dialog-sized changes.
        current = cv2.GaussianBlur(cv2.resize(image, (96, 208)), (9, 9), 0).astype(np.float32)
        for reference in self.references:
            other = cv2.GaussianBlur(cv2.resize(reference, (96, 208)), (9, 9), 0).astype(np.float32)
            diff = np.abs(current - other).mean(axis=2)
            diff[int(y*208/SIZE[1]):int((y+h)*208/SIZE[1])+1,
                 int(x*96/SIZE[0]):int((x+w)*96/SIZE[0])+1] = 0
            if diff.mean() <= 8 and np.quantile(diff, .95) <= 40:
                return True
        return False

    def gems(self, image):
        x, y, w, h = self.data['gems_roi']
        crop = image[y:y+h, x:x+w]
        # The ROI is already one text line. Detection can split adjacent digits
        # into separate boxes; run the recognizer directly on the original crop.
        result, _ = self.ocr(crop, use_det=False, use_cls=False)
        if not result or len(result) != 1:
            return None, 0.
        text, confidence = result[0]
        # Reject abbreviations, punctuation, signs and mixed text instead of guessing.
        if not re.fullmatch(r'[0-9]+', text.strip()):
            return None, 0.
        return int(text), float(confidence)


def calibrate(capture, path, gems_roi=None, interaction_rois=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image = capture.capture()
    display = image.copy()
    for a, b, c, d in zones():
        cv2.rectangle(display, (int(a), int(c)), (int(b), min(SIZE[1]-1, int(d) if np.isfinite(d) else SIZE[1]-1)), (0,0,255), 2)
    if gems_roi is None:
        print('Mark only the numeric Gems counter; Enter confirms, Esc cancels.', flush=True)
        roi = tuple(map(int, cv2.selectROI('Gems calibration / forbidden zones', display)))
        cv2.destroyAllWindows()
    else:
        roi = tuple(gems_roi)
    if roi[2] == 0 or roi[3] == 0:
        raise ValueError('Calibration cancelled')
    if interaction_rois is None:
        print('Mark safe gameplay interaction areas ONLY: exclude shop, currency, purchases and menus.', flush=True)
        print('Enter adds each rectangle; Esc finishes.', flush=True)
        interaction_rois = cv2.selectROIs('Safe gameplay controls / forbidden zones remain binding', display).tolist()
        cv2.destroyAllWindows()
    if len(interaction_rois) == 0:
        raise ValueError('No safe interaction area selected')
    for x,y,w,h in [roi, *interaction_rois]:
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x+w > SIZE[0] or y+h > SIZE[1]:
            raise ValueError('Calibration rectangle outside client area')
    source = capture.calibration_identity() if hasattr(capture, 'calibration_identity') else {'backend': 'window'}
    old = json.loads(path.read_text()) if path.exists() else None
    if old and old.get('source', {'backend': 'window'}) != source:
        raise ValueError('Capture source changed; use a separate calibration directory')
    if old and 'animation_profile' in old:
        raise ValueError('Do not replace an animation profile with legacy calibration; use a separate directory')
    views = old['views'] if old and old['config'] == fingerprint() else []
    name = f'view-{len(views):03d}.png'
    cv2.imwrite(str(path.parent / name), image)
    views.append(name)
    data = {'size': SIZE, 'config': fingerprint(), 'gems_roi': roi, 'source': source,
            'interaction_rois': interaction_rois, 'views': views, 'ocr_verified': False}
    path.write_text(json.dumps(data, indent=2))
    vision = Vision(path, calibration_check=True)
    value, confidence = vision.gems(image)
    print('OCR calibration reading:', (value, confidence))
    if value is None or confidence < .95:
        raise ValueError('Calibration disabled: OCR confidence below 0.95; adjust Gems rectangle')
    data['ocr_verified'] = True
    path.write_text(json.dumps(data, indent=2))
    print('Supported view saved. Repeat calibrate for additional safe gameplay views.')
