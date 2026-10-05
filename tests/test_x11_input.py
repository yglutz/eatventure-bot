"""XTest is mocked; never sends real mouse events on any test runner."""
import builtins
import importlib
import sys
import threading
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import Mock

import pytest

import mouse_controller
from mouse_controller import MouseController


@pytest.fixture
def desktop(monkeypatch):
    events = []
    X = NS(MotionNotify=6, ButtonPress=4, ButtonRelease=5)
    xlib = ModuleType('Xlib')
    xlib.X = X
    ext = ModuleType('Xlib.ext')
    xtest = ModuleType('Xlib.ext.xtest')
    xtest.fake_input = Mock(side_effect=lambda display, event, **kwargs: events.append((event, kwargs)))
    monkeypatch.setitem(sys.modules, 'Xlib', xlib)
    monkeypatch.setitem(sys.modules, 'Xlib.ext', ext)
    monkeypatch.setitem(sys.modules, 'Xlib.ext.xtest', xtest)
    capture = Mock()
    capture._transport_error = None
    capture.calibrated_size = (360, 780)
    capture.last_capture_ok = True
    capture.lock = threading.RLock()
    capture.get_window_rect.return_value = (30, 40, 360, 780)
    capture.is_window_active.return_value = True
    capture.input_ready.return_value = True
    mouse = MouseController(desktop_capture=capture, guard=lambda: True, click_delay=0)
    mouse.path_guard = lambda a, b: True  # Synthetic calibrated safe region.
    return NS(mouse=mouse, capture=capture, events=events, fake=xtest.fake_input)


def test_default_denied_and_mutually_exclusive_backend(desktop):
    mouse = MouseController(desktop_capture=desktop.capture)
    assert not mouse.click(100, 400)
    assert not desktop.events
    with pytest.raises(ValueError):
        MouseController(desktop_capture=desktop.capture, capture=Mock())
    with pytest.raises(ValueError):
        MouseController(desktop_capture=desktop.capture, hwnd=123)


def test_calibrated_region_guard_required(desktop):
    mouse = MouseController(desktop_capture=desktop.capture, guard=lambda: True)
    assert not mouse.click(100, 400)
    assert not desktop.events


def test_click_absolute_mapping_audit_and_capabilities(desktop):
    observer = Mock(return_value='token')
    desktop.mouse.observer = observer
    assert desktop.mouse.click(130, 440, relative=False)
    assert desktop.events == [(6, {'x': 130, 'y': 440, 'root': desktop.capture.root}),
                              (4, {'detail': 1}), (5, {'detail': 1})]
    observer.assert_any_call('before', 'click', (100., 400.), (100., 400.), .04, None)
    observer.assert_any_call('after', 'click', (100., 400.), (100., 400.), .04,
                             ('token', True, True))
    assert desktop.mouse.supports_action('hold') and desktop.mouse.supports_action('drag')
    assert not desktop.mouse.pressed


def test_drag_interpolation_fresh_capture_only_once(desktop):
    assert desktop.mouse.drag(100, 400, 220, 500, duration=.06)
    motions = [kw for event, kw in desktop.events if event == 6]
    assert len(motions) >= 3
    assert (motions[-1]['x'], motions[-1]['y']) == (250, 540)
    desktop.capture.input_ready.assert_called_once()
    assert desktop.events[-1] == (5, {'detail': 1})


@pytest.mark.parametrize('start,end', [
    ((100, 668), (100, 668)), ((60, 150), (60, 150)),
    ((290, 100), (290, 100)), ((140, 80), (210, 80)),
    ((54, 700), (286, 700)), ((100, 667.6), (100, 667.6)),
    ((-1, 400), (100, 400)), ((float('nan'), 400), (100, 400)),
    ((360, 400), (100, 400)),
])
def test_static_and_continuous_rounded_path_rejection(desktop, start, end):
    assert not desktop.mouse.drag(*start, *end, duration=0)
    assert not desktop.events


def test_path_mask_capture_and_observer_pause_deny(desktop):
    desktop.mouse.path_guard = lambda a, b: False
    assert not desktop.mouse.click(100, 400)
    desktop.mouse.path_guard = lambda a, b: True
    desktop.capture.input_ready.return_value = False
    assert not desktop.mouse.click(100, 400)
    desktop.capture.input_ready.return_value = True
    def pause(*args):
        if args[0] == 'before':
            desktop.mouse.guard = lambda: False
    desktop.mouse.observer = pause
    assert not desktop.mouse.click(100, 400)
    assert not desktop.events


@pytest.mark.parametrize('failure', ['focus', 'process', 'size', 'origin', 'guard', 'capture'])
def test_hold_cancels_releases_and_no_adb_polling(desktop, failure):
    original = desktop.fake.side_effect
    def dispatch(display, event, **kwargs):
        original(display, event, **kwargs)
        if event == 4:
            if failure in ('focus', 'process'):
                desktop.capture.is_window_active.return_value = False
            elif failure == 'size':
                desktop.capture.get_window_rect.return_value = (30, 40, 361, 780)
            elif failure == 'origin':
                desktop.capture.get_window_rect.return_value = (31, 40, 360, 780)
            elif failure == 'guard':
                desktop.mouse.guard = lambda: False
            else:
                desktop.capture.last_capture_ok = False
    desktop.fake.side_effect = dispatch
    assert not desktop.mouse.hold_at(100, 400, duration=.1)
    assert desktop.events[-1] == (5, {'detail': 1})
    assert not desktop.mouse.pressed
    desktop.capture.input_ready.assert_called_once()


def test_release_from_other_thread_during_long_hold(desktop):
    pressed = threading.Event()
    original = desktop.fake.side_effect
    def dispatch(display, event, **kwargs):
        original(display, event, **kwargs)
        if event == 4:
            pressed.set()
    desktop.fake.side_effect = dispatch
    results = []
    thread = threading.Thread(target=lambda: results.append(desktop.mouse.hold_at(100, 400, 5)))
    thread.start()
    try:
        assert pressed.wait(1)
        desktop.mouse.release()
        thread.join(1)
        assert not thread.is_alive()
        assert results == [False]
        assert not desktop.mouse.pressed
        assert sum(event == 5 for event, _ in desktop.events) == 1
    finally:
        desktop.mouse.guard = lambda: False
        desktop.mouse.release()
        thread.join(1)


def test_dispatch_and_release_errors_clear_state_disable_input(desktop):
    original = desktop.fake.side_effect
    def fail(display, event, **kwargs):
        original(display, event, **kwargs)
        if event in (4, 5):
            raise OSError('X11 disconnected')
    desktop.fake.side_effect = fail
    assert not desktop.mouse.hold_at(100, 400, duration=0)
    assert desktop.events[-1][0] == 5
    assert not desktop.mouse.pressed and not desktop.mouse.ready()


def test_absolute_origin_changed_during_capture_denies(desktop):
    def capture():
        desktop.capture.get_window_rect.return_value = (40, 40, 360, 780)
        return True
    desktop.capture.input_ready.side_effect = capture
    assert not desktop.mouse.click(130, 440, relative=False)
    assert not desktop.events


def test_lazy_xlib_import(desktop, monkeypatch):
    original = builtins.__import__
    def blocked(name, *args, **kwargs):
        if name.startswith('Xlib'):
            raise ImportError('Xlib unavailable on Windows')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', blocked)
    importlib.reload(mouse_controller)
    assert not desktop.mouse.click(100, 400)
    assert not desktop.mouse.pressed


@pytest.mark.parametrize('duration', [-1, float('nan'), float('inf')])
def test_invalid_duration(desktop, duration):
    assert not desktop.mouse.hold_at(100, 400, duration)
    assert not desktop.events


@pytest.mark.parametrize('action', ['click', 'move_to'])
def test_pause_during_slow_preparation_never_dispatches(desktop, action):
    entered, resume = threading.Event(), threading.Event()
    def prepare():
        entered.set()
        assert resume.wait(2)
        return True
    desktop.capture.input_ready.side_effect = prepare
    result = []
    thread = threading.Thread(target=lambda: result.append(getattr(desktop.mouse, action)(100, 400)))
    thread.start()
    try:
        assert entered.wait(1)
        assert desktop.mouse.lock.acquire(timeout=.2)
        try:
            desktop.mouse.release()
        finally:
            desktop.mouse.lock.release()
    finally:
        resume.set()
        thread.join(2)
    assert not thread.is_alive() and result == [False]
    assert not desktop.events


def test_context_exit_disables_input(desktop):
    with desktop.mouse as mouse:
        assert mouse.click(100, 400)
    desktop.events.clear()
    assert not desktop.mouse.click(100, 400)
    assert not desktop.events


def test_move_rechecks_calibrated_mask_after_capture(desktop):
    def prepare():
        desktop.mouse.path_guard = lambda a, b: False
        return True
    desktop.capture.input_ready.side_effect = prepare
    assert not desktop.mouse.move_to(100, 400)
    assert not desktop.events


def test_invalid_hold_not_hidden_by_duration_cap(desktop):
    desktop.mouse.max_hold_duration = .01
    assert not desktop.mouse.hold_at(100, 400, float('inf'))
    assert not desktop.events
