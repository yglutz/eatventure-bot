# Linux compatibility

## Android ADB: integrated, experimental input disabled by default

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
read-only; one direct native screenshot succeeded. Integrated capture attempts
encountered transient unauthorized status and then an Android-specific focus-query
format issue. Authorization subsequently recovered without changing permissions;
the focus query now uses the full window dump. Capture validation
was also updated for this phone's `mCurrentRotation=ROTATION_0` format (Android 13
does not necessarily expose legacy `SurfaceOrientation`). No taps or gestures
have been performed.

The corrected capture-only tool succeeded on this device: **0.893 seconds** for
one validated capture/decode/normalization sample. This is not a latency benchmark
or an input test. The preview remains candidate calibration, not approval.

Run the full offline suite with `uv run --locked python -m pytest -q` (pytest is
a uv development dependency). Offline results do not establish live-device gesture
or calibration correctness.

### Starting the app

```sh
uv run --locked main.py
# Explicit selection or another device:
uv run --locked main.py --backend adb --adb-serial SERIAL
```

`auto` selects ADB on Linux Wayland; otherwise it retains the desktop backend.
ADB uses the explicit serial/package/native-size/crop settings in `config.py`.
Startup validates a capture and starts **paused**. No desktop hotkeys or scrcpy
are required in ADB mode. Enter terminal-local commands `start`, `pause`, `status`,
or `quit`; EOF and Ctrl+C stop the application.

`start` refuses input unless launched with `--enable-adb-input`. This flag is an
explicit opt-in to experimental gestures **after manually reviewing calibration**;
it does not start gameplay automatically and does not certify the crop. Recheck
the preview, template scale, fixed targets and forbidden zones before opting in:

```sh
uv run --locked main.py --backend adb --enable-adb-input
# Then type start and press Enter only when ready for real device actions.
```

### ADB input limitations

Input stages a logical point and maps it into the explicit native crop. Presses
use independent `input touchscreen motionevent DOWN/MOVE/UP` commands, not a long
uninterruptible swipe. Foreground package, rotation, dimensions, default display,
unlocked/awake state, cancellation, and capture calibration are checked. Unknown
or conflicting supported Android state formats fail closed. Protected points and
whole drag paths remain checked by the common controller. A transport/input fault
requires restarting the backend rather than silently retrying old targets.

Separate Android shell events have differing gesture timestamps, and a zero ADB
exit status is **not proof that Android delivered the event**. Device/game-specific
gesture continuity remains unvalidated. UP is attempted after a potentially
delivered DOWN even if focus/cancellation checks fail; device disconnection can
prevent release. The staged touch position is not an observed cursor and cannot
detect someone touching the phone. Do not touch the device during automation.

ADB commands while pressed share an absolute deadline (maximum ten seconds,
shortened by the station hold deadline), with at most two seconds per subprocess
and a separate release attempt of at most two seconds. Cancellation can therefore
take roughly two seconds to surface and another two for UP, plus process and
scheduling overhead. There is no independent watchdog for stalled host code.
This is **not a 50ms stop
guarantee**. Capture, matching, subprocess scheduling and remote injection can
delay cancellation. Package checks cannot atomically prevent an overlay appearing
between validation and injection. No gameplay or speed improvement is claimed.

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
