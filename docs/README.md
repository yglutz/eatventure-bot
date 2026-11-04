# Improvement plans

These are implementation proposals, not completed features. Agents must verify
the current code before acting on findings. Do not run live input, purchases, or
notifications without explicit authorization.

| Plan | Owner | Depends on |
| --- | --- | --- |
| [Runtime and baseline fixes](01-runtime.md) | `bot-runtime` | None; coordinate safety-sensitive fixes |
| [Central input safety](02-input-safety.md) | `bot-safety` | Baseline tests |
| [Shared observations and composite actions](03-actions.md) | `bot-actions` | Safety execution boundary |
| [Named no-click buttons](04-no-click-buttons.md) | `bot-safety` | Safety boundary and observations |
| [Gameplay strategy](05-strategy.md) | `bot-strategy` | Verified actions and identifiable targets |
| [Selective upgrade exclusions](06-upgrade-exclusions.md) | `bot-strategy` | Policy interface and upgrade recognition |

`bot-improvements` coordinates stages and assigns a single writer for shared
files. `bot-review` reviews each stage. Read-only investigation and disjoint tests
may run in parallel; dependencies need agreed interfaces before implementation.

For each stage: capture the baseline, implement the smallest change, run offline
regression tests, review, then separately record any authorized Windows/device
validation. Update `AGENTS.md` when actual modules or test locations change.
Do not claim performance improvements without comparable measurements.

See [Linux compatibility](linux-compatibility.md) for current platform limits.
See [OpenCode permissions](opencode-permissions.md) for the per-agent action
matrix and centralized permission defaults.
