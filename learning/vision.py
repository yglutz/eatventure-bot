import json
import re
import shutil
from pathlib import Path

import cv2
import numpy as np
from learning.core import SIZE, fingerprint
from safety import zones


class Vision:
    def __init__(self, calibration, calibration_check=False):
        self.path = Path(calibration)
        self.data = json.loads(self.path.read_text())
        if not calibration_check and not self.data.get('ocr_verified', False):
            raise ValueError('Calibration has no confident OCR reading; recalibrate')
        if self.data['config'] != fingerprint() or tuple(self.data['size']) != SIZE:
            raise ValueError('Calibration/configuration mismatch; recalibrate')
        self.references = [cv2.imread(str(self.path.parent / p)) for p in self.data['views']]
        if not self.references or any(p is None for p in self.references):
            raise ValueError('Supported view references missing')
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
    old = json.loads(path.read_text()) if path.exists() else None
    views = old['views'] if old and old['config'] == fingerprint() else []
    name = f'view-{len(views):03d}.png'
    cv2.imwrite(str(path.parent / name), image)
    views.append(name)
    data = {'size': SIZE, 'config': fingerprint(), 'gems_roi': roi,
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
