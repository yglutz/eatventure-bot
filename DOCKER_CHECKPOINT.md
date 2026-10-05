# Docker / Wi-Fi ADB checkpoint

This milestone adds a Linux/Python 3.13 container and a selectable direct ADB
screenshot backend. scrcpy and a desktop are not needed for the ADB backend.
The existing Windows/window backend remains the default. This is not a claim
of live gameplay acceptance or improved learning performance.

## Capabilities and safety limits

For animation-heavy scenes, [ANIMATED_CALIBRATION.md](ANIMATED_CALIBRATION.md)
documents a versioned opt-in recognizer and disabled-candidate approval workflow.
It does not silently change existing calibration matching or input restrictions.

- Screenshots: serial-bound `adb exec-out screencap -p`, decoded as BGR and
  resized to the existing 360 × 780 coordinate system. No navigation bars are
  automatically cropped; calibrate against the actual resized full screenshot.
- Input: **taps only**, exclusively through `MouseController`. Holds and drags
  are rejected and masked out. A plain ADB swipe cannot reliably release on
  pause/focus loss, so it is deliberately not used as a replacement.
- Foreground package, unlocked/awake device, screenshot success, supported
  calibrated view, safe interaction ROI and every original forbidden zone are
  required. Unknown device state fails closed. No app-launch, shop or purchase
  commands are introduced.
- Native resolution changes latch a failure. ADB calibrations bind the serial,
  package and native resolution across restarts. Use a **separate directory**
  for ADB calibration; Windows calibrations cannot authorize ADB input.
- The policy/checkpoint action space stays compatible. Existing policy weights
  can load, but unavailable hold/drag actions remain masked. Tap-only operation
  is not equivalent to the original bot's full capabilities.
- A tap already dispatched to Android cannot be recalled. No remote focus
  check is atomic with dispatch. Timeout completion is unknown, never retried;
  this remains a live-safety limitation requiring supervised acceptance.
- Gems rewards and confirmation rules are unchanged. Unknown is not zero;
  confirmed decreases pause for review. Recordings are never deleted for budget.

## Connect over Wi-Fi

Enable developer options and USB debugging, authorize your own trusted computer.
Keep ADB on a trusted LAN only: do not expose its port to the internet.

For conventional TCP/IP ADB, while connected by USB:

```powershell
adb devices -l
adb -s YOUR_USB_SERIAL tcpip 5555
adb connect 192.168.1.50:5555
adb -s 192.168.1.50:5555 get-state
```

For Android Wireless debugging (Android 11+), use the phone's **pairing** port
with `adb pair HOST:PAIRING_PORT`, then its separate **connection** port with
`adb connect HOST:CONNECTION_PORT`. Ports can change. Pairing is an operator
step, not an application command. For containers, pair with the container's ADB
key (instructions below); host authorization alone does not authorize a new key.
Open the game manually and supply its exact Android package; the backend never
chooses a device or package implicitly.

## Native uv usage (Windows or Linux)

```powershell
uv sync --locked
uv run python main.py calibrate --backend adb --adb-serial 192.168.1.50:5555 --adb-package YOUR_GAME_PACKAGE --adb-connect --calibration models/adb/calibration.json --gems-roi X Y W H --interaction-roi X Y W H
uv run python main.py calibrate --backend adb --adb-serial 192.168.1.50:5555 --adb-package YOUR_GAME_PACKAGE --adb-connect --calibration models/adb/calibration.json --verify-only
uv run python main.py collect --backend adb --adb-serial 192.168.1.50:5555 --adb-package YOUR_GAME_PACKAGE --adb-connect --calibration models/adb/calibration.json --data data/adb-demonstrations --start --steps 256 --max-seconds 600
uv run python main.py train --backend adb --adb-serial 192.168.1.50:5555 --adb-package YOUR_GAME_PACKAGE --adb-connect --calibration models/adb/calibration.json --checkpoints models/adb-checkpoints --start --steps 256 --max-seconds 600
```

Replace every placeholder with operator-measured coordinates in the **360 × 780
full screenshot**, not native phone pixels. Select only gameplay areas, never
currency, shops or purchase controls. Repeat `--interaction-roi` for more areas.
Calibration does not spend currency or send input. It requires a confident OCR
measurement and writes reference screenshots. Repeat calibration for additional
safe views; forbidden zones always remain binding.

ADB sessions accept `z` + Enter to start/pause and `p` + Enter to stop on stdin.
Ctrl+C stops; SIGTERM requests orderly release/checkpoint cleanup. Use `--start`
for intentional headless starts and `--max-seconds` for bounded sessions. A
confirmed Gems decrease still pauses even with `--start`; operator `z` begins a
new baseline. Without stdin, a paused session waits until timeout or termination.
Pretraining stays offline: `main.py pretrain --data data/adb-demonstrations ...`.
Resume training using `--resume PATH`; evaluation requires `--resume PATH` and
does not save altered policy weights.

## Docker

Requires a running Docker **Linux x86-64 (amd64)** engine. The image uses locked dependencies
and Linux CPU PyTorch. Google's pinned platform-tools **36.0.2** provide ADB
pairing support; the older Debian Bookworm ADB package is intentionally not used.
The build rejects ARM64 rather than installing incompatible executables.
No GUI, GPU, USB passthrough, privileged mode or host
networking is required. Wi-Fi serials must be reachable from the container's
bridge network (VPN/firewall settings may block this).

```powershell
docker compose build
docker compose run --rm bot
```

The default service performs only offline simulation and has networking disabled.
Models, recordings, logs, screenshots and ADB private keys live in named volumes,
not the image or Git. Never use `docker compose down -v` if you need this data.

For the opt-in networked service:

```powershell
$env:ADB_SERIAL = '192.168.1.50:5555'
$env:ADB_PACKAGE = 'YOUR_GAME_PACKAGE'
# Optional wireless pairing, with credentials kept in the named key volume:
docker compose --profile adb run --rm --entrypoint adb adb pair HOST:PAIRING_PORT
# Initial authorization may require accepting the key on the phone.
docker compose --profile adb run --rm adb calibrate --backend adb --adb-serial $env:ADB_SERIAL --adb-package $env:ADB_PACKAGE --adb-connect --calibration models/adb/calibration.json --gems-roi X Y W H --interaction-roi X Y W H
# Default ADB service is verification-only, never automatic training:
docker compose --profile adb run --rm adb
docker compose --profile adb run --rm adb train --backend adb --adb-serial $env:ADB_SERIAL --adb-package $env:ADB_PACKAGE --adb-connect --calibration models/adb/calibration.json --checkpoints models/adb-checkpoints --start --steps 256 --max-seconds 600
```

Stop a supervised run with Ctrl+C, or use `docker compose stop`. Allow enough
shutdown time to save the optimizer/checkpoint; force-killing the container can
interrupt a checkpoint write. Container commands override the entire default
command, so explicitly include backend and calibration flags each time.

## Verification status

Verified in this Windows workspace on 2026-10-03:

| Check | Result |
|---|---|
| `uv run python -m pytest -q` | 135 passed |
| `uv lock --check` / Windows `uv sync --locked` | Passed |
| Default and ADB-profile Compose configuration | Passed |
| Required 256-step simulated training | Passed |
| Resume to 512 steps and simulated evaluation | Passed |
| `docker compose build` | Passed, including rebuilt platform-tools 36.0.2 image |
| Container `adb version` and pairing-command availability | Passed: 36.0.2-14143358; `pair HOST` supported |
| Container learning CLI `train --help` | Passed |
| Linux training / live Wi-Fi capture / live taps | Not verified |

Simulation artifacts are in ignored `models/smoke` and `models/docker-smoke`.
No live device input was sent for these checks. No Git commit or tag was created.

Mocked ADB tests cover transport errors, PNG capture, device focus/lock state,
resolution changes, serial binding, forbidden zones, coordinate rounding,
observer callbacks, capability masks and headless CLI import paths.
Offline training/resume/evaluation are required regression checks, **not live
acceptance**. Docker build and real Wi-Fi operation must be verified with a
running Linux engine and an explicitly authorized device. Full live acceptance
still needs supervised calibration, collection, pretraining, an online update
and resume. Tap-only collection may produce fewer useful demonstrations.
