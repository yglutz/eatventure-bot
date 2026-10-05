# Legacy browser portal

The opt-in portal runs the existing legacy state machine in a private Linux
Xvfb/openbox desktop and displays scrcpy images in an authenticated browser.
It is intended for an **amd64/x86-64** host such as an N100 mini PC or Docker
Desktop using Linux containers. **Raspberry Pi/ARM is not supported by this
image**: its pinned Google platform-tools and scrcpy binaries are x86-64 only.

`portal.py` serves the browser; `portal_runtime.py` owns the device connection,
desktop, calibration and bot lifecycle. The browser image is **view-only**:
clicking it does not send game input. Portal buttons control provisioning,
calibration, Start and Stop. No trainer is loaded and no learning weights are
created. ONNX Runtime, Gems OCR and calibration dependencies remain installed;
the `legacy` image excludes PyTorch, Gym, Stable-Baselines3/SB3-Contrib and
TensorBoard.

## Start the service

Run these commands from the repository root with Docker Compose available.
Replace the password placeholder with a unique, randomly generated password of
at least 16 characters, and replace the example IP with the phone's actual IPv4
address. Do not bake passwords into the Dockerfile or commit them to the repo.

### Bash (Linux server shell)

```bash
export PORTAL_PASSWORD='REPLACE_WITH_A_UNIQUE_RANDOM_PASSWORD_16_PLUS_CHARACTERS'
export ADB_ALLOWED_HOSTS='192.168.1.50'
docker compose --profile portal up -d --build portal
docker compose --profile portal logs portal
```

### PowerShell (Docker Desktop, Linux containers)

```powershell
$env:PORTAL_PASSWORD = 'REPLACE_WITH_A_UNIQUE_RANDOM_PASSWORD_16_PLUS_CHARACTERS'
$env:ADB_ALLOWED_HOSTS = '192.168.1.50'
docker compose --profile portal up -d --build portal
docker compose --profile portal logs portal
```

Open `http://localhost:8080` and use HTTP Basic authentication with username
**`admin`** and the configured password. Missing or short passwords prevent
startup. Basic credentials are not encrypted by HTTP; use plain HTTP only on
localhost, with an SSH tunnel for remote access, or use an HTTPS reverse proxy.
Passwords and pairing codes are not logged by the portal. Docker administrators
can still see environment secrets through `docker inspect`; shell history may
also retain the setup commands.

Compose enables `init: true`, `restart: unless-stopped`, and publishes only
`127.0.0.1:8080:8080`. A service/container restart **never starts gameplay**.
There is no host display mount or remote desktop. The defaults cap the container
at one CPU and 2 GiB RAM, with rotated JSON logs (10 MiB per file, three files).

| Environment variable | Purpose / default |
| --- | --- |
| `PORTAL_PASSWORD` | Required password, at least 16 characters. |
| `ADB_ALLOWED_HOSTS` | Exact comma-separated IPv4 addresses, e.g. `192.168.1.50,192.168.1.51`. Empty by default: all device connections are denied. No hostnames, CIDR ranges or wildcards. |
| `PORTAL_ALLOWED_HOSTS` | Exact HTTP Host authorities; default `localhost:8080,127.0.0.1:8080`. This is distinct from the device allowlist. |
| `PORTAL_PUBLIC_ORIGIN` | Optional exact browser origin, e.g. `https://portal.example.com`, with no path or trailing slash. Pins POST origins for a proxy. |
| `PORTAL_DISPLAY` | Private local X11 display; default `:99`. |
| `PORTAL_CPUS` / `PORTAL_MEM_LIMIT` | Compose resource limits; defaults `1.0` / `2g`. |
| `PORTAL_LOG_MAX_SIZE` / `PORTAL_LOG_MAX_FILE` | Compose log rotation; defaults `10m` / `3`. |

The device allowlist also rejects loopback, multicast, link-local and unspecified
addresses. Changing Compose environment values requires recreating the service;
set them again in the shell used to run Compose.

### Remote viewing

Recommended: set up the service on the Linux server using its shell, then run
this on your local computer:

```bash
ssh -L8080:127.0.0.1:8080 user@host
```

Keep the SSH session open and browse to `http://localhost:8080`. The default Host
allowlist works with this tunnel. For an HTTPS reverse proxy, set
`PORTAL_PUBLIC_ORIGIN` to its exact public HTTPS origin and configure
`PORTAL_ALLOWED_HOSTS` to include the exact Host authority forwarded by the
proxy. The proxy must preserve Basic authentication and the intended Host;
forwarded headers do not automatically establish trust. Keep the backend bound
to localhost.

## Pair and connect the phone

1. Enable Android wireless debugging and put the host and phone on a reachable
   network. Allowlist the phone's current IPv4 address before starting the service.
2. On the phone, open **Pair device with pairing code**. Enter its pairing
   `IPv4:port` and current six-digit code in the browser Pairing form.
3. Use the **connection** `IPv4:port` shown on the main wireless-debugging screen
   in the connection form. **Pairing and connection ports are distinct** and can
   change. Enter the expected foreground game package; the form defaults to
   `com.hwqgrhhjfd.idlefastfood`, which you must check for your installation.
4. Wake/unlock the phone and **manually open Eatventure in normal gameplay**.
   Connect, then inspect the browser image and status. Connection alone leaves
   the bot stopped.

The runtime binds scrcpy to the selected serial and checks the expected Android
foreground package. It uses pinned **scrcpy 3.3.4**, `--max-size 800`,
`--max-fps 10`, `--video-bit-rate 2M`, software rendering, `--no-audio`,
`--no-power-on` and disabled clipboard autosync. The private desktop is 640x960;
the calibrated client view is 360x780. The portal does not wake the phone or
launch the game automatically.

## Calibrate, inspect, verify, then Start

Start requires a newly approved, source-bound calibration and a fresh supported
gameplay capture. Coordinates are **client pixels**, expressed as
`x,y,width,height`; separate multiple rectangles with semicolons in the form.
Do not copy the form placeholders as device-independent calibration values.

1. While stopped, show normal gameplay with no dialog, shop, purchase or loading
   overlay. Inspect the actual screenshot and identify:
   - **Gems ROI** (`gems_roi`): only the numeric Gems counter. Compare OCR/status
     readings against the known visible number; unknown is not zero.
   - **Interaction rectangles** (`interaction_rois`): reviewed safe gameplay
     areas, excluding menus, currency displays, purchases and all forbidden zones.
   - **Animation rectangles** (`animation_rois`): reviewed animated gameplay
     regions. At least one is required by the portal runtime.
   - **Cash ROI** (`cash_roi`, optional): the actual cash display region, if used.
   These values depend on the device, layout and letterboxing. They are not
   universal rectangles.
2. Submit calibration. It captures **eight frames** of normal gameplay and
   creates a **disabled candidate at a new path** under `models/portal/`.
   Reference images go under `screenshots/portal/`; filenames do not constitute
   approval.
3. **Click the authenticated `/api/calibration-preview` link** (“Alle acht
   Referenzbilder prüfen”). Inspect the contact sheet containing all eight
   frames. Every frame must show normal gameplay without a dialog or unsafe
   overlay. If any frame is unsuitable, return to normal gameplay and create a
   new candidate before continuing.
4. Collect a negative sample separately. With the bot stopped, **manually open
   an unsafe dialog on the phone**, tick **Negativprobe**, leave **Freigabe
   bestätigen unchecked**, and submit the verification form. The view must be
   rejected, and the negative image is stored separately. At least one negative
   sample is required for approval. This manual operator step does not add
   automated shop navigation. If the unsafe view is accepted, the candidate
   fails and must not be approved.
5. **Manually close the popup** and restore normal gameplay. Untick
   **Negativprobe**, tick **Freigabe bestätigen**, and submit verification.
   Approval tests **20 new, independently captured frames** rather than reusing
   the eight references. It requires a stable source, at least **75% (15/20)**
   supported high-confidence Gems readings (confidence >= 0.95), at least one
   confirmed identical three-reading streak, no confirmed Gems decrease, and
   rejection of every supplied negative sample.
6. Check that status reports approval and `ready-stopped`. Only then click
   **Start**. The Start button does not bypass calibration if pressed early.

The 75% verification threshold **does not permit input on the unsupported 25%**.
Every input still needs a fresh supported view and safe path. Approval never
expands the static input permissions. Source identity includes the device serial,
package, geometry, display and **X11 window ID**. Reconnects and restarts require
new calibration review/approval and another explicit Start; persisted files do
not auto-authorize a new window.

## Runtime safety and controls

All clicks, holds and scrolling/continuous drag paths pass through
`MouseController`. Static forbidden zones in `config.py`, including the bottom
area and **inclusive boundaries**, remain binding. Calibration can add
restrictions but cannot weaken them. Whole drag paths must be safe, not just
their endpoints.

An unapproved/changing view, capture failure, focus loss, changed client geometry
or confirmed Gems decrease causes input rejection/safety pause. Gems confirmation
requires three consecutive readings at confidence >= 0.95. Unknown readings are
not treated as zero; restart/manual pause/measurement timeout resets the baseline.
Image changes and legacy counters are not positive Gems rewards. Buttons are
released on interruption, pause and exit.

Use browser **Stop** for an emergency pause. It is handled without queuing behind
calibration/provisioning; a generation check also prevents a concurrent Start
from re-enabling gameplay after Stop. Stop revokes gameplay permission; it does
not imply every long-running provisioning/calibration request has finished.
**Disconnect** tears down the private desktop/device session. To stop the service:

```bash
docker compose --profile portal stop portal
```

The legacy state machine can request blind navigation, but those requests cannot
bypass the same view/path restrictions. **The original bot's full-game
performance has not been demonstrated through this restricted portal.** There
is no added shop navigation, purchases or Gems spending.

## Persistence and low-power expectations

Compose named volumes persist ADB keys (`adb-keys` at `/root/.android`), calibration
and local OCR models (`models`), screenshots and logs. Generated artifacts under
`models/`, `screenshots/`, `logs/` and `data/` are Git-ignored; volume-resident keys
are outside the repository. Calibration collection stops at its storage budget
rather than deleting recordings. Preserve the volumes when stopping/recreating
the service; `docker compose down -v` deletes them.

An N100 or other x86 mini PC is a possible low-power host. A reported Docker
Desktop **idle** observation was **0.07% CPU and 89.85 MiB RAM**. This is not a
24/7 wall-power measurement or an active scrcpy/OCR/gameplay load measurement.
Measure the actual host/device workload before making consumption estimates.

## Health and developer diagnostics

Check Compose process state/logs and authenticated browser status:

```bash
docker compose --profile portal ps portal
docker compose --profile portal logs portal
docker stats --no-stream
```

`/api/status` reports connection, approval, running state, phase, error, Gems and
recent action results. HTTP requests require Basic authentication and an allowed
Host; mutation requests additionally require the session CSRF token and exact
allowed Origin. JSON bodies are bounded to 32 KiB with route-specific fields.
The API accepts no arbitrary file paths or shell arguments. The browser supplies
the CSRF token automatically.

Run the development suite on Windows/Python 3.13 with uv:

```powershell
uv run python -m pytest -q
```

For an isolated real-X11 diagnostic, build the legacy image as above, then run
the following from the repository root. The script is mounted **read-only**,
network access is disabled, and the explicit opt-in flag is required.

```bash
docker run --rm --network none \
  --mount "type=bind,source=$(pwd)/x11_smoke.py,target=/app/x11_smoke.py,readonly" \
  --entrypoint /opt/venv/bin/python eatventure-bot:legacy \
  /app/x11_smoke.py --run-isolated-synthetic
```

```powershell
docker run --rm --network none --mount "type=bind,source=$($PWD.Path)\x11_smoke.py,target=/app/x11_smoke.py,readonly" --entrypoint /opt/venv/bin/python eatventure-bot:legacy /app/x11_smoke.py --run-isolated-synthetic
```

This diagnostic creates its **own Xvfb server and synthetic window**, refuses an
existing display, and sends real XTest events only to that owned window through
`MouseController`. It checks pixel decoding, clicks, holds, drags, forbidden-path
rejection, release on focus loss and latched size-change rejection. It uses a
scoped dummy Android guard: no ADB, scrcpy, phone or device input is involved.

Verified for this documentation update: the Windows full suite passed
**502 tests**; the isolated X11 diagnostic returned `ok: true` with no errors.
These checks demonstrate the tested software/synthetic input behavior.
**The actual wireless pairing → scrcpy → calibration → gameplay flow remains
unverified with a real phone; no real-device input was used for these checks.**
They do not prove live safety, full-game completion or lasting performance gains.
