# Codebase routing

This bot controls Eatventure through scrcpy on Windows/Win32 and experimental
Linux/X11 (not Wayland desktop automation), plus opt-in experimental Android ADB usable
independently of the desktop session. Keep pure
logic testable offline; do not run live input, purchases, or notifications
without explicit authorization.

| Area | Files | OpenCode agent |
| --- | --- | --- |
| Runtime and detection | `image_matcher.py`, `window_capture.py`, `runtime_metrics.py`, `tests/test_runtime_metrics.py`, `tests/test_image_matcher.py`, `windows_backend.py`, `x11_backend.py`, timing in `main.py`, `tests/test_platform_capture.py` | `bot-runtime` |
| Action flow and state | `bot.py`, `state_machine.py`, `tests/test_portable_integration.py` | `bot-actions` |
| Input protection | `mouse_controller.py`, safety checks in `bot.py`, backend input/validation, overlay in `windows_backend.py`, `tests/test_mouse_controller.py` | `bot-safety` |
| Python environment | `pyproject.toml`, `uv.lock`, `requirements.txt` (compatibility mirror) | Coordinator with `bot-runtime` |
| Android capture/calibration | `adb_backend.py`, `adb_calibrate.py`, `tests/test_adb_backend.py`, `tests/test_adb_calibrate.py`; bundled executable `platform-tools/adb` (ignored) | `bot-runtime` |
| Experimental Android input (disabled by default) | `adb_input.py`, input transport in `adb_backend.py`, `tests/test_adb_input.py` | `bot-safety` |
| Terminal controls and shared desktop/terminal command queue | `terminal_controls.py`, `tests/test_terminal_controls.py`, integration in `main.py` | `bot-actions` |
| Gameplay decisions and upgrade exclusions | `bot.py`, `config.py`, recognition assets in `Assets/` | `bot-strategy` |
| Controls, settings, notifications | `main.py`, `config.py`, `telegram_notifier.py`, `tests/test_telegram_notifier.py`, `get_chat_id.py` | Coordinate by task |
| Agent definitions and usage | `.opencode/agents/`, `.opencode/README.md` | `bot-improvements` |
| Project OpenCode settings and agent permissions | `opencode.json` | `bot-improvements` |
| Per-agent permission matrix | `docs/opencode-permissions.md` | `bot-improvements` (update alongside permission changes) |
| Improvement plans and platform notes | `docs/` (start at `docs/README.md`) | `bot-improvements` and the relevant specialist |
| Independent read-only review and strict code-guideline enforcement | Relevant source and tests, `docs/code-guidelines.md` | `bot-review` |

Use `bot-improvements` to coordinate cross-cutting work. Shared files need one
writer at a time; routing is guidance, not exclusive file ownership. Tests belong
in `tests/` when introduced. See `README.md` for application setup.

## Keep routing current

Every implementation agent must update this file in the same change when it
adds, moves, renames, or splits modules, changes test locations, or changes agent
responsibilities. List actual paths, not planned modules; keep this file minimal.
Review agents must flag outdated routing without editing it. Delegated agents
must report required routing changes to the coordinator, who integrates them
serially when several tasks would touch this file.
Consult the relevant plan in `docs/` before implementation; update its status or
decisions when work changes it, without marking untested proposals as complete.
