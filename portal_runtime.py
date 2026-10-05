"""Single-owner legacy scrcpy service. No training and no direct gameplay input.

Provisioning is IP-allowlisted. The browser sees images, never a remote input
surface. Xvfb and scrcpy stay private; every gameplay event uses MouseController.
"""
import ipaddress
import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import cv2
import numpy as np

import config
from learning.core import Gems, SIZE


class Runtime:
    def __init__(self, *, allowed_hosts=None, root='.', display_name=None,
                 popen=subprocess.Popen, run=subprocess.run,
                 capture_factory=None, mouse_factory=None, bot_factory=None):
        hosts = allowed_hosts if allowed_hosts is not None else os.getenv('ADB_ALLOWED_HOSTS', '').split(',')
        self.allowed_hosts = {str(ipaddress.IPv4Address(h.strip())) for h in hosts if h.strip()}
        self.root = Path(root).resolve()
        self.display_name = display_name or os.getenv('DISPLAY', ':99')
        if not re.fullmatch(r':[0-9]{1,3}', self.display_name):
            raise ValueError('A private local X11 display is required')
        self.popen, self.run = popen, run
        self.capture_factory, self.mouse_factory, self.bot_factory = capture_factory, mouse_factory, bot_factory
        self.operation_lock = threading.RLock()
        self.state_lock = threading.RLock()
        self.step_lock = threading.RLock()
        self.active = threading.Event()
        self.shutdown = threading.Event()
        self.cancel = threading.Event()
        self.capture = self.mouse = self.bot = self.vision = None
        self.endpoint = self.package = self.candidate = None
        self.processes = []
        self.negatives = []
        self.preview = None
        self.error = None
        self.phase = 'disconnected'
        self.audit = deque(maxlen=100)
        self.gems = Gems()
        self.stop_generation = 0
        self.worker = threading.Thread(target=self._worker, name='legacy-worker', daemon=True)
        self.worker.start()

    def _endpoint(self, endpoint):
        if not isinstance(endpoint, str) or not re.fullmatch(r'[0-9.]+:[0-9]{1,5}', endpoint):
            raise ValueError('An explicit allowed IPv4 address and port are required')
        host, port = endpoint.split(':')
        host = str(ipaddress.IPv4Address(host))
        address = ipaddress.IPv4Address(host)
        if host not in self.allowed_hosts or address.is_loopback or address.is_multicast or address.is_link_local or address.is_unspecified:
            raise ValueError('Device IP is not permitted')
        if not 1 <= int(port) <= 65535:
            raise ValueError('Invalid device port')
        return f'{host}:{int(port)}'

    def _adb(self, args, timeout=20):
        try:
            result = self.run(['adb', *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, check=False, shell=False)
        except (OSError, subprocess.TimeoutExpired):
            # Never propagate command-bearing exceptions (pairing code/password).
            raise RuntimeError('ADB provisioning timed out or is unavailable') from None
        if result.returncode != 0:
            raise RuntimeError('ADB provisioning failed; check authorization and address')
        return result.stdout.decode('utf-8', errors='replace').strip()

    def pair(self, endpoint, code):
        endpoint = self._endpoint(endpoint)
        if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code):
            raise ValueError('Pairing needs the current six-digit code')
        with self.operation_lock:
            self._require_open()
            if self.capture is not None:
                raise RuntimeError('Disconnect before pairing a device')
            message = self._adb(['pair', endpoint, code], timeout=30)
            if f'Successfully paired to {endpoint}' not in message:
                raise RuntimeError('Pairing was not confirmed')
            return {'paired': True, 'endpoint': endpoint}

    def _require_open(self):
        if self.shutdown.is_set():
            raise RuntimeError('Service is closed')

    def _alive(self):
        return bool(self.processes) and all(p.poll() is None for p in self.processes)

    def _spawn(self, args, env):
        proc = self.popen(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, shell=False)
        self.processes.append(proc)
        return proc

    def _wait(self, predicate, seconds=15):
        deadline = time.monotonic()+seconds
        while time.monotonic() < deadline:
            if self.cancel.is_set() or self.shutdown.is_set():
                raise RuntimeError('Operation cancelled')
            if not self._alive():
                raise RuntimeError('Desktop or scrcpy process exited')
            if predicate():
                return
            time.sleep(.2)
        raise RuntimeError('Desktop startup timed out')

    def connect(self, endpoint, package):
        endpoint = self._endpoint(endpoint)
        if not isinstance(package, str) or not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+', package, re.ASCII):
            raise ValueError('An explicit foreground Android package is required')
        with self.operation_lock:
            self._require_open()
            self._disconnect()
            self.cancel.clear()
            self.phase = 'connecting'
            try:
                reply = self._adb(['connect', endpoint])
                if reply not in (f'connected to {endpoint}', f'already connected to {endpoint}'):
                    raise RuntimeError('ADB connection was not confirmed')
                if self._adb(['-s', endpoint, 'get-state']) != 'device':
                    raise RuntimeError('Device is not authorized and online')
                if self.cancel.is_set():
                    raise RuntimeError('Operation cancelled')
                env = os.environ.copy()
                env.update(DISPLAY=self.display_name, SDL_RENDER_DRIVER='software')
                self._spawn(['Xvfb', self.display_name, '-screen', '0', '640x960x24',
                             '-nolisten', 'tcp', '-noreset', '-ac'], env)
                def display_ready():
                    result = self.run(['xdpyinfo', '-display', self.display_name],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      timeout=2, check=False, shell=False)
                    return result.returncode == 0
                self._wait(display_ready)
                self._spawn(['openbox', '--sm-disable'], env)
                self._spawn(['scrcpy', '--serial', endpoint, '--window-title', 'op',
                             '--window-width', '360', '--window-height', '780',
                             '--max-size', '800', '--max-fps', '10', '--video-bit-rate', '2M',
                             '--no-audio', '--no-power-on', '--render-driver', 'software',
                             '--no-clipboard-autosync'], env)
                if self.capture_factory is None:
                    from x11_capture import X11Capture
                    self.capture_factory = X11Capture
                def find_capture():
                    capture = None
                    try:
                        capture = self.capture_factory(self.display_name, 'op', serial=endpoint,
                                                       expected_package=package, process_alive=self._alive)
                        capture.resize_window()
                        self.capture = capture
                        return True
                    except RuntimeError:
                        if capture is not None:
                            capture.close()
                        return False
                self._wait(find_capture)
                # Allow WM geometry/focus and Android's new-connection banner to settle.
                deadline = time.monotonic()+12
                while time.monotonic() < deadline:
                    if self.cancel.wait(.2):
                        raise RuntimeError('Operation cancelled')
                self.capture.capture()
                if self.mouse_factory is None:
                    from mouse_controller import MouseController
                    self.mouse_factory = MouseController
                self.mouse = self.mouse_factory(desktop_capture=self.capture, guard=self._guard)
                self.mouse.observer = self._observe
                self.endpoint, self.package = endpoint, package
                self.error, self.phase = None, 'connected-stopped'
            except Exception:
                self._disconnect()
                self.error = 'connection_failed'
                raise RuntimeError('Connection/desktop startup failed; bot remains stopped') from None
            return self.status()

    def _observe(self, phase, kind, start, end, duration, result):
        if phase == 'after':
            _, completed, executed = result
            with self.state_lock:
                self.audit.append({'time': time.time(), 'kind': kind, 'completed': bool(completed),
                                   'executed': bool(executed)})
            if executed and not completed:
                self.active.clear()
                self.error = 'input_interrupted'

    def _guard(self):
        if not self.active.is_set() or self.shutdown.is_set() or self.vision is None or self.capture is None:
            return False
        try:
            # Fresh X11 pixels, not a cached accepted result. Full Android state is
            # checked by input_ready() before every gesture and by the worker.
            safe = self._alive() and self.capture.is_window_active() and self.vision.supported(self.capture.capture_view())
        except Exception:
            safe = False
        if not safe:
            self.active.clear()
            self.error = 'unsupported_or_capture_failed'
        return bool(safe and self.active.is_set())

    def status(self):
        with self.state_lock:
            return {'connected': self.capture is not None and self._alive(),
                    'running': self.active.is_set(), 'phase': self.phase, 'error': self.error,
                    'endpoint': self.endpoint, 'package': self.package,
                    'calibration': str(self.candidate.relative_to(self.root)) if self.candidate else None,
                    'approved': self.vision is not None, 'negative_samples': len(self.negatives),
                    'gems': self.gems.last, 'actions': list(self.audit)[-10:],
                    'mode': 'legacy-scrcpy', 'learning': False}

    def screenshot(self):
        if self.capture is None:
            raise RuntimeError('No connected scrcpy view')
        try:
            image = self.capture.capture()
            ok, data = cv2.imencode('.png', image)
            if not ok:
                raise RuntimeError('Image encoding failed')
            return data.tobytes()
        except Exception:
            self.stop()
            self.error = 'capture_failed'
            raise RuntimeError('Screenshot unavailable; bot stopped') from None

    def calibration_preview(self):
        if self.preview is None:
            raise RuntimeError('No reviewed candidate yet')
        return self.preview

    def _storage(self):
        # No budget cleanup/deletions: reject collection when full.
        folders = [self.root/'models'/'portal', self.root/'screenshots'/'portal']
        total = sum(p.stat().st_size for folder in folders if folder.exists()
                    for p in folder.rglob('*') if p.is_file())
        if total >= 512*1024*1024:
            raise RuntimeError('Calibration storage budget reached; files retained')

    def calibrate(self, payload):
        self.stop()
        with self.operation_lock, self.step_lock:
            self._require_open()
            self.stop()
            if self.capture is None or not payload.get('animation_rois'):
                raise RuntimeError('Connect and specify reviewed animation rectangles first')
            from learning.calibration import create_animation_calibration
            self.vision = None
            self._storage()
            directory = self.root/'screenshots'/'portal'/uuid.uuid4().hex
            directory.mkdir(parents=True, exist_ok=False)
            refs, frames = [], []
            for i in range(8):
                self._storage()
                image = self.capture.capture()
                path = directory/f'approved-{i:03d}.png'
                if not cv2.imwrite(str(path), image):
                    raise RuntimeError('Could not save reference')
                refs.append(path)
                frames.append(image)
                time.sleep(.5)
            sheet = np.vstack([np.hstack(frames[i:i+4]) for i in (0,4)])
            ok, image = cv2.imencode('.png', sheet)
            self.preview = image.tobytes() if ok else None
            path = self.root/'models'/'portal'/uuid.uuid4().hex/'calibration.json'
            self.candidate, self.negatives = path, []
            create_animation_calibration(self.capture, path, refs, payload['gems_roi'],
                                         payload['interaction_rois'], payload['animation_rois'], payload.get('cash_roi'))
            self.phase = 'candidate-needs-review'
            return {'candidate_created': True, 'approved': False,
                    'review_preview': '/api/calibration-preview'}

    def verify(self, payload):
        self.stop()
        with self.operation_lock, self.step_lock:
            self.stop()
            if self.capture is None or self.candidate is None:
                raise RuntimeError('Create a candidate first')
            from learning.vision import Vision
            from learning.calibration import verify_calibration
            if payload.get('negative'):
                if payload.get('approve'):
                    raise ValueError('Collect negatives separately from approval')
                self._storage()
                vision = Vision(self.candidate, calibration_check=True)
                frame = self.capture.capture()
                if vision.supported(frame):
                    self.vision = None
                    self.mouse.path_guard = lambda start, end: False
                    from learning.calibration import _save
                    metadata = json.loads(self.candidate.read_text())
                    metadata['ocr_verified'] = metadata['readiness_verified'] = False
                    metadata['verification_report'] = {'passed': False, 'error': 'negative_accepted'}
                    _save(self.candidate, metadata)
                    self.phase = 'candidate-failed-negative'
                    raise RuntimeError('Unsafe view was accepted; candidate must not be approved')
                path = self.candidate.parent/f'negative-{uuid.uuid4().hex}.png'
                if not cv2.imwrite(str(path), frame):
                    raise RuntimeError('Negative capture failed')
                self.negatives.append(path)
                return {'negative_rejected': True, 'negative_samples': len(self.negatives)}
            approve = payload.get('approve', False)
            if approve:
                self.vision = None
            passed = verify_calibration(self.capture, self.candidate, checks=20,
                                        negative_paths=self.negatives, approve=approve)
            if approve and passed:
                self.vision = Vision(self.candidate)
                self.mouse.path_guard = self.vision.path_supported
                self.phase = 'ready-stopped'
            return {'passed': passed, 'approved': self.vision is not None, 'running': False}

    def start(self):
        with self.operation_lock, self.step_lock:
            self._require_open()
            if self.capture is None or self.mouse is None or self.vision is None or not self._alive():
                raise RuntimeError('Connect and approve calibration before starting')
            if self.active.is_set():
                return self.status()
            with self.state_lock:
                generation = self.stop_generation
            # Full fresh identity/focus/capture; approval belongs to this process.
            image = self.capture.capture()
            if not self.vision.supported(image) or self.capture.calibration_identity() != self.vision.data['source']:
                raise RuntimeError('Approved gameplay view/source changed')
            if self.bot_factory is None:
                from bot import EatventureBot
                self.bot_factory = EatventureBot
            self.bot = self.bot_factory(capture=self.capture, mouse=self.mouse)
            self.mouse.guard = self._guard
            self.mouse.path_guard = self.vision.path_supported
            self.gems.reset()
            with self.state_lock:
                if generation != self.stop_generation or self.shutdown.is_set():
                    self.bot.running = False
                    raise RuntimeError('Start was cancelled by stop')
                self.bot.running = True
                self.error, self.phase = None, 'running'
                self.active.set()
            return self.status()

    def stop(self):
        # No operation lock: stop can revoke input during provisioning/calibration.
        with self.state_lock:
            self.stop_generation += 1
            self.active.clear()
            if self.bot is not None:
                self.bot.running = False
        if self.mouse is not None:
            self.mouse.release()
        with self.state_lock:
            self.gems.reset()
            if self.capture is not None:
                self.phase = 'connected-stopped'
        return {'running': False}

    def _worker(self):
        while not self.shutdown.wait(.2):
            with self.step_lock:
                if not self.active.is_set():
                    continue
                try:
                    if not self._guard():
                        raise RuntimeError('Unsafe gameplay view')
                    bot = self.bot
                    bot.state_machine.update()
                    if not self.active.is_set():
                        self.stop()
                        continue
                    image = self.capture.capture()
                    if not self.vision.supported(image):
                        raise RuntimeError('Unsupported gameplay view')
                    value, confidence = self.vision.gems(image)
                    self.gems.read(value, confidence, time.monotonic())
                    if self.gems.decreased:
                        raise RuntimeError('Confirmed Gems decrease')
                except Exception:
                    self.stop()
                    self.error = 'safety_pause'
            self.shutdown.wait(.8)

    def _disconnect(self):
        self.stop()
        # Old cached handlers must finish before replacing/closing their input.
        with self.step_lock:
            self._cleanup()

    def _cleanup(self):
        self.vision, self.bot = None, None
        if self.mouse is not None:
            self.mouse.close()
            self.mouse = None
        if self.capture is not None:
            self.capture.close()
            self.capture = None
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        self.processes.clear()
        self.endpoint = self.package = self.candidate = None
        self.negatives, self.preview = [], None
        self.phase = 'disconnected'

    def disconnect(self):
        self.cancel.set()
        self.stop()
        with self.operation_lock:
            self._disconnect()
        return self.status()

    def close(self):
        self.shutdown.set()
        self.cancel.set()
        self.stop()
        with self.operation_lock:
            self._disconnect()
        if self.worker is not threading.current_thread():
            self.worker.join(timeout=15)
