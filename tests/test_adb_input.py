"""No live input: captures and the serial-bound transport are mocked."""
from types import SimpleNamespace
from unittest.mock import Mock
import subprocess
import sys

import pytest

import mouse_controller as mouse
from safety import zones


@pytest.fixture
def adb(monkeypatch):
    transport = SimpleNamespace(adb_path='adb-test', serial='explicit-device',
                                _execute=Mock(return_value=b''))
    capture = SimpleNamespace(calibrated_size=(360, 780), native_size=(720, 1560),
                              input_ready=Mock(return_value=True), transport=transport)
    monkeypatch.setattr(mouse.time, 'sleep', lambda _: None)
    controller = mouse.MouseController(capture=capture, guard=lambda: True)
    controller.observer = Mock(return_value='audit-token')
    return controller, capture, transport


def test_serial_bound_tap_mapping_and_audit(adb):
    c, capture, transport = adb
    assert c.click(170, 300)
    transport._execute.assert_called_once_with(
        ['adb-test', '-s', 'explicit-device', 'shell', 'input', 'tap', '340', '600'])
    assert capture.input_ready.call_count == 2
    assert c.observer.call_args.args == (
        'after', 'click', (170., 300.), (170., 300.), .04, ('audit-token', True, True))
    assert not c.pressed
    c.release()
    assert transport._execute.call_count == 1


def test_default_guard_denies_without_capture_or_input(adb):
    _, capture, transport = adb
    c = mouse.MouseController(capture=capture)
    assert not c.click(170, 300)
    capture.input_ready.assert_not_called()
    transport._execute.assert_not_called()


def test_unsupported_actions_and_absolute_coordinates(adb):
    c, capture, transport = adb
    assert c.supports_action('click') and c.supports_action('tap')
    assert not c.supports_action('hold') and not c.supports_action('drag')
    assert not c.hold_at(170, 300)
    assert not c.drag(170, 200, 170, 380)
    assert not c.click(170, 300, relative=False)
    assert not c.move_to(170, 300)
    capture.input_ready.assert_not_called()
    transport._execute.assert_not_called()


@pytest.mark.parametrize('point', [(360, 300), (-1, 300), (170, float('nan')),
                                  (float('inf'), 300), (None, 300), ('bad', 300)])
def test_invalid_coordinates_fail_closed(adb, point):
    c, _, transport = adb
    assert not c.click(*point)
    transport._execute.assert_not_called()


def test_all_forbidden_boundaries(adb):
    c, _, transport = adb
    for left, right, top, bottom in zones():
        for x in (left, right):
            for y in (top, min(bottom, 779)):
                assert not c.click(x, y)
    transport._execute.assert_not_called()


@pytest.mark.parametrize('change', ['pause', 'focus', 'capture_error', 'native', 'canonical', 'mask'])
def test_observer_changes_rechecked_before_dispatch(adb, change):
    c, capture, transport = adb
    def observe(stage, *args):
        if stage == 'before':
            if change == 'pause':
                c.guard = lambda: False
            elif change == 'focus':
                capture.input_ready.return_value = False
            elif change == 'capture_error':
                capture.input_ready.side_effect = RuntimeError('capture failed')
            elif change == 'native':
                capture.native_size = (721, 1560)
            elif change == 'canonical':
                capture.calibrated_size = (361, 780)
            elif change == 'mask':
                c.path_guard = lambda a, b: False
        return 'token'
    c.observer = Mock(side_effect=observe)
    assert not c.click(170, 300)
    transport._execute.assert_not_called()
    assert c.observer.call_args.args[-1] == ('token', False, False)
    assert not c.pressed


def test_native_size_failure_latched(adb):
    c, capture, transport = adb
    capture.native_size = (721, 1560)
    assert not c.click(170, 300)
    capture.native_size = (720, 1560)
    assert not c.click(170, 300)
    transport._execute.assert_not_called()


def test_unpinned_capture_requires_fresh_success(adb):
    _, capture, transport = adb
    capture.native_size = None
    c = mouse.MouseController(capture=capture, guard=lambda: True)
    capture.input_ready.return_value = False
    assert not c.click(170, 300)
    capture.input_ready.return_value = True
    capture.native_size = (720, 1560)
    assert c.click(170, 300)
    assert c._native_size == (720, 1560)
    assert transport._execute.call_count == 1


def test_pause_during_fresh_capture(adb):
    c, capture, transport = adb
    def fresh():
        c.guard = lambda: False
        return True
    capture.input_ready.side_effect = fresh
    assert not c.click(170, 300)
    transport._execute.assert_not_called()


def test_rounding_into_forbidden_boundary_rejected(adb):
    _, capture, transport = adb
    # Coarse native grid rounds a safe canonical point onto the forbidden bottom.
    capture.native_size = (720, 3)
    c = mouse.MouseController(capture=capture, guard=lambda: True)
    assert not c.click(170, 600)  # rounds y to bottom forbidden area (779).
    transport._execute.assert_not_called()


def test_rounding_onto_inclusive_zone_edge_rejected(adb):
    _, capture, transport = adb
    capture.native_size = capture.calibrated_size
    c = mouse.MouseController(capture=capture, guard=lambda: True)
    assert not c.is_in_forbidden_zone(200.1, 80)
    assert c.is_in_forbidden_zone(200, 80)
    assert not c.click(200.1, 80)
    transport._execute.assert_not_called()


@pytest.mark.parametrize('x,native_x', [(0, '0'), (359, '719'), (359.9, '719')])
def test_endpoint_rounding_stays_on_native_screen(adb, x, native_x):
    c, _, transport = adb
    assert c.click(x, 300)
    assert transport._execute.call_args.args[0][-2:] == [native_x, '600']


def test_complete_rounding_segment_not_only_endpoints_checked(adb):
    c, _, transport = adb
    c.path_guard = Mock(side_effect=lambda a, b: a == b)
    assert not c.click(170, 300)
    assert any(call.args[0] != call.args[1] for call in c.path_guard.call_args_list)
    transport._execute.assert_not_called()


def test_reverse_mapping_and_complete_segment_use_path_guard(adb):
    c, _, transport = adb
    c.path_guard = Mock(side_effect=lambda a, b: a == b and a == (170., 300.))
    assert not c.click(170, 300)
    assert c.path_guard.call_count >= 2
    transport._execute.assert_not_called()


@pytest.mark.parametrize('error', [RuntimeError('device offline'), FileNotFoundError('adb'),
                                  subprocess.TimeoutExpired('adb', 1)])
def test_dispatch_failure_audited_without_retry(adb, error):
    c, _, transport = adb
    transport._execute.side_effect = error
    assert not c.click(170, 300)
    assert transport._execute.call_count == 1
    assert c.observer.call_args.args[-1] == ('audit-token', False, True)
    assert not c.pressed


def test_observer_errors_are_contained_and_release_runs(adb):
    c, _, transport = adb
    c.observer.side_effect = RuntimeError('audit unavailable')
    c.release = Mock(wraps=c.release)
    assert not c.click(170, 300)
    transport._execute.assert_not_called()
    c.release.assert_called_once()
    assert c.observer.call_count == 2


def test_after_observer_failure_does_not_hide_completed_tap(adb):
    c, _, transport = adb
    c.observer.side_effect = ['token', RuntimeError('audit unavailable')]
    assert c.click(170, 300)
    assert c.observer.call_args.args[-1] == ('token', True, True)
    assert transport._execute.call_count == 1


def test_import_without_win32_and_mocked_windows_constants():
    # Separate interpreter, so existing Win32 tests retain their patched modules.
    code = """
import sys
sys.modules.update({name: None for name in ('win32api', 'win32gui', 'win32con')})
import mouse_controller as m
assert m.win32api is None and m.win32gui is None
assert m.win32con.MOUSEEVENTF_LEFTDOWN == 2
assert m.win32con.MOUSEEVENTF_LEFTUP == 4
try:
    m.MouseController(1)
except RuntimeError:
    pass
else:
    raise AssertionError('Windows input must fail without pywin32')
"""
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                            timeout=30, shell=False)
    assert result.returncode == 0, result.stderr
