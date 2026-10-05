import base64
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import signal
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import portal


PASSWORD = "portal-test-password-123"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-runtime-image"


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.active = 0
        self.maximum_active = 0
        self.delay = 0

    def call(self, name, *args):
        self.calls.append((name, args))
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            time.sleep(self.delay)
            if self.failure and name != "close":
                raise self.failure
            return {"connected": True, "readiness": {"calibrated": True}, "action": name}
        finally:
            self.active -= 1

    def status(self):
        return self.call("status")

    def screenshot(self):
        self.call("screenshot")
        return PNG

    def pair(self, endpoint, code):
        result = self.call("pair", endpoint, code)
        result.update(code=code, detail=f"paired using {code}")
        return result

    def connect(self, endpoint, package):
        return self.call("connect", endpoint, package)

    def disconnect(self):
        return self.call("disconnect")

    def start(self):
        return self.call("start")

    def stop(self):
        return self.call("stop")

    def calibrate(self, payload):
        return self.call("calibrate", payload)

    def verify(self, payload):
        return self.call("verify", payload)

    def close(self):
        return self.call("close")


class Client:
    def __init__(self, server):
        self.server = server
        self.host = f"127.0.0.1:{server.server_port}"
        self.origin = "http://" + self.host
        self.auth = "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()

    def request(self, path="/api/status", method="GET", payload=None, headers=None, raw=None, duplicates=()):
        defaults = {"Host": self.host, "Authorization": self.auth}
        if method == "POST":
            defaults.update({"Origin": self.origin, "X-CSRF-Token": self.server.csrf_token,
                             "Content-Type": "application/json"})
            if raw is None:
                raw = json.dumps({} if payload is None else payload).encode()
        defaults.update(headers or {})
        defaults = {key: value for key, value in defaults.items() if value is not None}
        if raw is not None and "Content-Length" not in defaults:
            defaults["Content-Length"] = str(len(raw))
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for key, value in defaults.items():
                connection.putheader(key, value)
            for key, value in duplicates:
                connection.putheader(key, value)
            connection.endheaders(raw)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()


@pytest.fixture
def live_portal():
    runtime = FakeRuntime()
    # Bind first, then configure the dynamically allocated localhost authority.
    server = portal.PortalServer(("127.0.0.1", 0), runtime, portal.Config(PASSWORD))
    server.config = portal.Config(PASSWORD, (f"127.0.0.1:{server.server_port}",))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield Client(server), runtime, server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.close_runtime()
        server.server_close()


@pytest.mark.parametrize("path", ["/", "/portal.js", "/api/session", "/api/status", "/api/screenshot", "/api/stop"])
@pytest.mark.parametrize("auth", [None, "Basic !!!!", "Basic YWRtaW46/w==", "Bearer token",
                                  "Basic " + base64.b64encode(b"other:portal-test-password-123").decode(),
                                  "Basic " + base64.b64encode(b"admin:wrong-password").decode()])
def test_every_resource_requires_strict_auth(live_portal, path, auth):
    client, runtime, _ = live_portal
    status, headers, body = client.request(path, "POST" if path == "/api/stop" else "GET",
                                           headers={"Authorization": auth})
    assert status == 401
    assert headers["WWW-Authenticate"] == 'Basic realm="Eatventure Portal", charset="UTF-8"'
    assert headers["Cache-Control"] == "no-store"
    assert b"wrong-password" not in body
    assert runtime.calls == []


def test_status_session_image_and_security_headers(live_portal):
    client, runtime, server = live_portal
    status, headers, body = client.request()
    data = json.loads(body)
    assert status == 200
    assert data["csrf_token"] == server.csrf_token
    assert data["readiness"] == {"calibrated": True}
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "unsafe-inline" not in headers["Content-Security-Policy"]
    assert json.loads(client.request("/api/session")[2]) == {"csrf_token": server.csrf_token}
    assert runtime.calls == [("status", ())]
    status, headers, body = client.request("/api/screenshot")
    assert (status, headers["Content-Type"], body) == (200, "image/png", PNG)
    assert runtime.calls[-1] == ("screenshot", ())
    assert client.request("/")[0] == 200
    js = client.request("/portal.js")[2]
    assert b"setTimeout(poll, 5000)" in js
    assert b"setTimeout(image, 3000)" in js
    assert b"connected && !document.hidden" in js
    assert b"pointer" not in js and b"keydown" not in js


@pytest.mark.parametrize("headers", [
    {"X-CSRF-Token": None}, {"X-CSRF-Token": "wrong"},
    {"Origin": None}, {"Origin": "null"}, {"Origin": "http://evil.example"},
    {"Origin": "http://localhost:8080"}, {"Origin": "http://127.0.0.1:8080.evil.example"},
])
def test_post_requires_csrf_and_exact_host_origin(live_portal, headers):
    client, runtime, _ = live_portal
    assert client.request("/api/stop", "POST", headers=headers)[0] == 403
    assert not runtime.calls


def test_host_allowlist_and_proxy_origin(live_portal):
    client, runtime, server = live_portal
    for host in ("evil.example", client.host + ".evil", None):
        assert client.request(headers={"Host": host})[0] == 400
    assert not runtime.calls
    assert client.request("/api/stop", "POST", headers={"Origin": "https://" + client.host})[0] == 200
    server.config = portal.Config(PASSWORD, (client.host,), "https://portal.example")
    assert client.request("/api/stop", "POST")[0] == 403
    assert client.request("/api/stop", "POST", headers={"Origin": "https://portal.example"})[0] == 200
    for origin in ("https://portal.example/", "http://portal.example", "https://portal.example.evil"):
        assert client.request("/api/stop", "POST", headers={"Origin": origin})[0] == 403


@pytest.mark.parametrize("header,status", [("Host", 400), ("Authorization", 401),
                                           ("Origin", 403), ("X-CSRF-Token", 403),
                                           ("Content-Length", 400), ("Content-Type", 415)])
def test_duplicate_security_headers_are_rejected(live_portal, header, status):
    client, runtime, server = live_portal
    values = {"Host": client.host, "Authorization": client.auth, "Origin": client.origin,
              "X-CSRF-Token": server.csrf_token, "Content-Length": "2",
              "Content-Type": "application/json"}
    assert client.request("/api/stop", "POST", duplicates=[(header, values[header])])[0] == status
    assert not runtime.calls


@pytest.mark.parametrize("raw,headers,expected", [
    (b"{", {}, 400), (b"[]", {}, 400), (b"null", {}, 400),
    (b'{"negative":true,"negative":false,"approve":true}', {}, 400),
    (b'{"negative":NaN,"approve":true}', {}, 400), (b"\xff", {}, 400),
    (b"[" * 2000, {}, 400),
    (b"{}", {"Content-Type": "text/plain"}, 415),
    (b"{}", {"Content-Type": None}, 415),
    (b"{}", {"Content-Length": "-1"}, 400),
    (b"{}", {"Content-Length": "999999999999999"}, 400),
    (b"{}", {"Content-Length": str(portal.MAX_BODY + 1)}, 413),
    (b"{}", {"Transfer-Encoding": "chunked"}, 400),
    (b"x" * (portal.MAX_BODY + 1), {}, 413),
], ids=[f"body-{n}" for n in range(14)])
def test_malformed_bodies_never_reach_runtime(live_portal, raw, headers, expected):
    client, runtime, _ = live_portal
    assert client.request("/api/verify", "POST", raw=raw, headers=headers)[0] == expected
    assert not runtime.calls


@pytest.mark.parametrize("path,payload", [
    ("/api/pair", {"endpoint": "phone:37123", "code": "123456"}),
    ("/api/connect", {"endpoint": "phone:41234", "package": "com.example.game"}),
    ("/api/disconnect", {}), ("/api/start", {}), ("/api/stop", {}),
    ("/api/calibrate", {"gems_roi": [0, 1, 20, 30], "interaction_rois": [[1, 2, 3, 4]],
                        "animation_rois": [], "cash_roi": [1, 2, 10, 10]}),
    ("/api/verify", {"negative": True, "approve": False}),
])
def test_explicit_actions_delegate_only_documented_arguments(live_portal, path, payload):
    client, runtime, _ = live_portal
    status, _, body = client.request(path, "POST", payload=payload)
    assert status == 200
    name = path.split("/")[-1]
    args = (tuple(payload[key] for key in ("endpoint", "code")) if name == "pair"
            else tuple(payload[key] for key in ("endpoint", "package")) if name == "connect"
            else (payload,) if name in ("calibrate", "verify") else ())
    assert runtime.calls == [(name, args)]
    if name == "pair":
        assert b"123456" not in body and "code" not in json.loads(body)


@pytest.mark.parametrize("path,payload", [
    ("/api/pair", {"endpoint": "phone:1", "code": "123", "args": ["shell"]}),
    ("/api/pair", {"endpoint": "x" * 257, "code": "123"}),
    ("/api/pair", {"endpoint": "phone:1", "code": "x" * 129}),
    ("/api/pair", {"endpoint": "phone\n:1", "code": "123"}),
    ("/api/connect", {"endpoint": "phone:1", "package": 123}),
    ("/api/connect", {"endpoint": "phone:1"}),
    ("/api/stop", {"coordinates": [1, 2]}),
    ("/api/start", {"path": "/tmp/file"}),
    ("/api/verify", {"negative": 0, "approve": True}),
    ("/api/verify", {"negative": False}),
    ("/api/calibrate", {"gems_roi": [True, 1, 2, 3], "interaction_rois": [[0, 0, 1, 1]], "animation_rois": []}),
    ("/api/calibrate", {"gems_roi": [0, 0, 0, 3], "interaction_rois": [[0, 0, 1, 1]], "animation_rois": []}),
    ("/api/calibrate", {"gems_roi": [0, 0, 2, 3], "interaction_rois": [], "animation_rois": []}),
    ("/api/calibrate", {"gems_roi": [0, 0, 2, 3], "interaction_rois": [[0, 0, 1, 1]] * 33, "animation_rois": []}),
    ("/api/calibrate", {"gems_roi": [0, 0, 2, 3], "interaction_rois": [[0, 0, 1, 1]], "animation_rois": [], "cash_roi": None}),
])
def test_schema_rejects_extra_keys_and_invalid_types(live_portal, path, payload):
    client, runtime, _ = live_portal
    assert client.request(path, "POST", payload=payload)[0] == 400
    assert not runtime.calls


def test_route_and_method_whitelist(live_portal):
    client, runtime, _ = live_portal
    for path in ("/api/shell", "/api/input", "/../config.py", "/api/status?code=secret"):
        assert client.request(path)[0] == 404
    for path, method in (("/api/start", "GET"), ("/api/status", "POST"), ("/api/status", "DELETE")):
        assert client.request(path, method)[0] == 405
    assert not runtime.calls


@pytest.mark.parametrize("exception,status", [(ValueError, 400), (RuntimeError, 409), (OSError, 500)])
def test_runtime_errors_and_logs_never_leak_code(live_portal, capsys, exception, status):
    client, runtime, _ = live_portal
    secret = "pairing-secret-918273"
    runtime.failure = exception(f"secret {secret}, password {PASSWORD}")
    response, _, body = client.request("/api/pair", "POST", payload={"endpoint": "phone:1", "code": secret})
    assert response == status
    assert secret.encode() not in body and PASSWORD.encode() not in body
    client.request("/api/status?code=" + secret)
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert PASSWORD not in captured.out + captured.err


def test_pair_rate_limit_is_shared_and_counts_failed_attempts(live_portal, monkeypatch):
    client, runtime, _ = live_portal
    runtime.failure = ValueError("rejected")
    monkeypatch.setattr(portal.time, "monotonic", lambda: 100)
    for _ in range(5):
        assert client.request("/api/pair", "POST", payload={"endpoint": "phone:1", "code": "123456"})[0] == 400
    other_client = Client(client.server)
    assert other_client.request("/api/pair", "POST", payload={"endpoint": "PHONE:1", "code": "999999"})[0] == 429
    assert len(runtime.calls) == 5
    assert client.request("/api/pair", "POST", payload={"endpoint": "phone:2", "code": "123456"})[0] == 400
    monkeypatch.setattr(portal.time, "monotonic", lambda: 161)
    assert client.request("/api/pair", "POST", payload={"endpoint": "phone:1", "code": "123456"})[0] == 400


def test_runtime_calls_are_serialized_and_close_is_idempotent(live_portal):
    client, runtime, server = live_portal
    runtime.delay = 0.02
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(client.request, "/api/stop", "POST") for _ in range(6)]
        assert all(future.result()[0] == 200 for future in futures)
    assert runtime.maximum_active == 1
    server.close_runtime()
    server.close_runtime()
    assert [name for name, _ in runtime.calls].count("close") == 1
    assert client.request("/api/start", "POST")[0] == 409


def test_tokens_are_per_instance(live_portal):
    _, _, server = live_portal
    other = portal.PortalServer(("127.0.0.1", 0), FakeRuntime(), portal.Config(PASSWORD))
    try:
        assert server.csrf_token != other.csrf_token
    finally:
        other.close_runtime()
        other.server_close()


@pytest.mark.parametrize("password", ["", "short", "a" * 15])
def test_password_configuration_has_no_insecure_default(password):
    with pytest.raises(ValueError):
        portal.Config(password)


@pytest.mark.parametrize("origin", ["https://portal.example/", "https://user@portal.example",
                                    "ftp://portal.example", "https://portal.example?q=1"])
def test_proxy_origin_configuration_is_exact(origin):
    with pytest.raises(ValueError):
        portal.Config(PASSWORD, public_origin=origin)


def test_cli_rejects_missing_auth_configuration_before_runtime_import(monkeypatch):
    monkeypatch.delenv("PORTAL_PASSWORD", raising=False)
    with pytest.raises(SystemExit) as error:
        portal.main([])
    assert error.value.code == 2


def test_main_sigterm_closes_runtime_and_shuts_down_from_worker(monkeypatch):
    runtime = FakeRuntime()
    handlers = {}
    workers = []
    shutdown_threads = []
    original_server = portal.PortalServer

    class TestServer(original_server):
        def serve_forever(self, poll_interval=0.5):
            def stop():
                time.sleep(0.05)
                handlers[signal.SIGTERM](signal.SIGTERM, None)
            worker = threading.Thread(target=stop)
            workers.append(worker)
            worker.start()
            return super().serve_forever(poll_interval=0.02)

        def shutdown(self):
            shutdown_threads.append(threading.current_thread())
            return super().shutdown()

    def register(signum, handler):
        previous = handlers.get(signum, signal.SIG_DFL)
        handlers[signum] = handler
        return previous

    monkeypatch.setenv("PORTAL_PASSWORD", PASSWORD)
    monkeypatch.setattr(portal, "PortalServer", TestServer)
    monkeypatch.setattr(portal.signal, "signal", register)
    monkeypatch.setitem(sys.modules, "portal_runtime", SimpleNamespace(Runtime=lambda: runtime))
    portal.main(["--host", "127.0.0.1", "--port", "0"])
    for worker in workers:
        worker.join(timeout=5)
    assert runtime.calls == [("close", ())]
    assert len(shutdown_threads) == 1
    assert shutdown_threads[0] is not threading.current_thread()
    assert handlers == {signal.SIGINT: signal.SIG_DFL, signal.SIGTERM: signal.SIG_DFL}
