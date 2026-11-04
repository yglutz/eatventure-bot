# Linux compatibility

## Current status: not supported as-is

The production application is Windows-specific:

- `requirements.txt` requires `pywin32`, which does not install as a native Linux
  dependency.
- `window_capture.py` imports Win32 modules, calls `ctypes.windll` during import,
  and uses Windows window handles, `PrintWindow`, and GDI resources for capture.
- `mouse_controller.py` and `main.py` import Win32 APIs for input/window handling.
- `bot.py` emits raw Win32 mouse input during station holds.
- The forbidden-area overlay is also implemented with Win32 APIs.

scrcpy itself runs on Linux, and OpenCV/NumPy-based matching is portable in
principle. That does not make this application's current capture/input pipeline
portable. Merely removing `pywin32` from requirements is insufficient. Existing
dependency pins also need validation against the selected Python/platform.

## What can run offline

Platform-independent detection and future pure policy/action tests can run on
Linux if dependencies are compatible and tests avoid importing Win32-dependent
modules, or use explicit mocks. This repository does not yet provide a complete
Linux-safe test boundary.

## Optional future port (not part of the improvement implementation scope)

1. Extract platform-neutral capture, window/context, input, and optional-overlay
   interfaces; inject implementations rather than importing Win32 everywhere.
2. Retain a Windows backend and add a separately tested Linux backend. Evaluate
   device-side screenshot/input transport versus desktop automation based on
   latency, coordinate mapping, and safety requirements.
3. Decide supported Linux session types explicitly. X11 and Wayland differ in
   capture/input/focus permissions; do not assume an X11 solution supports Wayland.
4. Make dependencies conditional and isolate platform imports. Permit offline
   tests without a desktop session or connected device.
5. Validate scaling, scrcpy content coordinates, focus handling, cancellation,
   protected controls, and capture latency on each supported platform. Preserve
   all safety rules and require authorization for live input validation.

A reliable port is feasible, but needs backend work and platform testing, not
just configuration changes. No Linux backend is implemented by these documents.
