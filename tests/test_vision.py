from unittest.mock import Mock

import numpy as np
import pytest
import json

from learning.core import SIZE
from learning.vision import Vision
from learning.environment import Controls, LiveInterface
import window_capture as capture_module


def vision_without_models():
    vision = Vision.__new__(Vision)
    image = np.random.default_rng(1).integers(0,256,(SIZE[1],SIZE[0],3),dtype=np.uint8)
    vision.references = [image]
    vision.data = {'gems_roi': [220,10,60,20], 'interaction_rois': [[70,200,210,380]]}
    return vision, image


@pytest.mark.parametrize('text,expected', [('123',123),('0',0),('1.2K',None),('-2',None),('Gems 3',None),('1,234',None)])
def test_ocr_rejects_ambiguous_text(text, expected):
    v,image = vision_without_models()
    v.ocr = Mock(return_value=([[text,.99]],None))
    assert v.gems(image) == (expected, .99 if expected is not None else 0.)


def test_unknown_dialog_and_loading_fail_closed():
    v,image = vision_without_models()
    assert v.supported(image)
    dialog = image.copy()
    dialog[200:600,60:300] = 200
    assert not v.supported(dialog)
    assert not v.supported(np.zeros_like(image))
    assert v.path_supported((170,300),(170,500))
    assert not v.path_supported((250,20),(250,20))


def test_capture_client_coordinates_failure_and_cleanup(monkeypatch):
    capture = capture_module.WindowCapture.__new__(capture_module.WindowCapture)
    capture.hwnd = 1
    capture.calibrated_size = (360,780)
    capture.last_capture_ok = True
    capture.get_window_rect = lambda: (100,200,360,780)
    gui,ui,ctypes = Mock(),Mock(),Mock()
    gui.IsIconic.return_value = False
    ctypes.windll.user32.PrintWindow.return_value = 0
    monkeypatch.setattr(capture_module,'win32gui',gui)
    monkeypatch.setattr(capture_module,'win32ui',ui)
    monkeypatch.setattr(capture_module,'ctypes',ctypes)
    with pytest.raises(RuntimeError,match='PrintWindow'):
        capture.capture()
    assert not capture.last_capture_ok
    gui.GetDC.assert_called_with(1)
    gui.ReleaseDC.assert_called_once()
    gui.DeleteObject.assert_called_once()
    capture.calibrated_size = (361,780)
    with pytest.raises(RuntimeError,match='size changed'):
        capture.capture()


def test_loading_never_reads_ocr_and_failed_capture_blocks():
    capture,mouse,vision = Mock(),Mock(),Mock()
    vision.supported.return_value = False
    capture.capture.return_value = np.zeros((780,360,3),np.uint8)
    interface = LiveInterface(capture,mouse,vision,Controls(paused=False))
    _,value,confidence,supported = interface.snapshot()
    assert (value,confidence,supported) == (None,0.,False)
    vision.gems.assert_not_called()
    capture.capture.side_effect = RuntimeError('capture failed')
    assert not interface.ready()
    assert not interface.execute(0)
    mouse.click.assert_not_called()


def test_noninteractive_calibration_confidence_gate(tmp_path, monkeypatch):
    import learning.vision as module
    capture = Mock()
    capture.capture.return_value = np.ones((780,360,3),np.uint8)
    engine = Mock()
    engine.gems.return_value = (123,.94)
    monkeypatch.setattr(module,'Vision',Mock(return_value=engine))
    path = tmp_path/'calibration.json'
    with pytest.raises(ValueError,match='confidence'):
        module.calibrate(capture,path,[220,30,60,25],[[70,200,200,300]])
    assert not json.loads(path.read_text())['ocr_verified']
    engine.gems.return_value = (123,.99)
    module.calibrate(capture,path,[220,30,60,25],[[70,200,200,300]])
    assert json.loads(path.read_text())['ocr_verified']
    with pytest.raises(ValueError,match='outside'):
        module.calibrate(capture,path,[350,30,60,25],[[70,200,200,300]])
