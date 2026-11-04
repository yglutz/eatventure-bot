# Codebase routing

This is a Windows/Win32 bot controlling Eatventure through scrcpy. Keep pure
logic testable offline; do not run live input, purchases, or notifications
without explicit authorization.

| Area | Files | OpenCode agent |
| --- | --- | --- |
| Runtime and detection | `image_matcher.py`, `window_capture.py`, `runtime_metrics.py`, `tools/benchmark_runtime.py`, timing in `main.py` and `state_machine.py` | `bot-runtime` |
| Action flow and state | `bot.py`, `state_machine.py` | `bot-actions` |
| Input protection | `mouse_controller.py`, safety checks in `bot.py`, overlay in `window_capture.py` | `bot-safety` |
| Input outcomes | `action_result.py` | `bot-safety`, coordinate with `bot-actions` |
| Offline runtime regressions | `tests/test_runtime_metrics.py`, `tests/test_image_matcher.py`, `tests/test_mouse_controller.py`, `tests/test_bot_runtime.py`, `tests/test_telegram_notifier.py` | Corresponding runtime, safety, and actions specialists |
| Gameplay decisions and upgrade exclusions | `bot.py`, `config.py`, recognition assets in `Assets/` | `bot-strategy` |
| Controls, settings, notifications | `main.py`, `config.py`, `telegram_notifier.py`, `get_chat_id.py` | Coordinate by task |
| Agent definitions and usage | `.opencode/agents/`, `.opencode/README.md` | `bot-improvements` |
| Project OpenCode settings and agent permissions | `opencode.json` | `bot-improvements` |
| Per-agent permission matrix | `docs/opencode-permissions.md` | `bot-improvements` (update alongside permission changes) |
| Improvement plans and platform notes | `docs/` (start at `docs/README.md`) | `bot-improvements` and the relevant specialist |
| Independent read-only review | Relevant source and tests | `bot-review` |

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
