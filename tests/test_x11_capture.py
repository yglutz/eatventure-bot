"""Mock-only X11 tests: no X server, Android device or Linux required."""
import builtins
import importlib
import sys
import threading
from types import SimpleNamespace as NS, ModuleType
from unittest.mock import Mock

import numpy as np
import pytest

import x11_capture
from x11_capture import X11Capture, X11CaptureError


@pytest.fixture
def server(monkeypatch):
    X = NS(IsViewable=2, AnyPropertyType=0, TrueColor=4, LSBFirst=0, MSBFirst=1, ZPixmap=2)
    geometry = NS(width=360, height=780)
    attrs = NS(map_state=2)
    visual = NS(visual_id=42, visual_class=4, red_mask=0xff0000,
                green_mask=0xff00, blue_mask=0xff)
    fmt = NS(depth=24, bits_per_pixel=32, scanline_pad=32)
    info = NS(image_byte_order=0, pixmap_formats=[fmt])
    data = np.zeros((780, 360, 4), dtype=np.uint8)
    # Include letterbox-like black rows: must not crop/rescale them.
    data[10:770, :180, 0] = 255
    data[10:770, 180:, 2] = 255
    reply = NS(depth=24, visual=42, data=data.tobytes())
    window = Mock(id=11)
    window.get_wm_name.return_value = 'op'
    window.get_wm_class.return_value = ('scrcpy', 'scrcpy')
    window.get_attributes.return_value = attrs
    window.get_geometry.return_value = geometry
    window.get_image.return_value = reply
    window.query_tree.return_value = NS(children=[])
    root = Mock()
    root.query_tree.return_value = NS(children=[window])
    root.translate_coords.return_value = NS(x=30, y=40)
    display = Mock()
    display.screen.return_value = NS(root=root, allowed_depths=[NS(visuals=[visual])])
    display.display.info = info
    display.intern_atom.return_value = 0
    display.get_input_focus.return_value = NS(focus=window)
    module = ModuleType('Xlib')
    module.X = X
    module.display = NS(Display=Mock(return_value=display))
    monkeypatch.setitem(sys.modules, 'Xlib', module)
    guard = Mock()
    guard.transport.serial = '192.0.2.10:5555'
    guard.is_window_active.return_value = True
    monkeypatch.setattr(x11_capture, 'ADBCapture', Mock(return_value=guard))
    alive = Mock(return_value=True)
    capture = X11Capture(':9', serial='192.0.2.10:5555', expected_package='com.example.game',
                         process_alive=alive)
    return NS(capture=capture, display=display, window=window, root=root,
              geometry=geometry, attrs=attrs, reply=reply, fmt=fmt, visual=visual,
              info=info, guard=guard, alive=alive, module=module)


def test_capture_bgr_letterboxing_identity_no_hwnd(server):
    c = server.capture
    assert not hasattr(c, 'hwnd')
    with pytest.raises(X11CaptureError):
        c.calibration_identity()
    image = c.capture()
    assert image.shape == (780, 360, 3) and image.flags.c_contiguous
    assert tuple(image[0, 0]) == (0, 0, 0)
    assert tuple(image[20, 0]) == (255, 0, 0)
    assert tuple(image[20, 359]) == (0, 0, 255)
    assert c.capture(7).shape == (7, 360, 3)
    assert c.get_window_rect() == (30, 40, 360, 780)
    assert c.calibration_identity() == {
        'backend': 'scrcpy-x11', 'serial': '192.0.2.10:5555', 'package': 'com.example.game',
        'display_name': ':9', 'window_id': 11,
        'canonical_size': [360, 780], 'native_size': [360, 780],
        'coordinate_system': 'canonical-client-pixels', 'video_size': None}
    server.guard.capture.assert_not_called()
    server.window.get_image.assert_called_with(0, 0, 360, 780, 2, 0xffffffff)


@pytest.mark.parametrize('bits,endian,masks,value,expected', [
    (32, 0, (0xff0000, 0xff00, 0xff), 0x123456, (0x56, 0x34, 0x12)),
    (32, 1, (0xff, 0xff00, 0xff0000), 0x123456, (0x12, 0x34, 0x56)),
    (24, 0, (0xff0000, 0xff00, 0xff), 0x123456, (0x56, 0x34, 0x12)),
    (24, 1, (0xff0000, 0xff00, 0xff), 0x123456, (0x56, 0x34, 0x12)),
    (16, 0, (0xf800, 0x7e0, 0x1f), 0xf81f, (255, 0, 255)),
    (16, 1, (0xf800, 0x7e0, 0x1f), 0x07e0, (0, 255, 0)),
])
def test_server_pixel_formats_and_padding(server, bits, endian, masks, value, expected):
    server.fmt.bits_per_pixel = bits
    server.info.image_byte_order = endian
    server.visual.red_mask, server.visual.green_mask, server.visual.blue_mask = masks
    # Odd width forces padding for 16/24-bit formats.
    geometry = NS(width=3, height=2)
    row = value.to_bytes(bits // 8, 'little' if endian == 0 else 'big') * 3
    stride = ((3 * bits + 31) // 32) * 4
    server.reply.data = (row + b'\xaa' * (stride - len(row))) * 2
    image = server.capture._decode(server.reply, geometry)
    assert image.shape == (2, 3, 3)
    assert np.all(image == expected)


@pytest.mark.parametrize('failure', ['focus', 'minimized', 'process', 'android', 'uniform',
                                      'truncated', 'disconnect', 'duplicate', 'missing'])
def test_fail_closed_no_stale_frame(server, failure):
    assert server.capture.input_ready()
    if failure == 'focus':
        server.display.get_input_focus.return_value.focus = 99
    elif failure == 'minimized':
        server.attrs.map_state = 0
    elif failure == 'process':
        server.alive.return_value = False
    elif failure == 'android':
        server.guard.is_window_active.return_value = False
    elif failure == 'uniform':
        server.reply.data = bytes(780 * 360 * 4)
    elif failure == 'truncated':
        server.reply.data = b'bad'
    elif failure == 'disconnect':
        server.display.get_input_focus.side_effect = OSError('disconnected')
    elif failure == 'duplicate':
        other = Mock(wraps=server.window, id=12)
        server.root.query_tree.return_value.children.append(other)
    else:
        server.root.query_tree.return_value.children.clear()
    assert not server.capture.input_ready()
    assert not server.capture.last_capture_ok


def test_size_change_latches_and_resize_only_before_calibration(server):
    server.capture.resize_window()
    server.window.configure.assert_called_once_with(width=360, height=780)
    server.capture.capture()
    server.geometry.width = 361
    assert not server.capture.is_window_active()
    server.geometry.width = 360
    assert not server.capture.input_ready()
    with pytest.raises(X11CaptureError):
        server.capture.resize_window()


def test_live_guard_never_contacts_adb_and_capture_rechecks(server):
    server.capture.capture()
    server.guard.reset_mock()
    for _ in range(20):
        assert server.capture.is_window_active()
    server.guard.is_window_active.assert_not_called()
    server.guard.is_window_active.side_effect = [True, False]
    assert not server.capture.input_ready()


def test_lazy_import_windows_safe_and_missing_xlib(monkeypatch):
    original = builtins.__import__
    def blocked(name, *args, **kwargs):
        if name.startswith('Xlib'):
            raise ImportError('not installed')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', blocked)
    importlib.reload(x11_capture)
    with pytest.raises(RuntimeError, match='not installed'):
        x11_capture.X11Capture(':9', serial='test', expected_package='com.example.game',
                              process_alive=lambda: True)


def test_close_and_invalid_crop(server):
    with pytest.raises(ValueError):
        server.capture.capture(0)
    assert not server.capture.last_capture_ok
    server.capture.close()
    server.capture.close()
    server.display.close.assert_called_once()
    assert not server.capture.input_ready()


def test_slow_android_guard_does_not_hold_xlib_transport_lock(server):
    entered = threading.Event()
    resume = threading.Event()
    def android_check():
        entered.set()
        assert resume.wait(2)
        return True
    server.guard.is_window_active.side_effect = android_check
    result = []
    thread = threading.Thread(target=lambda: result.append(server.capture.input_ready()))
    thread.start()
    try:
        assert entered.wait(1)
        assert server.capture.lock.acquire(timeout=.2)
        server.capture.lock.release()
    finally:
        resume.set()
        thread.join(2)
    assert not thread.is_alive() and result == [True]


def test_process_exit_cannot_rebind_to_replacement(server):
    assert server.capture.input_ready()
    server.alive.return_value = False
    assert not server.capture.is_window_active()
    server.alive.return_value = True
    assert not server.capture.input_ready()


def test_process_guard_is_required():
    with pytest.raises(ValueError, match='liveness'):
        X11Capture(':9', serial='test', expected_package='com.example.game')


def test_ewmh_foreground_must_match_input_focus(server):
    assert server.capture.input_ready()
    server.display.intern_atom.side_effect = lambda name, **kw: 7 if name == '_NET_ACTIVE_WINDOW' else 0
    server.root.get_full_property.return_value = NS(value=[99])
    assert not server.capture.input_ready()


def test_asynchronous_x_error_latches_failure(server):
    handler = server.display.set_error_handler.call_args.args[0]
    server.display.sync.side_effect = lambda: handler(OSError('BadWindow'), None)
    with pytest.raises(x11_capture.X11CaptureError):
        server.capture.resize_window()
    assert not server.capture.input_ready()
