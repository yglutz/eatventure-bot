# Supervised animated-gameplay calibration

The legacy whole-screen reference matcher stays unchanged. Animation-aware
recognition is an explicit, versioned opt-in; no old calibration silently gains
new tolerances. Input remains ADB tap-only through MouseController. Forbidden
zones, source/focus/size checks, action space, checkpoint compatibility, and
Gems reward rules are unchanged.

## Recognition

An animation profile uses 8–32 **operator-reviewed normal-gameplay PNGs** from
one layout, in chronological order. Training references must never include
verification holdout frames, menus, station dialogs, loading screens or purchase
views. There is no automatic reference learning or reference addition during play.

The profile uses hashes of approved references, distributed stable structural
anchors, bounded observed-color and spatial tolerance in explicitly reviewed
animation regions, and whole-screen tile/connected-residual checks. Large panels,
dimming, blank frames and wrong layouts are tested negatives. Numeric Gems and
an optional narrow cash-number crop are treated separately. A matching HUD alone
never authorizes gameplay.

Animation rectangles affect **recognition only**, not input permission. Safe
interaction rectangles remain a separate additional restriction. The current
profile schema supports a specific full-restaurant framing: restaurant animation
within x=[20,340), y=[80,621), numeric Gems within x=[225,280), y=[30,65), and an
optional numeric cash rectangle within x=[65,160), y=[30,61). Other framings need
separately reviewed supported profiles, not silently expanded regions.

## Create a disabled candidate

Supply approved PNG paths using repeated `--animation-reference` arguments.
Creation requires a **new directory** and does not overwrite an old calibration.
The approved references must all pass recognition and confident Gems OCR,
including a confirmed three-reading baseline. The result still has
`ocr_verified=false` and `readiness_verified=false`; collection/training cannot
load it until explicit approval.

Example in PowerShell using the already reviewed Docker-volume burst:

```powershell
$arguments = @('calibrate', '--backend', 'adb', '--adb-serial', '192.168.63.163:36753', '--adb-package', 'com.hwqgrhhjfd.idlefastfood', '--adb-connect', '--calibration', 'models/adb-animated-v1/calibration.json', '--gems-roi', '230', '40', '42', '20', '--interaction-roi', '70', '285', '210', '295', '--animation-roi', '20', '80', '320', '541', '--cash-roi', '75', '38', '80', '20')
for ($i = 0; $i -lt 12; $i++) { $arguments += @('--animation-reference', "/app/screenshots/adb-candidates/$i.png") }
docker compose --profile adb run --rm adb @arguments
```

These paths, address and coordinates describe one supervised session, not
universal defaults. Wireless ports can change. Use a new candidate directory for
a different layout or source. The Android connection banner is allowed to settle
for twelve seconds before ADB calibration captures.

## Verify without enabling input

```powershell
docker compose --profile adb run --rm adb calibrate --backend adb --adb-serial 192.168.63.163:36753 --adb-package com.hwqgrhhjfd.idlefastfood --adb-connect --calibration models/adb-animated-v1/calibration.json --verify-only --verification-checks 20 --negative-reference /app/screenshots/adb-connection-test.png
```

Verification-only never changes readiness flags or policy weights. It requires
at least 75% of fresh captures to be supported with Gems confidence >=0.95,
at least one consecutive equal three-reading confirmation, stable source/focus,
no confirmed decrease, and rejection of every supplied negative PNG. Unsupported
or failed captures break the Gems confirmation streak and never invoke OCR.
Every unsupported frame continues to deny runtime input, regardless of the
session's acceptance rate. The 75% criterion measures usefulness, not a safety
exception allowing input on the other 25%.

To approve deliberately, add `--approve-calibration` to that verification command.
Approval requires at least one negative PNG; testing several actual unsafe
views is preferable to relying only on a single known dialog. An approval attempt
first disables both readiness flags; failures leave them disabled. Successful
approval records version/source/counts and atomically enables the candidate.
Legacy calibrations cannot be rewritten with this approval operation. Runtime
checks require both readiness flags, a successful live/negative report, the
supported profile version and intact reference hashes.

This workflow never opens a station, menu or shop. Operators prepare negative
views manually; no app navigation or currency-spending code is introduced.

## Evidence and limits

- Twelve approved screenshots: 12/12 accepted.
- Twenty independent recorded gameplay holdouts: 15/20 accepted; five rejected
  conservatively. Holdouts were not used in profile fitting.
- Recorded station popup: rejected. A panel transplanted without the wireless
  banner: rejected. Synthetic dialogs, dimming and changed layouts: rejected.
- Fresh phone capture after manual scrolling to the beach/restaurant framing:
  0/20 accepted, correctly outside the approved original layout.
- New candidate at `models/adb-animated-v1/calibration.json` in the Docker volume
  remains disabled pending successful supervised live verification.
- Full regression suite: 223 passed. Required Windows simulated training,
  optimizer-preserving resume, evaluation without checkpoint changes, and Docker
  offline simulated training passed. Docker rebuild and CLI import checks passed.
- Windows tests and offline train/resume/evaluate, plus Docker offline training,
  are regression evidence; they do not prove rejection of every possible overlay
  or lasting game performance. No live gameplay input was sent during this work.

Small unfamiliar overlays within variable regions remain a limitation. Do not
relax thresholds simply to eliminate conservative rejections. Further layouts,
live negative checks, calibration, collection, pretraining, online update and
resume are needed before full live acceptance. Keep all screenshots, calibration
references, OCR models, logs and checkpoints outside Git.
