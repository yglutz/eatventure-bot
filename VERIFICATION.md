# Verification, 2026-09-25

Implemented and checked on Windows with Python 3.13 and CPU PyTorch.

- `uv run python -m pytest -q`: 32 tests passed. Includes inclusive forbidden zones,
  continuous drag intersections, absolute coordinates, focus loss, release on
  errors, capture cleanup, OCR confidence/baselines, storage budget, calibration,
  separate-session imitation, an actual 256-step CNN/PPO update, optimizer reload,
  continuation to 512 steps and evaluation without weight changes. Additional
  tests cover focus-scoped hotkeys, wall-clock hold duration including safety
  checks, and reduced imitation weight for repeated idle clicks.
- CLI simulation: training to 256, resume to 512, and 16 evaluation steps passed.
  Artifacts are under ignored `models/smoke/` and `logs/smoke/`.
- TensorBoard served HTTP 200 on `http://127.0.0.1:6006/`; its scalar endpoint
  exposed session Gems/min, reward components, OCR availability and PPO losses.
- `git diff --check` and Python compilation passed.
- Real window `op` was found outside the sandbox. The captured client size was
  360 x 780. OCR read visible Gems values, including 466 and 489, with confidence
  above 0.99. Direct line recognition fixed digit splitting seen with the initial
  text-detection mode. Calibration and OCR models are local ignored artifacts.
- Two independent live demonstration sessions supplied six and three usable
  actions for ten epochs of imitation training. Interrupted actions were retained
  as audit records and excluded from imitation. This is a very small technical
  test dataset, dominated by helper clicks rather than station upgrades.
- A later session was interrupted by a changed client size/minimized window.
  Input stopped. Unknown views also blocked input as designed.

- A complete live PPO rollout/update finished at step 256 in about 489 seconds.
  Model tensors changed from the pretrained checkpoint, and the saved optimizer
  had 12 state entries. The checkpoint restored with four optimization epochs.
- Live resume collected 209 additional steps and saved at step 465 after an
  interrupt. The incomplete rollout was discarded: the checkpoint retained the
  weights from step 256 and its optimizer state. A second online update did not
  finish. Offline evaluation of the restored model left all weights unchanged.
- Both live runs recorded 0 confirmed new Gems. Each blocked one action when a
  new view appeared, then restricted actions to waiting. The new restaurant's
  804 Gems established a fresh baseline, without rewarding the overnight change.
- Global hotkeys could explain the interrupt while typing in chat. Z/P now apply
  only with game focus, and stop reasons are logged. This cause was not recorded
  by the old process and is therefore an inference.
- Collection now caps demonstration holds at one second; elapsed safety-check
  time is included. Idle/close-panel demonstrations have imitation weight 0.1,
  other actions weight 1. These changes are tested. A subsequent short live
  collection produced helper clicks and a scroll, but no usable upgrade hold.

The planned technical path (calibration, independent demonstrations, imitation,
one full live update, checkpoint resume and dashboard) has been exercised.
More targeted building/upgrade examples and broader safe-view calibration remain
necessary for useful gameplay. No lasting Gems/min improvement has been established.
Screenshot matching is a conservative calibrated whitelist, not a general semantic
dialog detector. The focus/hold/imitation refinements were made after the live
update; they do not imply that the existing checkpoint has learned station upgrades.
