"""Runtime safety contracts, using only in-memory devices and processes."""
import builtins
import importlib.util
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

import portal_runtime


ENDPOINT = "192.168.1.40:5555"
PACKAGE = "com.example.game"


def forbidden_process(*args, **kwargs):
    pytest.fail("A test attempted to launch a real subprocess")


@pytest.fixture(autouse=True)
def no_live_processes_or_worker(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", forbidden_process)
    monkeypatch.setattr(subprocess, "run", forbidden_process)
    # Exercise the worker synchronously below, without scheduling races.
    monkeypatch.setattr(portal_runtime.threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(portal_runtime.threading.Thread, "join", lambda self, **kw: None)


class Process:
    def __init__(self, events, name="scrcpy"):
        self.events, self.name = events, name
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.events.append((self.name, "terminate"))
        self.returncode = 0

    def kill(self):
        self.events.append((self.name, "kill"))
        self.returncode = -9

    def wait(self, timeout):
        self.events.append((self.name, "wait", timeout))
        return self.returncode


class Capture:
    def __init__(self, events):
        self.events = events
        self.image = np.zeros((24, 16, 3), dtype=np.uint8)
        self.source = {"serial": ENDPOINT, "size": [16, 24]}
        self.failure = False
        self.focused = True

    def capture(self):
        self.events.append("capture")
        if self.failure:
            raise RuntimeError("private capture details")
        return self.image.copy()

    def capture_view(self):
        self.events.append("capture_view")
        return self.capture()

    def calibration_identity(self):
        return dict(self.source)

    def is_window_active(self):
        return self.focused

    def resize_window(self):
        self.events.append("resize")

    def close(self):
        self.events.append("capture.close")


class Mouse:
    def __init__(self, events):
        self.events = events

    def release(self):
        self.events.append("mouse.release")

    def close(self):
        self.events.append("mouse.close")

    def click(self, *args, **kwargs):
        pytest.fail("Runtime test attempted gameplay input")

    hold = drag = click


class Vision:
    def __init__(self, capture):
        self.data = {"source": capture.calibration_identity()}
        self.accepted = True
        self.reading = (100, .99)
        self.frames = []

    def supported(self, image):
        self.frames.append(image)
        return self.accepted

    def path_supported(self, *args):
        return self.accepted

    def gems(self, image):
        return self.reading


@pytest.fixture
def runtime(tmp_path):
    events, commands, spawned, bots = [], [], [], []

    def run(args, **kwargs):
        commands.append((args, kwargs))
        if args[1] == "pair":
            output = f"Successfully paired to {args[2]}"
        elif args[1] == "connect":
            output = f"connected to {args[2]}"
        else:
            output = "device"
        return SimpleNamespace(returncode=0, stdout=output.encode(), stderr=b"")

    def popen(args, **kwargs):
        spawned.append((args, kwargs))
        return Process(events, args[0])

    def bot_factory(**kwargs):
        events.append("bot.created")
        bot = SimpleNamespace(running=False, state_machine=SimpleNamespace(
            update=lambda: events.append("bot.update")))
        bots.append(bot)
        return bot

    capture, mouse = Capture(events), Mouse(events)
    instance = portal_runtime.Runtime(
        allowed_hosts=["192.168.1.40", "127.0.0.1", "169.254.1.2", "224.0.0.1", "0.0.0.0"],
        root=tmp_path, display_name=":99", popen=popen, run=run,
        capture_factory=lambda *a, **kw: capture,
        mouse_factory=lambda **kw: mouse, bot_factory=bot_factory)
    instance.test = SimpleNamespace(events=events, commands=commands, spawned=spawned,
                                    capture=capture, mouse=mouse, bots=bots)
    yield instance
    instance.close()


def approve_fake(runtime):
    runtime.capture = runtime.test.capture
    runtime.mouse = runtime.test.mouse
    runtime.vision = Vision(runtime.capture)
    runtime.processes = [Process(runtime.test.events)]
    runtime.endpoint, runtime.package = ENDPOINT, PACKAGE


def worker_ticks(runtime, count):
    """Run exactly count iterations of the actual worker loop, without sleeps."""
    event = runtime.shutdown

    class Shutdown:
        ticks = 0

        def is_set(self):
            return event.is_set()

        def wait(self, timeout):
            if timeout == .2:
                self.ticks += 1
                return self.ticks > count
            return False

    runtime.shutdown = Shutdown()
    try:
        runtime._worker()
    finally:
        runtime.shutdown = event


def test_default_construction_is_stopped_and_imports_no_learning_stack(monkeypatch, tmp_path):
    original_import = builtins.__import__
    blocked = {"torch", "gym", "gymnasium", "stable_baselines3", "bot", "x11_capture", "mouse_controller"}

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in blocked:
            pytest.fail(f"Eager runtime dependency: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.delitem(sys.modules, "learning.core", raising=False)
    spec = importlib.util.spec_from_file_location("isolated_portal_runtime", portal_runtime.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    instance = module.Runtime(root=tmp_path, allowed_hosts=[], display_name=":99")
    try:
        status = instance.status()
        assert not status["running"] and not status["connected"]
        assert not status["approved"] and status["learning"] is False
        assert status["gems"] is None
        assert instance.processes == []
        assert instance.capture is instance.mouse is instance.bot is instance.vision is None
    finally:
        instance.close()


@pytest.mark.parametrize("endpoint", [
    "phone:5555", "192.168.1.41:5555", "192.168.1.40:0", "192.168.1.40:65536",
    "192.168.1.40:5555;whoami", "192.168.1.40:5555\n", "999.1.1.1:5555",
    "127.0.0.1:5555", "169.254.1.2:5555", "224.0.0.1:5555", "0.0.0.0:5555",
    "[::1]:5555", None,
])
def test_provisioning_rejects_unvalidated_addresses_before_subprocess(runtime, endpoint):
    with pytest.raises(ValueError):
        runtime.pair(endpoint, "123456")
    with pytest.raises(ValueError):
        runtime.connect(endpoint, PACKAGE)
    assert runtime.test.commands == runtime.test.spawned == []


@pytest.mark.parametrize("code", ["12345", "1234567", "123456;id", "123456\n", 123456, None])
def test_pairing_code_validation(runtime, code):
    with pytest.raises(ValueError):
        runtime.pair(ENDPOINT, code)
    assert runtime.test.commands == []


def test_pair_and_adb_use_argument_lists_shell_false_and_bounded_timeouts(runtime):
    assert runtime.pair(ENDPOINT, "123456") == {"paired": True, "endpoint": ENDPOINT}
    runtime._adb(["-s", ENDPOINT, "get-state"])
    assert [args for args, _ in runtime.test.commands] == [
        ["adb", "pair", ENDPOINT, "123456"], ["adb", "-s", ENDPOINT, "get-state"]]
    for _, options in runtime.test.commands:
        assert options["shell"] is False and options["check"] is False
        assert 0 < options["timeout"] <= 30
        assert options["stdout"] == options["stderr"] == subprocess.PIPE
    assert "123456" not in repr(runtime.status())


@pytest.mark.parametrize("failure", ["exit", "timeout", "oserror"])
def test_pair_errors_do_not_expose_pairing_code(runtime, failure):
    code = "938271"

    def fail(args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, kwargs["timeout"], output=code, stderr=code)
        if failure == "oserror":
            raise OSError(f"private details {code}")
        return SimpleNamespace(returncode=1, stdout=code.encode(), stderr=code.encode())

    runtime.run = fail
    with pytest.raises(Exception) as error:
        runtime.pair(ENDPOINT, code)
    assert code not in str(error.value)
    assert code not in repr(runtime.status())


def test_connect_is_lazy_and_uses_private_process_commands(runtime, monkeypatch):
    # Readiness checks still execute; only the twelve-second settling clock advances.
    clock = iter(range(0, 1000, 20))
    monkeypatch.setattr(portal_runtime.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(runtime, "_wait", lambda predicate, **kw: predicate())
    assert runtime.test.spawned == []
    status = runtime.connect(ENDPOINT, PACKAGE)
    assert status["connected"] and not status["running"] and not status["approved"]
    assert runtime.test.bots == []
    assert [args[0] for args, _ in runtime.test.spawned] == ["Xvfb", "openbox", "scrcpy"]
    for args, options in runtime.test.spawned:
        assert options["shell"] is False
        assert options["env"]["DISPLAY"] == ":99"
        assert options["stdin"] == options["stdout"] == options["stderr"] == subprocess.DEVNULL
    xvfb, _, scrcpy = [args for args, _ in runtime.test.spawned]
    assert xvfb[xvfb.index("-nolisten") + 1] == "tcp"
    assert scrcpy[scrcpy.index("--serial") + 1] == ENDPOINT
    assert "--no-clipboard-autosync" in scrcpy
    assert all(options["shell"] is False and 0 < options["timeout"] <= 30
               for _, options in runtime.test.commands)


def test_start_is_fresh_stop_resets_baseline_and_restart_creates_bot(runtime):
    approve_fake(runtime)
    assert runtime.start()["running"]
    assert "capture" in runtime.test.events
    assert len(runtime.vision.frames) == 1
    for now in range(3):
        runtime.gems.read(100, .99, now)
    assert runtime.gems.last == 100
    first = runtime.bot
    assert runtime.stop() == {"running": False}
    assert not runtime.active.is_set() and not first.running
    assert runtime.gems.last is None and runtime.gems.count == 0
    assert "mouse.release" in runtime.test.events
    assert runtime.start()["running"]
    assert runtime.bot is not first and len(runtime.test.bots) == 2


@pytest.mark.parametrize("unsafe", ["view", "source", "capture", "process"])
def test_start_rejects_changed_or_failed_source(runtime, unsafe):
    approve_fake(runtime)
    if unsafe == "view":
        runtime.vision.accepted = False
    elif unsafe == "source":
        runtime.capture.source = {"serial": "192.168.1.41:5555", "size": [16, 24]}
    elif unsafe == "capture":
        runtime.capture.failure = True
    else:
        runtime.processes[0].returncode = 1
    with pytest.raises(RuntimeError):
        runtime.start()
    assert not runtime.active.is_set()
    assert runtime.test.bots == []


@pytest.mark.parametrize("unsafe", ["view", "capture", "focus", "process"])
def test_guard_revokes_running_input_using_fresh_view(runtime, unsafe):
    approve_fake(runtime)
    runtime.start()
    runtime.test.events.clear()
    if unsafe == "view":
        runtime.vision.accepted = False
    elif unsafe == "capture":
        runtime.capture.failure = True
    elif unsafe == "focus":
        runtime.capture.focused = False
    else:
        runtime.processes[0].returncode = 1
    assert runtime._guard() is False
    assert not runtime.active.is_set()
    if unsafe in ("view", "capture"):
        assert "capture_view" in runtime.test.events


@pytest.mark.parametrize("unsafe", ["view", "capture", "process"])
def test_worker_safety_pause_releases_and_stops_bot(runtime, unsafe):
    approve_fake(runtime)
    runtime.start()
    if unsafe == "view":
        runtime.vision.accepted = False
    elif unsafe == "capture":
        runtime.capture.failure = True
    else:
        runtime.processes[0].returncode = 1
    worker_ticks(runtime, 1)
    assert not runtime.active.is_set() and not runtime.bot.running
    assert "mouse.release" in runtime.test.events
    assert runtime.error == "safety_pause"
    assert "bot.update" not in runtime.test.events


def test_worker_pauses_only_on_confirmed_gems_decrease(runtime):
    approve_fake(runtime)
    runtime.start()
    worker_ticks(runtime, 3)
    assert runtime.gems.last == 100 and runtime.active.is_set()
    runtime.vision.reading = (99, .94)
    worker_ticks(runtime, 3)
    assert runtime.active.is_set() and runtime.gems.last == 100
    runtime.vision.reading = (99, .99)
    worker_ticks(runtime, 2)
    assert runtime.active.is_set() and runtime.gems.last == 100
    worker_ticks(runtime, 1)
    assert not runtime.active.is_set() and not runtime.bot.running
    assert runtime.error == "safety_pause"
    assert runtime.gems.last is None
    assert "mouse.release" in runtime.test.events


@pytest.mark.parametrize("failure", ["capture", "encoding"])
def test_screenshot_failure_stops_without_input(runtime, monkeypatch, failure):
    approve_fake(runtime)
    runtime.start()
    if failure == "capture":
        runtime.capture.failure = True
    else:
        monkeypatch.setattr(portal_runtime.cv2, "imencode", lambda *args: (False, None))
    with pytest.raises(RuntimeError, match="Screenshot unavailable; bot stopped") as error:
        runtime.screenshot()
    assert "private" not in str(error.value)
    assert not runtime.active.is_set() and not runtime.bot.running
    assert runtime.error == "capture_failed"
    assert "mouse.release" in runtime.test.events


def test_screenshot_success_does_not_start_bot(runtime):
    runtime.capture = runtime.test.capture
    assert runtime.screenshot().startswith(b"\x89PNG\r\n\x1a\n")
    assert not runtime.active.is_set() and runtime.test.bots == []


def test_negative_cannot_simultaneously_approve(runtime, monkeypatch, tmp_path):
    approve_fake(runtime)
    runtime.candidate = tmp_path / "calibration.json"
    runtime.start()
    vision_module = SimpleNamespace(Vision=lambda *a, **kw: pytest.fail("Verification proceeded"))
    calibration_module = SimpleNamespace(verify_calibration=lambda *a, **kw: pytest.fail("Approval proceeded"))
    monkeypatch.setitem(sys.modules, "learning.vision", vision_module)
    monkeypatch.setitem(sys.modules, "learning.calibration", calibration_module)
    with pytest.raises(ValueError, match="separately"):
        runtime.verify({"negative": True, "approve": True})
    assert not runtime.active.is_set()
    assert runtime.negatives == []
    assert not runtime.candidate.exists()


def test_dead_process_cannot_reuse_cached_approval(runtime):
    approve_fake(runtime)
    runtime.start()
    runtime.stop()
    runtime.processes[0].returncode = 1
    with pytest.raises(RuntimeError):
        runtime.start()
    assert not runtime.active.is_set()
    assert len(runtime.test.bots) == 1
    assert runtime.status()["connected"] is False


def test_failed_reconnection_discards_cached_approval(runtime):
    approve_fake(runtime)
    runtime.start()
    old_bot = runtime.bot

    def failed_run(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout=b"private error", stderr=b"private error")

    runtime.run = failed_run
    with pytest.raises(RuntimeError, match="bot remains stopped"):
        runtime.connect(ENDPOINT, PACKAGE)
    assert not old_bot.running and not runtime.active.is_set()
    assert runtime.capture is runtime.mouse is runtime.vision is runtime.bot is None
    assert runtime.processes == []
    assert runtime.status()["approved"] is False
    with pytest.raises(RuntimeError):
        runtime.start()
    assert len(runtime.test.bots) == 1


def test_close_revokes_input_releases_then_closes_capture_and_processes(runtime):
    approve_fake(runtime)
    runtime.start()
    bot = runtime.bot
    runtime.test.events.clear()
    runtime.close()
    events = runtime.test.events
    assert not bot.running and not runtime.active.is_set()
    assert runtime.shutdown.is_set() and runtime.cancel.is_set()
    assert events.index("mouse.release") < events.index("mouse.close")
    assert events.index("mouse.close") < events.index("capture.close")
    assert events.index("capture.close") < events.index(("scrcpy", "terminate"))
    assert ("scrcpy", "wait", 3) in events
    assert runtime.capture is runtime.mouse is runtime.bot is runtime.vision is None
    assert runtime.processes == []
    with pytest.raises(RuntimeError, match="closed"):
        runtime.start()


def test_close_kills_process_after_bounded_termination_timeout(runtime):
    approve_fake(runtime)
    process = runtime.processes[0]
    original_wait = process.wait

    def wait(timeout):
        if process.returncode != -9:
            raise subprocess.TimeoutExpired("scrcpy", timeout)
        return original_wait(timeout)

    process.wait = wait
    runtime.close()
    assert ("scrcpy", "kill") in runtime.test.events
    assert ("scrcpy", "wait", 3) in runtime.test.events
