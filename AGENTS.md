# OpenCode Mobile AI Agent Setup

Struktur:

.opencode/
  agents/
    *.md

Diese Agents sind für ein Projekt gedacht, bei dem eine KI ein Android-Spiel über:
- Computer Vision
- Reinforcement Learning
- ADB Steuerung

lernen soll.

Empfohlene Reihenfolge:
1. @ai-architect
2. @game-analyzer
3. @android-controller
4. @computer-vision-engineer
5. @rl-researcher
6. @experiment-manager

# Starting point for strategies
- Ein einziger Koch performt besser als mehrere Köche.
- Es ist nicht immer besser alle Kunden-Upgrades durchzuführen.
- Eine Station nach der anderen zu upgraden ist oft besser als alles gleichzeitig zu verbessern.
- Es gibt einen Multiplikator in der linken oberen Ecke, der den Profit im Spiel erhöht. Da mehr Profit bedeutet, dass Stationen schneller aufgewertet werden können, beeinflusst dieser auch die Gems pro Minute. Dieser Multiplikator sollte in der Belohnungsfunktion berücksichtigt werden.

# Project rules

- Keep Windows, uv and Python 3.13 support. Preserve unrelated local changes.
- Never remove, shrink or weaken any forbidden zone in `config.py`, including
  the bottom forbidden area. Boundaries are inclusive. Calibration may only add
  restrictions; it must never override these zones.
- All mouse input must pass through `MouseController` in `mouse_controller.py`.
  Do not introduce direct Win32, pynput, pyautogui, adb or other input bypasses.
  Absolute coordinates, holds and complete continuous drag paths require the
  same safety checks. Always release pressed buttons on error, pause and exit.
- Reject input on focus loss, capture failure or a changed calibrated client
  size. Learning/collection require a supported calibrated gameplay view and
  safe interaction area. Do not add shop navigation, purchases or Gems spending.
- Positive rewards come only from confirmed Gems measurements: three consecutive
  readings with confidence >= 0.95. Unknown is never zero. Preserve baseline
  resets on restart/manual pause/timeout, and pause on confirmed decreases.
  Image changes, disappeared buttons and legacy counters are not positive rewards.
- Keep demonstration sessions separate across training and validation. PPO uses
  only fresh policy rollouts. Preserve checkpoint optimizer state, counters and
  configuration/action compatibility checks. Evaluation must not change weights.
- Never delete recordings to satisfy a storage budget. Pause collection instead.
- After changes to input, capture, rewards or learning, run
  `uv run python -m pytest -q`. Use simulation for unattended integration checks:
  `uv run python main.py train --simulate --steps 256 --checkpoints models/smoke`.
  Test resume and evaluation after checkpoint changes. Real gameplay acceptance
  requires calibration, collection, pretraining, a full online update and resume;
  simulations do not prove live safety or a lasting performance improvement.
- Keep recordings, calibration images, OCR models, checkpoints and logs out of Git.
  Refresh `uv.lock` and `requirements.txt` after dependency changes.
- `scrcpy --window-title "op"` will start the Android client in a window with the title "op".