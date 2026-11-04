# Linux compatibility

## Android ADB: capture-only stage

ADB capture works independently of X11/Wayland. Only the project's ignored
`platform-tools/adb` is used; there is no PATH fallback. Explicit serial, expected
foreground package, native dimensions, crop and logical dimensions are required.
Do not commit platform-tools binaries or device screenshots.

Example capture-only preview for the discovered phone (full-screen crop is a
candidate, **not approved calibration**):

```sh
uv run --locked python adb_calibrate.py \
  --serial ZT5TIJLVAQH6LJ7T --package com.hwqgrhhjfd.idlefastfood \
  --native-size 1080 2400 --crop 0 0 1080 2400 \
  --logical-size 360 780 --output /tmp/opencode/adb-calibration.png
```

The output directory must exist. The command creates the PNG and an
`.annotated.png` sibling marking fixed clicks and forbidden zones. Existing
outputs are rejected. Capture validates exact focus, portrait rotation zero and
native dimensions before and after transfer, with bounded subprocess timeouts.
Unknown dumpsys formats fail closed. Annotation does not approve the crop or
template scale; resizing the full phone image may distort the old layout.

Android 13, package and native 1080×2400 portrait dimensions were discovered
read-only; one direct native screenshot succeeded. The integrated capture attempt
was blocked when the device reverted to unauthorized. Reaccept USB debugging on
the phone before retrying. No taps or gestures have been performed.

Run the full offline suite with `uv run --locked python -m pytest -q` (pytest is
a uv development dependency). Current result: 64 passed and 43 subtests passed;
this is not evidence of live-device gesture or calibration correctness.

**Gameplay is not enabled through ADB yet.** Pointer/button methods deliberately
raise errors. Cancellable gesture semantics, release after failures, calibration,
and terminal-local start/pause/quit controls must be implemented and validated
before integrating this backend into normal gameplay. Desktop backends remain
unchanged; running `main.py` does not select ADB.

## Status: experimental X11 backend implemented; live validation pending

Windows uses the retained Win32 backend. Linux uses python-xlib/XTEST for window
validation and input, and MSS for desktop capture. Wayland, including XWayland
sessions, is explicitly unsupported. Platform imports and desktop initialization
are isolated; offline tests need neither a desktop nor a connected device.

## Environment (uv is standard)

Python 3.14 is the supported dependency baseline. Install uv, then:

```sh
uv python install 3.14
uv sync --python "$(uv python find --managed-python 3.14)" --locked
uv run --locked python -m unittest discover -s tests
```

The managed interpreter includes headers needed to build pynput's evdev
dependency. This machine's system Python lacked those headers; managed Python
successfully installed the locked dependencies. A compiler and Linux input
headers may still be required on other distributions. `requirements.txt` is a
compatibility mirror; `pyproject.toml` and `uv.lock` are authoritative.
Windows dependency installation/runtime still requires separate validation.

## X11 setup before any authorized live test

- Use a native X11 session with authenticated `DISPLAY`, EWMH window manager,
  and XTEST. The bot never steals focus.
- Open exactly one scrcpy window matching `config.WINDOW_TITLE`. Request
  `--window-width 360 --window-height 780` for the current configuration.
- X11 dimensions mean **client pixels**. Windows historically resized the outer
  window instead. The current 360×780 configuration is a calibration candidate,
  not verified equivalence to Windows screenshots.
- Manually verify portrait orientation, no letterboxing, template scale, fixed
  click positions and every forbidden zone before starting automation. Geometry
  checks cannot recognize an incorrect game layout or rotation inside the client.
- Keep scrcpy focused, unobscured, fully on-screen and stationary. Startup already
  requires focus, so a delayed terminal launch may be necessary. Do not run the
  bot or press Z until live validation is explicitly authorized.
- X11 has no visual forbidden-area overlay; the omission is logged. Static input
  protections remain enforced. Disable Telegram notifications for validation.

## Safety and limitations

Capture validates focus, mapped state, bounds, occlusion and stable geometry.
Input shares the same backend and validates captured geometry, client bounds,
forbidden zones and drag segments. Holds check cancellation and pointer movement;
button release is attempted even on errors. Failed initialization unwinds resources.

X11 global input has unavoidable focus/check races. A disconnected X server can
prevent release delivery. Captured geometry does not prove a target is visually
fresh or the game layout calibrated. Blocking capture/matching can delay
cancellation beyond the 50ms polling interval. Conservative focus/occlusion checks
may reject some window-manager layouts. Do not touch the pointer during operation.

## Validation

Mocked offline tests cover backend selection/capture, failure cleanup, guarded
input, cancellation and application integration. Dependency installation was
tested locally using uv-managed Python 3.14. No live desktop capture, device input,
purchases or notifications were performed. Windows and actual X11/scrcpy scaling,
focus, latency and gameplay outcomes remain unvalidated. No speedup is claimed.
