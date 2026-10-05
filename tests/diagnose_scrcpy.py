"""Opt-in, input-free Linux scrcpy diagnostic; not collected by pytest.

Run in the container: PYTHONPATH=/app python /tmp/diagnose_scrcpy.py
No focus requests, XTest events, Android input, launch, wake or guard overrides.
Only this process's private X server, WM and mirror are terminated on exit.
"""
import argparse
from collections import deque
import json
import os
import re
import subprocess
import sys
import threading
import time


def scrub(text):
    text = re.sub(r"(?i)\b(password|passwd|token|secret|credential|pairing[ _-]?code)\s*[:=]\s*\S+",
                  r"\1=[REDACTED]", str(text))
    text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    return text


def emit(event, **fields):
    print(scrub(json.dumps(dict(event=event, **fields), default=str)), flush=True)


class Tail:
    """Drain pipes continuously, retaining at most 20 lines of 400 chars each."""
    def __init__(self, pipe):
        self.lines = deque(maxlen=20)
        self.pending = ""
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.drain, args=(pipe,), daemon=True)
        self.thread.start()

    def drain(self, pipe):
        try:
            while True:
                chunk = os.read(pipe.fileno(), 1024)
                if not chunk:
                    break
                with self.lock:
                    parts = (self.pending + chunk.decode("utf-8", "replace")).split("\n")
                    self.lines.extend(scrub(line[-400:]) for line in parts[:-1])
                    self.pending = parts[-1][-400:]
        except (OSError, ValueError) as exc:
            with self.lock:
                self.lines.append(scrub(str(exc))[-400:])

    def snapshot(self):
        with self.lock:
            return list(self.lines) + ([scrub(self.pending)] if self.pending else [])


def property_info(display, window, name):
    from Xlib import X
    atom = display.intern_atom(name, only_if_exists=True)
    prop = window.get_full_property(atom, X.AnyPropertyType) if atom else None
    if prop is None:
        return None
    value = prop.value
    return {"python_type": type(value).__name__, "format": prop.format,
            "value": value.decode("utf-8", "replace")[:200] if isinstance(value, bytes)
            else list(value)[:8] if not isinstance(value, str) else value[:200]}


def windows(display):
    from Xlib import X
    pending = [display.screen().root]
    found = []
    visited = 0
    while pending and visited < 2048:
        parent = pending.pop()
        for child in parent.query_tree().children:
            visited += 1
            pending.append(child)
            if child.get_attributes().map_state != X.IsViewable:
                continue
            title, classes = child.get_wm_name(), child.get_wm_class()
            if title or classes:
                geometry = child.get_geometry()
                found.append({"id": child.id, "title": title, "classes": classes,
                              "geometry": [geometry.x, geometry.y, geometry.width, geometry.height],
                              "net_wm_name": property_info(display, child, "_NET_WM_NAME")})
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default="192.168.63.163:37097")
    parser.add_argument("--expected-package", default="com.hwqgrhhjfd.idlefastfood")
    parser.add_argument("--observe-seconds", type=int, default=20)
    args = parser.parse_args()
    if not 0 <= args.observe_seconds <= 120:
        parser.error("observe-seconds must be between 0 and 120")
    owned = []
    captures = []
    display = None
    full_ok = False
    view_ok = False
    env = dict(os.environ, DISPLAY=":97", SDL_VIDEODRIVER="x11",
               SDL_RENDER_DRIVER="software", LIBGL_ALWAYS_SOFTWARE="1")

    def start(name, command):
        proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        owned.append((name, proc, Tail(proc.stdout), Tail(proc.stderr)))
        emit("started", name=name, pid=proc.pid, command=command)
        return proc

    try:
        from Xlib import display as xd
        from x11_capture import X11Capture
        from adb_capture import ADBTransport
        if os.path.exists("/tmp/.X97-lock") or os.path.exists("/tmp/.X11-unix/X97"):
            raise RuntimeError("Private display :97 already exists; refusing to touch it")
        transport = ADBTransport(args.serial, timeout=5)
        emit("adb", state=transport.run(["get-state"]).decode().strip())
        xvfb = start("Xvfb", ["Xvfb", ":97", "-screen", "0", "1024x1000x24", "-nolisten", "tcp", "-noreset"])
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if xvfb.poll() is not None:
                raise RuntimeError("Owned Xvfb exited")
            try:
                display = xd.Display(":97")
                break
            except Exception:
                time.sleep(.2)
        if display is None:
            raise RuntimeError("Private X server did not become ready")
        wm = start("openbox", ["openbox", "--sm-disable"])
        # Wait for WM readiness without requesting focus or generating events.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if wm.poll() is not None:
                raise RuntimeError("Owned openbox exited")
            if property_info(display, display.screen().root, "_NET_SUPPORTING_WM_CHECK"):
                break
            time.sleep(.2)
        mirror = start("scrcpy", ["scrcpy", "--serial", args.serial, "--window-title", "op",
                                  "--no-control", "--no-audio", "--no-power-on",
                                  "--no-clipboard-autosync", "--max-size", "800",
                                  "--max-fps", "10", "--render-driver", "software"])
        deadline = time.monotonic() + 20
        entries = []
        while time.monotonic() < deadline:
            if mirror.poll() is not None:
                raise RuntimeError("Owned scrcpy exited before window discovery")
            entries = windows(display)
            if any(w["title"] == "op" for w in entries):
                break
            time.sleep(.25)
        emit("mapped_windows", windows=entries)

        def fresh():
            capture = X11Capture(":97", "op", serial=args.serial,
                                 expected_package=args.expected_package,
                                 process_alive=lambda: mirror.poll() is None)
            captures.append(capture)
            return capture

        capture = fresh()
        emit("find_window", id=capture.window.id, rect=capture.get_window_rect())
        capture.resize_window()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and capture.get_window_rect()[2:] != (360, 780):
            time.sleep(.1)
        time.sleep(1)
        emit("desktop_metadata", rect=capture.get_window_rect(),
             focus_id=getattr(display.get_input_focus().focus, "id", display.get_input_focus().focus),
             net_active_window=property_info(display, display.screen().root, "_NET_ACTIVE_WINDOW"),
             windows=windows(display), active=capture.is_window_active())
        geometry = capture.window.get_geometry()
        reply = capture.window.get_image(0, 0, geometry.width, geometry.height,
                                         capture.X.ZPixmap, 0xffffffff)
        info = capture.display.display.info
        emit("x11_image_metadata", depth=reply.depth, visual_id=reply.visual,
             byte_count=len(reply.data), image_byte_order=info.image_byte_order,
             truecolor_constant=capture.X.TrueColor,
             formats=[{"depth": f.depth, "bits_per_pixel": f.bits_per_pixel,
                       "scanline_pad": f.scanline_pad}
                      for f in info.pixmap_formats if f.depth == reply.depth],
             visuals=[{"visual_id": v.visual_id, "visual_class": v.visual_class,
                       "red_mask": v.red_mask, "green_mask": v.green_mask,
                       "blue_mask": v.blue_mask, "bits_per_rgb": v.bits_per_rgb_value,
                       "colormap_entries": v.colormap_entries}
                      for d in capture.display.screen().allowed_depths
                      for v in d.visuals if v.visual_id == reply.visual])
        try:
            image = capture.capture_view()
            view_ok = True
            emit("desktop_capture", ok=True, shape=list(image.shape), dtype=str(image.dtype),
                 contiguous=bool(image.flags.c_contiguous), last_capture_ok=capture.last_capture_ok)
        except Exception as exc:
            emit("desktop_capture", ok=False, error=str(exc))
        deadline = time.monotonic() + args.observe_seconds
        previous = None
        while True:
            # A full rejection latches failure: never clear/override it; use a new instance.
            attempt = fresh()
            try:
                raw = transport.run(["shell", "dumpsys", "window"]).decode("utf-8", "replace")
                focuses = re.findall(r"mCurrentFocus\s*=\s*([^\r\n]+)", raw)
                android_ok = attempt.android_guard.is_window_active()
                try:
                    image = attempt.capture()
                    full_ok = True
                    status = dict(android_guard=android_ok, full_capture=True, shape=list(image.shape))
                except Exception as exc:
                    status = dict(android_guard=android_ok, full_capture=False, error=str(exc))
                status["foreground"] = [s[:200] for s in focuses[:4]]
                if status != previous:
                    emit("full_capture", **status)
                    previous = status
            finally:
                attempt.close()
                captures.remove(attempt)
            if full_ok or time.monotonic() >= deadline:
                break
            time.sleep(2)
        emit("summary", desktop_capture=view_ok, full_capture=full_ok,
             observation_seconds=args.observe_seconds, input_sent=False)
        return 0 if view_ok else 1
    except Exception as exc:
        emit("diagnostic_error", type=type(exc).__name__, error=str(exc))
        return 1
    finally:
        for capture in captures:
            capture.close()
        if display is not None:
            display.close()
        for name, proc, stdout, stderr in reversed(owned):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            stdout.thread.join(timeout=1)
            stderr.thread.join(timeout=1)
            emit("owned_cleanup", name=name, pid=proc.pid, returncode=proc.returncode,
                 stdout_last20=stdout.snapshot()[-20:], stderr_last20=stderr.snapshot()[-20:])


if __name__ == "__main__":
    sys.exit(main())
