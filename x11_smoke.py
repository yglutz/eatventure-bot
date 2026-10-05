"""Opt-in real-X11 diagnostic against an OWNED synthetic window, never a phone.

Run on Linux in an isolated container with --network none:
    /opt/venv/bin/python /app/x11_smoke.py --run-isolated-synthetic

No existing DISPLAY is used. No ADB subprocess, scrcpy process, device, or
network connection is needed. All synthetic mouse input uses MouseController.
Xlib is imported inside main only, so importing this module is Windows-safe.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-isolated-synthetic', action='store_true',
                        help='Explicitly opt in to an owned Xvfb and synthetic GUI')
    args = parser.parse_args(argv)
    if not args.run_isolated_synthetic:
        parser.error('--run-isolated-synthetic is required; no existing display is used')
    if sys.platform != 'linux':
        parser.error('Run this diagnostic in the Linux legacy Docker image')

    process = owner = window = mouse = capture = thread = None
    evidence = []
    errors = []
    display_name = ':98'
    try:
        from Xlib import X, Xatom, display
        import numpy as np
        import x11_capture
        from mouse_controller import MouseController

        def require(condition, message):
            if not condition:
                raise RuntimeError(message)

        # Never connect to someone else's :98, including during startup races.
        require(not Path('/tmp/.X98-lock').exists()
                and not Path('/tmp/.X11-unix/X98').exists(),
                'Private display :98 already exists; refusing to attach')
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(
                ['Xvfb', display_name, '-screen', '0', '800x1000x24',
                 '-nolisten', 'tcp', '-noreset', '-ac'],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                env={**os.environ, 'DISPLAY': display_name})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log.seek(0)
                    raise RuntimeError('Owned Xvfb exited: ' + log.read().decode(errors='replace'))
                try:
                    owner = display.Display(display_name)
                    break
                except Exception:
                    time.sleep(.05)
            require(owner is not None and process.poll() is None, 'Owned Xvfb startup timed out')
            require(int(Path('/tmp/.X98-lock').read_text().strip()) == process.pid,
                    'Display lock does not belong to the owned Xvfb process')
            require(owner.has_extension('XTEST'), 'Owned Xvfb lacks XTEST')
            root = owner.screen().root
            active_atom = owner.intern_atom('_NET_ACTIVE_WINDOW')
            window = root.create_window(
                40, 60, 360, 780, 0, owner.screen().root_depth,
                X.InputOutput, X.CopyFromParent, background_pixel=0,
                event_mask=X.ButtonPressMask | X.ButtonReleaseMask | X.PointerMotionMask)
            window.set_wm_name('x11-smoke-owned-synthetic')
            window.set_wm_class('scrcpy', 'scrcpy')
            window.map()
            owner.sync()
            red = window.create_gc(foreground=0xff0000)
            blue = window.create_gc(foreground=0x0000ff)
            window.fill_rectangle(red, 0, 0, 180, 780)
            window.fill_rectangle(blue, 180, 0, 180, 780)
            owner.sync()

            def focus_owned():
                window.set_input_focus(X.RevertToParent, X.CurrentTime)
                root.change_property(active_atom, Xatom.WINDOW, 32, [window.id])
                owner.sync()

            focus_owned()

            class NoDeviceAndroidGuard:
                """Diagnostic-only dummy, scoped below; NOT an application backend."""
                calls = 0

                def __init__(self, serial, expected_package, **kwargs):
                    require(serial == 'simulation' and expected_package == 'simulation.game',
                            'Dummy guard accepts only synthetic identity')
                    self.transport = SimpleNamespace(serial=serial)

                def is_window_active(self):
                    type(self).calls += 1
                    return True

            def drain():
                owner.sync()
                events = []
                while owner.pending_events():
                    event = owner.next_event()
                    if event.type in (X.ButtonPress, X.ButtonRelease, X.MotionNotify):
                        require(event.window.id == window.id, 'Input reached a non-owned window')
                        require(not event.send_event, 'Expected actual XTest, not SendEvent')
                        events.append({'type': event.type, 'x': event.event_x,
                                       'y': event.event_y, 'server_time_ms': event.time})
                return events

            def verify_pair(name, events, start, end):
                buttons = [e for e in events if e['type'] in (X.ButtonPress, X.ButtonRelease)]
                require([e['type'] for e in buttons] == [X.ButtonPress, X.ButtonRelease],
                        f'{name}: expected exactly one real down/up pair: {events}')
                require((buttons[0]['x'], buttons[0]['y']) == start
                        and (buttons[1]['x'], buttons[1]['y']) == end,
                        f'{name}: wrong client coordinates: {events}')
                require(all(160 <= e['x'] <= 180 and 200 <= e['y'] <= 380 for e in events),
                        f'{name}: input left synthetic safe gameplay rectangle')
                require(not root.query_pointer().mask & X.Button1Mask,
                        f'{name}: server button remains held')
                evidence.append({'check': name, 'events': events, 'button_released': True})

            external_attempts = []

            def forbid_external(*args, **kwargs):
                external_attempts.append(str(args))
                raise RuntimeError('No external subprocess permitted during synthetic capture/input')

            # The guard replacement is strictly local to this executable test.
            with (patch.object(x11_capture, 'ADBCapture', NoDeviceAndroidGuard),
                  patch.object(subprocess, 'run', forbid_external),
                  patch.object(subprocess, 'Popen', forbid_external)):
                def new_controller():
                    c = x11_capture.X11Capture(
                        display_name, 'x11-smoke-owned-synthetic', serial='simulation',
                        expected_package='simulation.game', process_alive=lambda: True)

                    def supported_view():
                        try:
                            image = c.capture_view()
                            return (image.shape == (780, 360, 3)
                                    and tuple(image[100, 100]) == (0, 0, 255)
                                    and tuple(image[100, 250]) == (255, 0, 0))
                        except x11_capture.X11CaptureError:
                            return False

                    m = MouseController(desktop_capture=c, guard=supported_view, click_delay=0)
                    m.path_guard = lambda a, b: all(
                        160 <= p[0] <= 180 and 200 <= p[1] <= 380 for p in (a, b))
                    return c, m

                capture, mouse = new_controller()
                require(capture.capture_view().shape == (780, 360, 3), 'Desktop-only view failed')
                require(NoDeviceAndroidGuard.calls == 0 and capture.native_size is None
                        and not capture.last_capture_ok, 'View established Android eligibility')
                frame = capture.capture()
                require(frame.dtype == np.uint8 and frame.flags.c_contiguous
                        and frame.shape == (780, 360, 3), 'Incorrect frame layout')
                require(np.all(frame[:, :180] == (0, 0, 255))
                        and np.all(frame[:, 180:] == (255, 0, 0)),
                        'Actual X11 frame decode does not match the painted pattern')
                evidence.append({'check': 'actual_frame_decode', 'shape': list(frame.shape),
                                 'left_bgr': frame[100, 100].tolist(),
                                 'right_bgr': frame[100, 250].tolist(),
                                 'source': capture.calibration_identity()})
                drain()
                for name, action, start, end in (
                    ('click', lambda: mouse.click(170, 200), (170, 200), (170, 200)),
                    ('hold_0.05s', lambda: mouse.hold_at(170, 200, .05), (170, 200), (170, 200)),
                    ('drag_0.1s', lambda: mouse.drag(170, 200, 170, 380, .1), (170, 200), (170, 380)),
                ):
                    require(action(), f'{name}: controller returned failure')
                    events = drain()
                    verify_pair(name, events, start, end)
                    if name == 'hold_0.05s':
                        buttons = [e for e in events if e['type'] in (X.ButtonPress, X.ButtonRelease)]
                        held_ms = (buttons[1]['server_time_ms'] - buttons[0]['server_time_ms']) % (2**32)
                        require(held_ms >= 50, f'Hold released early: {held_ms} ms')
                        evidence[-1]['held_ms'] = held_ms
                require(mouse.is_in_forbidden_zone(170, 700), 'Expected static bottom forbidden zone')
                require(not mouse.drag(170, 200, 170, 700, .1), 'Forbidden drag accepted')
                require(not drain(), 'Forbidden drag dispatched input')
                evidence.append({'check': 'static_forbidden_drag', 'denied': True, 'events': []})

                # Lose focus after a real press, while an actual hold is active.
                result = []
                thread = threading.Thread(target=lambda: result.append(mouse.hold_at(170, 200, .5)))
                thread.start()
                seen = []
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    seen.extend(drain())
                    if any(e['type'] == X.ButtonPress for e in seen):
                        break
                    time.sleep(.005)
                require(any(e['type'] == X.ButtonPress for e in seen), 'Hold never dispatched press')
                with capture.lock:
                    root.set_input_focus(X.RevertToParent, X.CurrentTime)
                    root.change_property(active_atom, Xatom.WINDOW, 32, [0])
                    owner.sync()
                thread.join(2)
                require(not thread.is_alive() and result == [False], 'Focus loss did not cancel hold')
                seen.extend(drain())
                verify_pair('focus_loss_release', seen, (170, 200), (170, 200))
                require(not mouse.click(170, 200) and not drain(), 'Focus loss did not deny new input')
                mouse.close()
                capture.close()
                mouse = capture = None

                # A new source is necessary after the intentionally latched failure.
                focus_owned()
                capture, mouse = new_controller()
                capture.capture()
                window.configure(width=361)
                owner.sync()
                require(not mouse.click(170, 200), 'Changed client size accepted')
                require(not capture.last_capture_ok and not drain(), 'Size failure dispatched input')
                window.configure(width=360)
                owner.sync()
                require(not mouse.click(170, 200), 'Restoring size bypassed failure latch')
                evidence.append({'check': 'size_change', 'denied': True, 'latched': True, 'events': []})
                require(not external_attempts, 'Capture/input attempted an external subprocess')
                evidence.append({'check': 'no_device', 'adb_calls': 0,
                                 'external_subprocess_attempts': len(external_attempts),
                                 'dummy_guard_checks': NoDeviceAndroidGuard.calls})
    except Exception as exc:
        errors.append(f'{type(exc).__name__}: {exc}')
    finally:
        # Disable input before joining/releasing, and close Xvfb last.
        if mouse is not None:
            try:
                mouse.guard = lambda: False
                mouse.close()
            except Exception as exc:
                errors.append(f'Controller cleanup: {exc}')
        if thread is not None and thread.is_alive():
            thread.join(2)
            if thread.is_alive():
                errors.append('Gesture thread did not stop')
        if capture is not None:
            capture.close()
        if owner is not None:
            try:
                if window is not None:
                    window.destroy()
                owner.sync()
                owner.close()
            except Exception as exc:
                errors.append(f'Synthetic window cleanup: {exc}')
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
            except Exception as exc:
                errors.append(f'Xvfb cleanup: {exc}')
    print(json.dumps({'ok': not errors, 'display': display_name,
                      'synthetic_only': True, 'evidence': evidence, 'errors': errors}, indent=2))
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
