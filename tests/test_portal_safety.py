"""Bounded lifecycle races using fake captures: no ADB or gameplay input."""
import base64
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import signal
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

import portal
import portal_runtime


PASSWORD = 'safety-test-strong-password-123'


@pytest.fixture
def runtime(tmp_path):
    events = []
    capture = SimpleNamespace(
        capture=lambda: np.zeros((24, 16, 3), dtype=np.uint8),
        calibration_identity=lambda: {'session': 'current'},
        close=lambda: events.append('capture.close'))
    mouse = SimpleNamespace(release=lambda: events.append('release'),
                            close=lambda: events.append('mouse.close'))
    vision = SimpleNamespace(supported=lambda image: True,
                             data={'source': {'session': 'current'}},
                             path_supported=lambda *args: True)

    def forbidden(*args, **kwargs):
        pytest.fail('Unexpected subprocess/device access')

    instance = portal_runtime.Runtime(
        root=tmp_path, allowed_hosts=[], run=forbidden, popen=forbidden,
        bot_factory=lambda **kwargs: SimpleNamespace(running=False))
    # Quiesce the real worker; race tests schedule _worker explicitly if needed.
    instance.shutdown.set()
    instance.worker.join(timeout=1)
    assert not instance.worker.is_alive()
    instance.shutdown.clear()
    instance.capture, instance.mouse, instance.vision = capture, mouse, vision
    instance.processes = [SimpleNamespace(
        poll=lambda: None, terminate=lambda: events.append('terminate'),
        wait=lambda **kwargs: events.append('wait'))]
    instance.candidate = tmp_path / 'calibration.json'
    instance.test_events = events
    try:
        yield instance
    finally:
        instance.close()


def test_stop_and_status_bypass_both_calibration_locks(runtime):
    runtime.active.set()
    server = portal.PortalServer(('127.0.0.1', 0), runtime, portal.Config(PASSWORD))
    entered, finish = threading.Event(), threading.Event()

    def calibration(payload):
        with runtime.operation_lock, runtime.step_lock:
            entered.set()
            assert finish.wait(2)
            return {'approved': False}

    runtime.calibrate = calibration
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            slow = pool.submit(server.invoke, '/api/calibrate', {})
            try:
                assert entered.wait(1)
                assert pool.submit(server.invoke, '/api/stop', {}).result(timeout=.5) == {'running': False}
                status = pool.submit(server.invoke, '/api/status').result(timeout=.5)
                assert not status['running']
                assert runtime.test_events == ['release']
                assert not slow.done()
            finally:
                finish.set()
            slow.result(timeout=1)
    finally:
        server.server_close()


def test_restart_waits_for_old_handler_and_old_handler_cannot_dispatch(runtime, monkeypatch):
    entered, finish, attempted = threading.Event(), threading.Event(), threading.Event()
    old_bot = SimpleNamespace(running=True)

    def handler():
        entered.set()
        assert finish.wait(2)
        attempted.set()
        # Model the MouseController guard at dispatch; no input is sent.
        assert not runtime._guard()
        assert not old_bot.running

    class OneTick:
        ticks = 0

        def is_set(self):
            return False

        def wait(self, timeout):
            if timeout == .2:
                self.ticks += 1
                return self.ticks > 1
            return False

    old_bot.state_machine = SimpleNamespace(update=handler)
    monkeypatch.setattr(runtime, '_guard', runtime.active.is_set)
    monkeypatch.setattr(runtime, 'shutdown', OneTick())
    runtime.bot = old_bot
    runtime.active.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(runtime._worker)
        try:
            assert entered.wait(1)
            runtime.stop()
            restart = pool.submit(runtime.start)
            with pytest.raises(TimeoutError):
                restart.result(timeout=.1)
        finally:
            finish.set()
        old.result(timeout=1)
        assert restart.result(timeout=1)['running']
    assert attempted.is_set() and runtime.bot is not old_bot


def test_close_releases_before_waiting_for_handler_or_tearing_down(runtime):
    entered, finish = threading.Event(), threading.Event()

    def handler():
        with runtime.step_lock:
            entered.set()
            assert finish.wait(2)
            assert not runtime.active.is_set()

    runtime.active.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(handler)
        try:
            assert entered.wait(1)
            closing = pool.submit(runtime.close)
            with pytest.raises(TimeoutError):
                closing.result(timeout=.1)
            assert runtime.test_events == ['release', 'release']
            assert runtime.shutdown.is_set()
        finally:
            finish.set()
        old.result(timeout=1)
        closing.result(timeout=1)
    assert runtime.test_events.index('release') < runtime.test_events.index('mouse.close')
    assert runtime.test_events.index('mouse.close') < runtime.test_events.index('capture.close')
    assert runtime.test_events.index('capture.close') < runtime.test_events.index('terminate')


@pytest.mark.parametrize('raises', [False, True])
def test_failed_explicit_approval_disables_previous_vision(runtime, monkeypatch, raises):
    def verify(*args, **kwargs):
        assert runtime.vision is None
        if raises:
            raise RuntimeError('Verification failed')
        return False

    monkeypatch.setitem(sys.modules, 'learning.calibration', SimpleNamespace(verify_calibration=verify))
    monkeypatch.setitem(sys.modules, 'learning.vision', SimpleNamespace(Vision=lambda *a, **kw: None))
    if raises:
        with pytest.raises(RuntimeError):
            runtime.verify({'negative': False, 'approve': True})
    else:
        assert runtime.verify({'negative': False, 'approve': True})['approved'] is False
    assert runtime.vision is None and not runtime.active.is_set()
    with pytest.raises(RuntimeError):
        runtime.start()


def test_accepted_negative_invalidates_prior_approval(runtime, monkeypatch):
    from learning.calibration import _save
    runtime.candidate.write_text(json.dumps({'ocr_verified': True, 'readiness_verified': True}))
    monkeypatch.setitem(sys.modules, 'learning.calibration', SimpleNamespace(verify_calibration=None, _save=_save))
    monkeypatch.setitem(sys.modules, 'learning.vision', SimpleNamespace(
        Vision=lambda *a, **kw: SimpleNamespace(supported=lambda frame: True)))
    with pytest.raises(RuntimeError, match='Unsafe view was accepted'):
        runtime.verify({'negative': True, 'approve': False})
    assert runtime.vision is None
    metadata = json.loads(runtime.candidate.read_text())
    assert not metadata['ocr_verified'] and not metadata['readiness_verified']
    with pytest.raises(RuntimeError):
        runtime.start()


def test_stop_during_start_capture_cannot_be_undone(runtime):
    entered, finish = threading.Event(), threading.Event()
    original = runtime.capture.capture

    def capture():
        entered.set()
        assert finish.wait(2)
        return original()

    runtime.capture.capture = capture
    with ThreadPoolExecutor(max_workers=1) as pool:
        starting = pool.submit(runtime.start)
        try:
            assert entered.wait(1)
            assert runtime.stop() == {'running': False}
        finally:
            finish.set()
        try:
            starting.result(timeout=1)
        except RuntimeError:
            pass  # A cancelled start is an acceptable implementation.
    assert not runtime.active.is_set()


def test_old_approved_source_is_rejected_and_disconnect_clears_candidate(runtime):
    runtime.vision.data['source'] = {'session': 'previous'}
    with pytest.raises(RuntimeError, match='source changed'):
        runtime.start()
    runtime.disconnect()
    assert runtime.candidate is runtime.vision is None
    with pytest.raises(RuntimeError, match='candidate'):
        runtime.verify({'negative': False, 'approve': True})


def test_default_main_http_startup_is_view_only_and_sanitizes_pair_errors(monkeypatch):
    """Run the actual main/server/auth/shutdown path, with no phone or input stack."""
    calls, handlers, errors = [], {}, []
    original_server = portal.PortalServer

    class FakeRuntime:
        def status(self):
            calls.append('status')
            return {'connected': False, 'running': False, 'learning': False, 'approved': False}

        def screenshot(self):
            calls.append('screenshot')
            return b'\x89PNG\r\n\x1a\nview-only-fake'

        def pair(self, endpoint, code):
            calls.append('pair')
            raise RuntimeError(f'{PASSWORD} {code}')

        def close(self):
            calls.append('close')

    def request(server, path, payload=None):
        auth = base64.b64encode(f'admin:{PASSWORD}'.encode()).decode()
        headers = {'Host': '127.0.0.1:8080', 'Authorization': 'Basic ' + auth}
        body = None
        if payload is not None:
            body = json.dumps(payload)
            headers.update({'Origin': 'http://127.0.0.1:8080',
                            'X-CSRF-Token': server.csrf_token, 'Content-Type': 'application/json'})
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=1)
        try:
            connection.request('POST' if payload is not None else 'GET', path, body, headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    class Server(original_server):
        def serve_forever(self, poll_interval=.5):
            def exercise():
                try:
                    code, body = request(self, '/api/status')
                    assert code == 200
                    data = json.loads(body)
                    assert not any(data[key] for key in ('connected', 'running', 'learning', 'approved'))
                    assert request(self, '/api/screenshot')[0] == 200
                    assert request(self, '/api/input', {'x': 1, 'y': 1})[0] == 404
                    code, body = request(self, '/api/pair', {'endpoint': '192.168.1.40:5555', 'code': '938271'})
                    assert code == 409
                    assert PASSWORD.encode() not in body and b'938271' not in body
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    handlers[signal.SIGTERM](signal.SIGTERM, None)

            client = threading.Thread(target=exercise)
            client.start()
            super().serve_forever(poll_interval=.01)
            client.join(timeout=1)
            assert not client.is_alive()

    def register(signum, handler):
        previous = handlers.get(signum, signal.SIG_DFL)
        handlers[signum] = handler
        return previous

    monkeypatch.setenv('PORTAL_PASSWORD', PASSWORD)
    monkeypatch.delenv('PORTAL_ALLOWED_HOSTS', raising=False)
    monkeypatch.delenv('PORTAL_PUBLIC_ORIGIN', raising=False)
    monkeypatch.setitem(sys.modules, 'portal_runtime', SimpleNamespace(Runtime=FakeRuntime))
    monkeypatch.setattr(portal, 'PortalServer', Server)
    monkeypatch.setattr(portal.signal, 'signal', register)
    portal.main(['--host', '127.0.0.1', '--port', '0'])
    assert not errors, errors
    assert calls == ['status', 'screenshot', 'pair', 'close']
