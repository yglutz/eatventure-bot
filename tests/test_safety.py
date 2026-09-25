import ast
import math
from pathlib import Path
from unittest.mock import Mock

import pytest
import mouse_controller as mouse
from safety import zones, forbidden, allowed, path_allowed


@pytest.mark.parametrize('zone', zones())
def test_all_inclusive_zone_boundaries(zone):
    a, b, c, d = zone
    d = min(d, 779)
    for x in (a, (a+b)/2, b):
        for y in (c, (c+d)/2, d):
            assert forbidden(x,y)
            assert not allowed((x,y), (360,780))
    for x,y in ((a-1,c-1), (b+1,d+1)):
        expected = any(l <= x <= r and t <= y <= bottom for l,r,t,bottom in zones())
        assert forbidden(x,y) == expected


def test_crossing_continuous_drag_and_bounds():
    assert not path_allowed((100,80), (250,80), (360,780))
    assert not path_allowed((100,64), (201,111), (360,780))
    assert path_allowed((170,200), (170,380), (360,780))
    assert not allowed((360,300), (360,780))
    assert not allowed((100,float('nan')), (360,780))


@pytest.fixture
def controller(monkeypatch):
    gui = Mock()
    gui.GetClientRect.return_value = (0,0,360,780)
    gui.GetForegroundWindow.return_value = 1
    gui.IsWindow.return_value = True
    gui.ClientToScreen.side_effect = lambda hwnd,p: (p[0]+100,p[1]+200)
    gui.ScreenToClient.side_effect = lambda hwnd,p: (p[0]-100,p[1]-200)
    api = Mock()
    monkeypatch.setattr(mouse, 'win32gui', gui)
    monkeypatch.setattr(mouse, 'win32api', api)
    monkeypatch.setattr(mouse.time, 'sleep', lambda _: None)
    return mouse.MouseController(1, 0), gui, api


def test_absolute_hold_and_drag_never_press_forbidden(controller):
    c, gui, api = controller
    for a,b,y,z in zones():
        point = (int(a), int(y))
        assert not c.click(*point)
        assert not c.hold_at(point[0]+100, point[1]+200, relative=False)
    assert not c.drag(200,280,350,280, relative=False)
    api.mouse_event.assert_not_called()


def test_valid_conversion_and_release_on_error(controller):
    c, gui, api = controller
    assert c.click(270,500,relative=False)
    api.SetCursorPos.assert_called_with((270,500))
    assert not c.pressed
    api.SetCursorPos.side_effect = [None, RuntimeError('failure')]
    with pytest.raises(RuntimeError):
        c.hold_at(170,300)
    assert not c.pressed
    assert api.mouse_event.call_args.args[0] == mouse.win32con.MOUSEEVENTF_LEFTUP


def test_focus_resize_guard_prevent_press(controller):
    c, gui, api = controller
    gui.GetForegroundWindow.return_value = 2
    assert not c.click(170,300)
    gui.GetForegroundWindow.return_value = 1
    gui.GetClientRect.return_value = (0,0,361,780)
    assert not c.click(170,300)
    gui.GetClientRect.return_value = (0,0,360,780)
    c.guard = lambda: False
    assert not c.click(170,300)
    api.mouse_event.assert_not_called()


def test_focus_loss_during_hold_releases(controller):
    c, gui, api = controller
    gui.GetForegroundWindow.side_effect = [1,1,1,2]
    assert not c.hold_at(170,300)
    assert not c.pressed
    assert api.mouse_event.call_args.args[0] == mouse.win32con.MOUSEEVENTF_LEFTUP


def test_hold_uses_elapsed_time_including_safety_checks(controller, monkeypatch):
    c, gui, api = controller
    clock = [0.]
    events = []
    def guard():
        clock[0] += .03
        return True
    c.guard = guard
    c.max_hold_duration = 1.
    monkeypatch.setattr(mouse.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(mouse.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    api.mouse_event.side_effect = lambda *args: events.append(clock[0])
    assert c.hold_at(170,300,duration=3.5)
    assert 1. <= events[-1]-events[0] <= 1.05
    api.mouse_event.reset_mock()
    with pytest.raises(ValueError):
        c.hold_at(170,300,duration=float('nan'))
    api.mouse_event.assert_not_called()


def test_no_direct_input_bypasses():
    prohibited = {'mouse_event', 'SendInput', 'PostMessage', 'SendMessage', 'SetCursorPos'}
    root = Path(__file__).resolve().parents[1]
    for path in [*root.glob('*.py'), *root.joinpath('learning').glob('*.py')]:
        if path.name == 'mouse_controller.py':
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'))
        assert not any(isinstance(n, ast.Attribute) and n.attr in prohibited for n in ast.walk(tree)), path
