"""Authenticated, view-only browser portal for portal_runtime.Runtime.

Run with PORTAL_PASSWORD (at least 16 characters) set in the environment.
PORTAL_ALLOWED_HOSTS is a comma-separated list of HTTP Host authorities;
PORTAL_PUBLIC_ORIGIN optionally pins POST requests to one exact proxy origin.
No runtime or device modules are imported until main has validated configuration.
"""

from __future__ import annotations

import argparse
import base64
import binascii
from collections import deque
from dataclasses import dataclass
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import secrets
import signal
import threading
import time
from urllib.parse import urlsplit


MAX_BODY = 32 * 1024
POST_ROUTES = {
    "/api/pair": "pair", "/api/connect": "connect",
    "/api/disconnect": "disconnect", "/api/start": "start",
    "/api/stop": "stop", "/api/calibrate": "calibrate",
    "/api/verify": "verify",
}
GET_ROUTES = {"/", "/portal.js", "/api/session", "/api/status", "/api/screenshot", "/api/calibration-preview"}


def _authority(value: str) -> bool:
    if not value or re.search(r"[\s/@?#\\]", value):
        return False
    try:
        parsed = urlsplit("http://" + value)
        return bool(parsed.hostname and parsed.netloc == value and not parsed.path
                    and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        return False


@dataclass(frozen=True)
class Config:
    password: str
    allowed_hosts: tuple[str, ...] = ("localhost:8080", "127.0.0.1:8080")
    public_origin: str | None = None

    def __post_init__(self):
        if len(self.password) < 16:
            raise ValueError("PORTAL_PASSWORD must contain at least 16 characters")
        if not self.allowed_hosts or any(not _authority(host) for host in self.allowed_hosts):
            raise ValueError("PORTAL_ALLOWED_HOSTS must contain valid Host authorities")
        if self.public_origin is not None:
            parsed = urlsplit(self.public_origin)
            if (parsed.scheme not in ("http", "https") or not _authority(parsed.netloc)
                    or self.public_origin != f"{parsed.scheme}://{parsed.netloc}"):
                raise ValueError("PORTAL_PUBLIC_ORIGIN must be an exact http(s) origin without a path")

    @classmethod
    def from_env(cls):
        return cls(
            os.environ.get("PORTAL_PASSWORD", ""),
            tuple(host.strip() for host in os.environ.get(
                "PORTAL_ALLOWED_HOSTS", "localhost:8080,127.0.0.1:8080").split(",")),
            os.environ.get("PORTAL_PUBLIC_ORIGIN") or None,
        )


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message


def _text(value, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(
        ord(c) < 32 or ord(c) == 127 for c in value)


def _roi(value) -> bool:
    return (isinstance(value, list) and len(value) == 4
            and all(type(n) is int for n in value)
            and all(0 <= n <= 65535 for n in value[:2])
            and all(1 <= n <= 65535 for n in value[2:]))


def validate_payload(route: str, payload: dict) -> None:
    valid = False
    if route == "/api/pair":
        valid = (set(payload) == {"endpoint", "code"}
                 and _text(payload["endpoint"], 256) and _text(payload["code"], 128))
    elif route == "/api/connect":
        valid = (set(payload) == {"endpoint", "package"}
                 and _text(payload["endpoint"], 256) and _text(payload["package"], 256))
    elif route in ("/api/disconnect", "/api/start", "/api/stop"):
        valid = not payload
    elif route == "/api/verify":
        valid = (set(payload) == {"negative", "approve"}
                 and all(type(v) is bool for v in payload.values()))
    elif route == "/api/calibrate":
        valid = ({"gems_roi", "interaction_rois", "animation_rois"} <= set(payload)
                 <= {"gems_roi", "interaction_rois", "animation_rois", "cash_roi"}
                 and _roi(payload["gems_roi"])
                 and ("cash_roi" not in payload or _roi(payload["cash_roi"]))
                 and all(isinstance(payload[key], list)
                         and (1 if key == "interaction_rois" else 0) <= len(payload[key]) <= 32
                         and all(_roi(roi) for roi in payload[key])
                         for key in ("interaction_rois", "animation_rois")))
    if not valid:
        raise RequestError(400, "Invalid action parameters")


HTML = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Eatventure Portal</title><script src="/portal.js" defer></script></head>
<body><h1>Eatventure Portal</h1>
<p>Anmeldung: Benutzer admin, Portal-Passwort im Browser-Anmeldedialog.</p>
<p>Pairing und Verbindung verwenden unterschiedliche Android-Ports. Es erfolgt kein automatischer Start.</p>
<form id="pair"><h2>Pairing</h2><label>Pairing Host:Port <input name="endpoint" required maxlength="256"></label>
<label>Pairing-Code <input name="code" type="password" required maxlength="128" autocomplete="off"></label>
<button>Pairing durchführen</button></form>
<form id="connect"><h2>Verbindung</h2><label>Verbindungs-Host:Port <input name="endpoint" required maxlength="256"></label>
<label>Android-Paket <input name="package" required maxlength="256" value="com.hwqgrhhjfd.idlefastfood"></label>
<button>Verbinden</button></form>
<p><button id="disconnect">Trennen</button> <button id="start">Start</button> <button id="stop">Stop</button></p>
<p id="message" role="status"></p><h2>Status</h2><pre id="status"></pre>
<h2>Bild (nur Ansicht)</h2><img id="screen" alt="Spielansicht, keine Eingabesteuerung">
<h2>Kalibrierung</h2><p>Rechtecke: x, y, Breite, Höhe. Mehrere Rechtecke durch Semikolon trennen.
Nur vom Runtime freigegebene Spielansichten und Interaktionsbereiche sind zulässig.</p>
<form id="calibrate"><label>Gems ROI <input name="gems_roi" required placeholder="10,20,100,30"></label><br>
<label>Interaktionsrechtecke <input name="interaction_rois" required placeholder="10,100,200,300;220,100,100,300"></label><br>
<label>Animationsrechtecke <input name="animation_rois" placeholder="10,100,200,300"></label><br>
<label>Cash ROI (optional) <input name="cash_roi" placeholder="10,50,100,30"></label><br>
<button>Kalibrierung übermitteln</button></form>
<p><a href="/api/calibration-preview" target="_blank" rel="noopener">Alle acht Referenzbilder prüfen</a>:
Freigabe nur bestätigen, wenn jedes Bild normales Gameplay zeigt. Keine Shops, Käufe oder Dialoge als Referenz verwenden.</p>
<form id="verify"><h2>Zustandsprobe sammeln / prüfen</h2>
<label><input name="negative" type="checkbox">Negativprobe (nicht unterstützte Ansicht)</label>
<label><input name="approve" type="checkbox">Freigabe bestätigen</label><button>Probe prüfen</button></form>
</body></html>"""

JAVASCRIPT = """'use strict';
let csrf = '', connected = false, imageURL = null;
const el = id => document.getElementById(id);
async function request(path, payload) {
  const options = {credentials: 'same-origin', cache: 'no-store'};
  if (payload !== undefined) Object.assign(options, {method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify(payload)});
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Anfrage fehlgeschlagen');
  return data;
}
async function status() {
  try {
    const data = await request('/api/status');
    csrf = data.csrf_token;
    connected = data.connected === true || data.connection?.connected === true;
    const display = {...data}; delete display.csrf_token;
    el('status').textContent = JSON.stringify(display, null, 2);
  } catch (e) { connected = false; el('message').textContent = e.message; }
}
async function action(name, payload) {
  try {
    if (!csrf) csrf = (await request('/api/session')).csrf_token;
    await request('/api/' + name, payload);
    el('message').textContent = 'Aktion abgeschlossen';
    await status();
  } catch (e) { el('message').textContent = e.message; }
}
for (const name of ['pair', 'connect']) el(name).addEventListener('submit', event => {
  event.preventDefault(); const data = Object.fromEntries(new FormData(event.target));
  if (name === 'pair') event.target.elements.code.value = '';
  action(name, data);
});
for (const name of ['disconnect', 'start', 'stop']) el(name).addEventListener('click', () => action(name, {}));
const roi = value => {
  const parts = value.split(',').map(x => x.trim());
  if (parts.length !== 4 || parts.some(x => !/^\\d+$/.test(x))) throw new Error('Rechteck benötigt vier ganze Zahlen');
  return parts.map(Number);
};
const rois = value => value.trim() ? value.split(';').map(roi) : [];
el('calibrate').addEventListener('submit', event => {
  event.preventDefault();
  try {
    const fields = Object.fromEntries(new FormData(event.target));
    const data = {gems_roi: roi(fields.gems_roi), interaction_rois: rois(fields.interaction_rois),
      animation_rois: rois(fields.animation_rois)};
    if (fields.cash_roi.trim()) data.cash_roi = roi(fields.cash_roi);
    action('calibrate', data);
  } catch (e) { el('message').textContent = e.message; }
});
el('verify').addEventListener('submit', event => {
  event.preventDefault(); action('verify', {negative: event.target.elements.negative.checked,
    approve: event.target.elements.approve.checked});
});
async function image() {
  try {
    if (connected && !document.hidden) {
      const response = await fetch('/api/screenshot', {credentials: 'same-origin', cache: 'no-store'});
      if (response.ok) {
        const next = URL.createObjectURL(await response.blob());
        el('screen').src = next;
        if (imageURL) URL.revokeObjectURL(imageURL);
        imageURL = next;
      }
    } else {
      el('screen').removeAttribute('src');
      if (imageURL) URL.revokeObjectURL(imageURL);
      imageURL = null;
    }
  } catch (e) { el('message').textContent = 'Bildabruf fehlgeschlagen'; }
  finally { setTimeout(image, 3000); }
}
async function poll() { await status(); setTimeout(poll, 5000); }
poll(); image();
"""


class PortalServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = True

    def __init__(self, address, runtime, config: Config):
        self.runtime = runtime
        self.config = config
        self.csrf_token = secrets.token_urlsafe(32)
        self.operation_lock = threading.Lock()
        self.shutdown_lock = threading.Lock()
        self.stop_lock = threading.Lock()
        self.thread_slots = threading.BoundedSemaphore(16)
        self.pair_attempts: dict[str, deque] = {}
        self.redactions = {config.password}
        self.closed_runtime = False
        super().__init__(address, PortalHandler)

    def close_runtime(self):
        with self.shutdown_lock:
            if not self.closed_runtime:
                self.closed_runtime = True
                try:
                    self.runtime.close()
                except Exception:
                    raise RuntimeError("Runtime shutdown failed") from None

    def process_request(self, request, address):
        if not self.thread_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.thread_slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.thread_slots.release()

    def redact(self, value):
        if isinstance(value, dict):
            return {self.redact(k): self.redact(v) for k, v in value.items()
                    if k.lower() not in {"code", "pairing_code", "password", "authorization"}}
        if isinstance(value, (list, tuple)):
            return [self.redact(v) for v in value]
        if isinstance(value, str):
            for secret in tuple(self.redactions):
                value = value.replace(secret, "[redacted]")
        return value

    def invoke(self, route, payload=None):
        # Emergency stop/status must not queue behind a slow calibration/ADB call.
        if route in ("/api/stop", "/api/status"):
            if self.closed_runtime:
                raise RequestError(409, "Runtime is closed")
            try:
                if route == "/api/stop":
                    with self.stop_lock:
                        return self.redact(self.runtime.stop())
                return self.redact(self.runtime.status())
            except Exception:
                raise RequestError(409, "Runtime operation unavailable") from None
        with self.operation_lock:
            if self.closed_runtime:
                raise RequestError(409, "Runtime is closed")
            if route == "/api/pair":
                now = time.monotonic()
                for endpoint in list(self.pair_attempts):
                    attempts = self.pair_attempts[endpoint]
                    while attempts and attempts[0] <= now - 60:
                        attempts.popleft()
                    if not attempts:
                        del self.pair_attempts[endpoint]
                endpoint = payload["endpoint"].strip().lower()
                if endpoint not in self.pair_attempts and len(self.pair_attempts) >= 1024:
                    raise RequestError(429, "Pairing rate limit exceeded")
                attempts = self.pair_attempts.setdefault(endpoint, deque())
                if len(attempts) >= 5:
                    raise RequestError(429, "Pairing rate limit exceeded")
                attempts.append(now)
                self.redactions.add(payload["code"])
            try:
                if route == "/api/status":
                    result = self.runtime.status()
                elif route == "/api/screenshot":
                    return self.runtime.screenshot()
                elif route == "/api/calibration-preview":
                    return self.runtime.calibration_preview()
                else:
                    method = getattr(self.runtime, POST_ROUTES[route])
                    if route == "/api/pair":
                        result = method(payload["endpoint"], payload["code"])
                    elif route == "/api/connect":
                        result = method(payload["endpoint"], payload["package"])
                    elif route in ("/api/calibrate", "/api/verify"):
                        result = method(payload)
                    else:
                        result = method()
                return self.redact(result)
            except ValueError:
                raise RequestError(400, "Runtime rejected the request") from None
            except RuntimeError:
                raise RequestError(409, "Runtime is not ready for this operation") from None
            except Exception:
                raise RequestError(500, "Runtime operation failed") from None


class PortalHandler(BaseHTTPRequestHandler):
    server_version = "Portal"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # BaseHTTPRequestHandler logs the raw request line: never log it or credentials.
        pass

    def _send(self, status, body, content_type="application/json; charset=utf-8", headers=None):
        # Drain bounded rejected bodies before closing, avoiding TCP resets on Windows.
        # Never parse these bytes; do not wait on an untrusted declared large length.
        if not getattr(self, "body_read", False):
            length = self._single("Content-Length") if hasattr(self, "headers") else None
            if length is not None or (hasattr(self, "headers") and
                                      (self.headers.get_all("Content-Length") or self.command == "POST")):
                self.connection.settimeout(0.2)
                try:
                    if length and re.fullmatch(r"[0-9]{1,10}", length):
                        self.rfile.read(min(int(length), MAX_BODY + 1))
                    else:
                        self.rfile.read1(MAX_BODY + 1)
                except OSError:
                    pass
                finally:
                    self.body_read = True
                    self.connection.settimeout(10)
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=True, allow_nan=False).encode()
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; img-src 'self' blob:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        self._send(code, {"error": "Invalid HTTP request"})

    def _single(self, name):
        values = self.headers.get_all(name, [])
        return values[0] if len(values) == 1 else None

    def _authorized(self):
        header = self._single("Authorization")
        if not header or not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8", errors="strict")
            user, password = decoded.split(":", 1)
        except (ValueError, UnicodeError, binascii.Error):
            return False
        return (hmac.compare_digest(user.encode(), b"admin")
                & hmac.compare_digest(password.encode(), self.server.config.password.encode()))

    def _body(self):
        if self.headers.get_all("Transfer-Encoding"):
            raise RequestError(400, "Transfer encoding is not supported")
        length = self._single("Content-Length")
        if length is None or not re.fullmatch(r"[0-9]{1,10}", length):
            raise RequestError(400, "Invalid content length")
        length = int(length)
        if length > MAX_BODY:
            raise RequestError(413, "Request body is too large")
        content_type = self._single("Content-Type")
        if not content_type or content_type.lower() != "application/json":
            raise RequestError(415, "Content-Type must be application/json")
        try:
            raw = self.rfile.read(length)
            self.body_read = True
            if len(raw) != length:
                raise ValueError
            def unique_pairs(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result
            def invalid_constant(value):
                raise ValueError
            data = json.loads(raw.decode("utf-8", errors="strict"),
                              object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, UnicodeError, RecursionError, TimeoutError):
            raise RequestError(400, "Invalid JSON object") from None

    def _dispatch(self):
        try:
            host = self._single("Host")
            if host not in self.server.config.allowed_hosts:
                raise RequestError(400, "Host is not allowed")
            if not self._authorized():
                self._send(401, {"error": "Authentication required"}, headers={
                    "WWW-Authenticate": 'Basic realm="Eatventure Portal", charset="UTF-8"'})
                return
            if self.path not in GET_ROUTES | POST_ROUTES.keys():
                raise RequestError(404, "Route not found")
            if self.command == "GET" and self.path in GET_ROUTES:
                if self.path == "/":
                    self._send(200, HTML, "text/html; charset=utf-8")
                elif self.path == "/portal.js":
                    self._send(200, JAVASCRIPT, "text/javascript; charset=utf-8")
                elif self.path == "/api/session":
                    self._send(200, {"csrf_token": self.server.csrf_token})
                elif self.path == "/api/status":
                    data = self.server.invoke(self.path)
                    self._send(200, {**data, "csrf_token": self.server.csrf_token})
                else:
                    image = self.server.invoke(self.path)
                    if not isinstance(image, bytes) or not image.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise RequestError(409, "Screenshot unavailable")
                    self._send(200, image, "image/png")
            elif self.command == "POST" and self.path in POST_ROUTES:
                token = self._single("X-CSRF-Token")
                if token is None or not hmac.compare_digest(token.encode(), self.server.csrf_token.encode()):
                    raise RequestError(403, "CSRF token is invalid")
                origins = ({self.server.config.public_origin} if self.server.config.public_origin
                           else {"http://" + host, "https://" + host})
                if self._single("Origin") not in origins:
                    raise RequestError(403, "Origin is not allowed")
                payload = self._body()
                validate_payload(self.path, payload)
                self._send(200, self.server.invoke(self.path, payload))
            else:
                raise RequestError(405, "Method is not allowed")
        except RequestError as error:
            self._send(error.status, {"error": error.message})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            self._send(500, {"error": "Portal operation failed"})

    do_GET = _dispatch
    do_POST = _dispatch
    do_HEAD = _dispatch
    do_PUT = _dispatch
    do_PATCH = _dispatch
    do_DELETE = _dispatch
    do_OPTIONS = _dispatch


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    try:
        config = Config.from_env()
        if not 0 <= args.port <= 65535:
            raise ValueError("Port must be between 0 and 65535")
    except ValueError as error:
        parser.error(str(error))
    from portal_runtime import Runtime

    runtime = Runtime()
    try:
        server = PortalServer((args.host, args.port), runtime, config)
    except BaseException:
        runtime.close()
        raise
    stopping = threading.Event()

    def shutdown():
        try:
            server.close_runtime()
        finally:
            server.shutdown()

    def request_shutdown(signum=None, frame=None):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=shutdown, name="portal-shutdown").start()

    old_handlers = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, request_shutdown)
        print(f"Portal listening on {args.host}:{server.server_port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            request_shutdown()
    finally:
        try:
            server.close_runtime()
        finally:
            server.server_close()
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)


if __name__ == "__main__":
    main()
