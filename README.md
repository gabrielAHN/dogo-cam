# Dogo Cam

Manual Raspberry Pi dog camera: Flask web UI, live Picamera2 stream, pan/tilt MG90S servos (keyboard, on‑screen arrows, mobile touch‑drag), temperature/humidity readout (local DHT22 or Home Assistant), and optional Cloudflare Tunnel. Manual‑only (tracking removed).

![Dog stream UI](img/dog-stream.png)

## Layout

- `dogcam_stream.py` — Flask app + camera endpoints (+ lux metering over MQTT)
- `servo_control_rpigpio.py` — MG90S pan/tilt control
- `ky004-control.py` — optional GPIO on/off switch + remote power over MQTT
- `dogcam_mqtt.py` — shared MQTT helper for the two scripts above (see [House modes](#house-modes-remote-power-and-lux-over-mqtt))
- `templates/` — camera UI + login
- `service_startup/` — example systemd units
- `deploy/` — CI deploy script + scoped sudoers
- `PIN_DIAGRAM.md` — full wiring reference

## Hardware

Raspberry Pi 3B (Pi OS) + **wide-angle NoIR CSI camera (OV5647 sensor, no IR-cut filter)** + 2× MG90S servos, plus optional GPIO toggle switch, DHT22 (`GPIO4`), and cooling fan.

![Raspberry Pi camera build](img/raspberry-pi-cam.png)

| Part | Signal / V+ / GND pins | Notes |
|------|------------------------|-------|
| NoIR wide-angle camera | ribbon → CSI port | OV5647 sensor; sees in the dark under IR, wide FoV. Dedicated camera port, not GPIO |
| Tilt servo (servo1) | `GPIO18` (Pin 12) / Pin 2 (5V) / Pin 14 | |
| Pan servo (servo2) | `GPIO19` (Pin 35) / Pin 4 (5V) / Pin 39 | |
| Toggle switch | `GPIO17` (Pin 11) / Pin 17 (3.3V) / Pin 25 | 3‑pin module |
| DHT22 (optional) | `GPIO4` (Pin 7) / Pin 1 (3.3V) / Pin 9 | |
| Cooling fan | Pin 4 (5V, split with pan) / Pin 6 | |

Servos and fan draw from the Pi 5V rail with shared ground. The mount is inverted, so select **Rotate 180°** in the web UI's ⚙ Camera Controls; the choice persists across service restarts and reboots. See `PIN_DIAGRAM.md` for the full pinout.

> **Camera note:** the sensor reports as a plain `ov5647` regardless of the lens/filter, so software can't tell it's the NoIR wide-angle module — the NoIR behaviour is configured, not auto-detected. See [Camera capabilities](#camera-capabilities) for the tuning, day/night and image config.

**Switch** (`ky004-control.py`): `SWITCH_MODE=auto` probes `GPIO17` at startup. A floating pin means no switch is attached, so the camera is kept on and restarted if it stops. A connected switch keeps the existing behavior: ON (`GPIO17` low) starts `dog-stream` (and `cloudflared-tunnel` if enabled); OFF stops them cleanly. Use `SWITCH_MODE=gpio` to force hardware control or `SWITCH_MODE=always-on` to bypass GPIO explicitly. Set `SWITCH_ON_VALUE=1` if your module is inverted, or `SWITCH_PIN` for a different GPIO. Home Assistant can additionally hold the camera off remotely; see [House modes](#house-modes-remote-power-and-lux-over-mqtt).

## Controls

- Desktop: arrow buttons, `↑ ↓ ← →`, or `W A S D`
- Mobile: tap/hold above/below center (tilt) or left/right (pan)

## Environment

Copy `.env.example` to `.env` on the Pi (never commit it). The most relevant settings:

```env
SECRET_KEY=replace_me
MAX_VIEWERS=3
PORT=5000
STREAM_MAX_FPS=15          # framerate cap; lower = less power draw (see Power & stability)
STREAM_IDLE_TIMEOUT=10      # stop acquisition/encoding after the final viewer leaves
STREAM_WIDTH=1296          # stream resolution (default 1296x972, was 640x480)
STREAM_HEIGHT=972          #   1080p works but VGA/960p are gentler on the 5V rail
DOG_NAME=Kotaro
DOGCAM_CAMERA_VIEW=normal  # initial fallback; the saved gear-menu choice wins
CAMERA_VIEW_STATE_FILE=/var/lib/dogcam/camera-view.json
# --- NoIR wide-angle camera: colour tuning + day/night ---
CAM_TUNING_FILE=ov5647_noir.json  # NoIR tuning kills the daylight magenta cast; "" = sensor default
CAM_SHARPNESS=1.5          # ISP tuning (0-16); also CAM_CONTRAST/CAM_SATURATION/CAM_BRIGHTNESS/CAM_EV
CAM_NOISE_REDUCTION=fast   # off|fast|high_quality|minimal|zsl
CAM_SAT_BRIGHT=1.0         # saturation in real daylight (>= CAM_BRIGHT_LUX)
CAM_SAT_DIM=0.7            # saturation at the day/night edge (artificial light = IR false colour)
CAM_BRIGHT_LUX=400         # lux considered real daylight
NIGHT_MAX_FPS=6            # night mode lowers fps so exposure can lengthen (real sensitivity)
CAM_NIGHT_EV_MAX=1.0       # EV bias at total darkness (scaled to 0 at DAYNIGHT_NIGHT_LUX)
DAYNIGHT_AUTO=1            # auto colour(day)/grayscale(night) off the camera Lux meter
DAYNIGHT_NIGHT_LUX=5       # below this (sustained DAYNIGHT_NIGHT_AFTER=45s) -> night
DAYNIGHT_DAY_LUX=12        # above this (sustained DAYNIGHT_DAY_AFTER=8s) -> day; eager to show colour
DAYNIGHT_MODE=auto         # startup override: auto|day|night
CAM_ZOOM=1.0               # digital zoom 1.0-CAM_ZOOM_MAX(4.0) via ScalerCrop
SWITCH_MODE=auto            # auto|gpio|always-on
SWITCH_PIN=17
SWITCH_ON_VALUE=0
TEMP_SOURCE=sensor         # or ha
ENABLE_CLOUDFLARED=1       # 0 when a reverse proxy owns the domain
```

See `.env.example` for the full list (servo tuning, Home Assistant, Cloudflare, proxy‑trust flags).

**Reverse‑proxy mode** — when another host (e.g. a Mac mini running Traefik/Authelia/Cloudflare) owns the public domain and proxies to the Pi: set `ENABLE_CLOUDFLARED=0`, `TRUST_PROXY_HEADERS=1`, and (behind Authelia) `TRUST_PROXY_AUTH_HEADERS=1` so a `Remote-User` header is trusted. Configure the proxy-only navigation links with `DOGCAM_HOME_URL=https://portal.example/` and `DOGCAM_LOGOUT_URL=https://auth.example/logout`; they are rendered only for trusted proxy-authenticated requests, while a local login keeps only its local Logout action. Camera controls are limited to users in `DOGCAM_CONTROL_GROUPS`; others can view only. Set `DOGCAM_VIEW_GROUPS` (e.g. `dogo_viewers`) to also re-check the viewer group; unset, any proxy-authenticated user may view. Keep all `TRUST_PROXY_*` at `0` in standalone mode.

**Trust boundary.** `Remote-*` and `X-Forwarded-*` headers are believed only when the TCP peer is listed in `TRUSTED_PROXY_ADDRS` (IPs/CIDRs; default `127.0.0.1,::1` for the watchdog). Add exactly the proxy's address, e.g. `TRUSTED_PROXY_ADDRS=127.0.0.1,::1,192.168.100.1` when the proxy reaches the Pi over Nebula; never a LAN range. Requests from any other peer are treated as anonymous, whatever headers they carry. Also keep gunicorn off the LAN: the systemd unit binds only `127.0.0.1:5000` and the Pi's Nebula address `192.168.100.10:5000`, never `0.0.0.0`.

**Temperature source** — `/temp` reads a local DHT22 (`TEMP_SOURCE=sensor`, needs `adafruit_dht`, wired to `GPIO4`) or Home Assistant (`TEMP_SOURCE=ha` + `HA_URL`/`HA_TOKEN`/`HA_*_ENTITY` using an HA long‑lived token). HA mode skips the DHT22 dependency.

## House modes: remote power and lux over MQTT

Home Assistant's house modes (Away turns the camera off, Dog keeps it on and drives the light from the room's brightness) talk to the Pi through retained MQTT topics on the broker at `DOGCAM_MQTT_HOST`:`DOGCAM_MQTT_PORT` (anonymous). MQTT is opt-in: set `DOGCAM_MQTT=1` in the environment file that both `button-control` and `dog-stream` load (`/etc/dogo-cam.env` on the live Pi, `.env` in the example units). Without it neither script creates a client, so tests and bench runs never reach the broker. Set payloads are plain text, matched case-insensitively after trimming whitespace; an empty payload (a cleared retained message) means the default, and anything else is ignored (logged once) while the current mode is kept.

| Topic | Direction | Payload |
|---|---|---|
| `jg-casa/dogo-cam/power/set` | HA → `ky004-control.py` | `ON` / `OFF`, retained. Default `ON`. |
| `jg-casa/dogo-cam/power/state` | `ky004-control.py` → HA | Retained JSON `{"remote": "ON", "switch": "ON", "camera": "ON", "t": 1762059999}` on every change, on (re)connect and every 300 s. `switch` is `ON` in always-on mode; `camera` is the effective state. |
| `jg-casa/dogo-cam/status` | `ky004-control.py` → HA | Retained `online` on connect; the broker publishes the retained LWT `offline` if the script disappears. |
| `jg-casa/dogo-cam/meter/set` | HA → `dogcam_stream.py` | `ON` / `OFF`, retained. Default `OFF`. |
| `jg-casa/dogo-cam/lux` | `dogcam_stream.py` → HA | Retained JSON `{"lux": 12.3, "t": 1762059999, "source": "stream"}` after every reading while metering is ON; `"source"` is `"stream"` or `"sample"`. When metering turns OFF, an empty retained message deletes the last reading. |

**Remote power** (`ky004-control.py`, MQTT client id `dogo-cam-power`). The camera runs when the hardware switch is ON **and** the remote value is not `OFF`; in switch-less always-on mode the remote value alone decides. A remote change is applied on the control loop's next 0.1 s tick through the same start/stop path as the switch, and the 5 s reconcile enforces the effective state, so a remote `OFF` stays off. The power LED shows the effective state. Each remote value received from the broker is written atomically to `DOGCAM_REMOTE_POWER_FILE` (default `/var/lib/dogcam/remote-power`, one word: `ON` or `OFF`). At startup the script loads that file, then waits up to 5 s for the broker's retained value before its first start/stop; if the broker can't be reached it keeps the file's value, and with no file it uses `ON`. `dogcam-watchdog.sh` reads the same file (if you override `DOGCAM_REMOTE_POWER_FILE`, set it in the `.env` that both `button-control` and the watchdog load): while it holds `OFF` (trimmed, any case) the watchdog exits without probing or restarting `dog-stream`, so it does not start the service ky004 stopped. It logs `camera off by remote` once per OFF period, not on every 30 s run: the first OFF run creates the stamp file `DOGCAM_REMOTE_OFF_STAMP` (default `/run/dogcam-remote-off`, cleared at boot), and any later run that does not see `OFF` removes it, so the next OFF is logged again. A missing, unreadable or unrecognised file leaves the watchdog unchanged.

**Lux metering** (`dogcam_stream.py`, MQTT client id `dogo-cam-stream`; it runs only while the camera service is up). While metering is ON, a background thread publishes a reading at least every `DOGCAM_METER_INTERVAL` seconds. If the camera is already recording for viewers and its AE `Lux` reading is under 15 s old, that reading is published (`"source": "stream"`). Otherwise the meter takes a short sample the way `/snapshot` does: it takes a snapshot stream demand (starting an idle camera), waits up to 5 s for camera metadata carrying `Lux`, and releases the demand on every path (`"source": "sample"`), so `STREAM_IDLE_TIMEOUT` stops the camera again afterwards. When the sample itself started the camera, it ignores `Lux` from the first 1.0 s of frames while auto-exposure settles; that second counts toward the 5 s. When `meter/set` turns metering from ON to OFF, the script publishes an empty retained message to `jg-casa/dogo-cam/lux`, so the broker no longer hands an old reading to Home Assistant (for example after an HA restart); a reading still in progress at that moment is dropped. Metering adds no HTTP routes, and nothing else happens while it is OFF.

| Variable | Default | Description |
|---|---|---|
| `DOGCAM_MQTT` | unset (off) | Opt-in. Only `1`, `true`, `yes` or `on` (any case) turns MQTT on. Unset or any other value (tests, bench) means no client, no meter thread, and paho is never imported. |
| `DOGCAM_MQTT_HOST` | `192.168.50.211` | Broker host. |
| `DOGCAM_MQTT_PORT` | `1883` | Broker port. |
| `DOGCAM_MQTT_POWER_CLIENT_ID` | `dogo-cam-power` | Client id of `ky004-control.py`; must be unique on the broker. |
| `DOGCAM_MQTT_STREAM_CLIENT_ID` | `dogo-cam-stream` | Client id of `dogcam_stream.py`; must be unique on the broker. |
| `DOGCAM_REMOTE_POWER_FILE` | `/var/lib/dogcam/remote-power` | Persisted remote power value. The directory must be writable by the `button-control` service user. |
| `DOGCAM_METER_INTERVAL` | `60` | Longest gap in seconds between lux readings while metering is ON (minimum 1). |

`paho-mqtt` is a project dependency (`pyproject.toml`, and pinned to 2.1.0 in `requirements.txt`); the helper also accepts the 1.x client API. Install it into the interpreter that runs each script. The deployed units run both scripts from the project virtualenv, not the system `python3`: `button-control.service` starts `<checkout>/.venv/bin/python ky004-control.py` (for example `/opt/dogo-cam/.venv/bin/python /opt/dogo-cam/ky004-control.py`) and `dog-stream.service` starts that venv's `gunicorn`, so installing the requirements into the venv (`uv sync`, or `.venv/bin/pip install -r requirements.txt`) covers both. The example `service_startup/button-control.service` still starts `/usr/bin/python3`; point its `ExecStart` at the venv's `python` (or give the system interpreter paho, e.g. `sudo apt install python3-paho-mqtt`). Without paho, or with the broker unreachable, both scripts keep running: remote power follows the persisted file, and metering stays OFF until a `meter/set` `ON` arrives.

## Camera capabilities

The sensor is an **OV5647 behind a wide‑angle NoIR (no IR‑cut filter) lens**. NoIR sees in the dark under IR light, but has two quirks this app handles:

- **Daytime magenta cast.** With no IR‑cut filter, IR leaks into the red/blue channels and standard `ov5647.json` can't correct it (AWB alone leaves it purple). `CAM_TUNING_FILE=ov5647_noir.json` loads the NoIR colour‑correction matrix and neutralises it. *The tuned `Picamera2` must be constructed before any other camera‑manager call — `global_camera_info()` first silently pins the default tuning.*
- **Automatic day/night.** `DAYNIGHT_AUTO=1` runs **colour by day, grayscale by night** (colour is pure noise under IR in the dark), switched off the camera's own AE **Lux** meter — no extra hardware. Two thresholds (`DAYNIGHT_NIGHT_LUX` / `DAYNIGHT_DAY_LUX`) with per‑direction sustain times give hysteresis; recovery to day is eager and descent to night is lazy, so a lit room always shows colour.
- **Light‑adaptive tuning.** Under artificial light IR reflects unevenly (green ceiling, magenta fabric) — no white balance fixes it and saturation only amplifies it. So saturation scales with lux (`CAM_SAT_DIM` at the day edge → `CAM_SAT_BRIGHT` in real daylight). Night mode drops to `NIGHT_MAX_FPS` so exposure can lengthen (real light gathering, less power) and adds an EV bias that scales with darkness (a fixed boost blows out a lit room).

All image tuning (`CAM_*`), day/night switching and digital zoom are **ISP‑side `set_controls()` — no pipeline restart and no measurable extra power** (bench‑tested: identical `vcgencmd get_throttled` at VGA vs 1296×972 vs 1080p under a live viewer; the `STREAM_MAX_FPS` cap, not pixel count, bounds the draw). Endpoints (control actions need `DOGCAM_CONTROL_GROUPS`; GETs are view‑only):

| Endpoint | Method | Purpose |
|---|---|---|
| `/camera/info` | GET | active resolution, fps cap, orientation, zoom, tuning, day/night mode + lux |
| `/camera/view` | GET / POST | read / set persisted orientation `{"view":"normal\|upside_down"}`; changes restart the camera pipeline |
| `/camera/daynight` | GET / POST | read status / set override `{"mode":"auto\|day\|night"}` |
| `/camera/zoom` | GET / POST | read / set digital zoom `{"zoom":2.0}` or `{"step":0.5}` |
| `/snapshot` | GET | fresh frame as a still JPEG (temporarily starts an idle camera) |

**In the web UI**, the ⚙ settings button opens Camera Controls. The **Camera orientation** row switches between **Normal** and **Rotate 180°**, restarts the camera pipeline to apply the transform, and persists the choice across service restarts and reboots. The **Day / Night** row provides **🔄 Auto** (follow the light sensor, the default), **☀️ Light** (force colour), and **🌙 Dark** (force grayscale). Camera-control permission (`DOGCAM_CONTROL_GROUPS`) is required to change either setting; everyone else sees them read-only.

The **Show title** switch at the bottom of Camera Controls hides or shows the "<name> Stream" title. It needs no permission and is remembered per device (browser `localStorage`), so hiding it on a phone leaves other screens unchanged.

**On phones** (width ≤ 640px, or a landscape phone: height ≤ 500px on a touch screen) the top bar is a single row: a small status pill (e.g. ⚪ Idle, 🟢 Live) and four 44px icon buttons (Home, Logout, ⚙, ⛶). The title sits on one line below it, the temperature shrinks to one line, and Camera Controls packs the tilt/pan pad side by side so a 393×852 phone shows every control without scrolling. Overlays stay clear of the notch in landscape. Desktop, including short desktop windows, keeps the text buttons and full status text.

### Camera configuration reference

Every knob is an env var in `.env` with a safe default — nothing below is required, and unset values keep the sensor/ISP default. Change them without touching code; a restart applies resolution/tuning-file changes, while day/night, saturation, zoom and EV adjust live. The **Default** column is the value shipped in `.env.example` (a few internal code fallbacks differ slightly).

**Resolution & tuning**

| Variable | Default | Description |
|---|---|---|
| `STREAM_WIDTH` × `STREAM_HEIGHT` | `1296` × `972` | Stream resolution. 640×480 / 800×600 are lighter; 1920×1080 works. Pixel count barely affects the 5V rail at a fixed fps. |
| `STREAM_MAX_FPS` | `15` | Framerate cap — the real power lever (higher = more brown-out risk). |
| `CAM_TUNING_FILE` | `ov5647_noir.json` | libcamera tuning file. NoIR file removes the daylight magenta cast; set empty (`CAM_TUNING_FILE=`) for the sensor default. |
| `DOGCAM_CAMERA_VIEW` | `normal` | Initial `normal` or `upside_down` fallback before a UI choice is saved. |
| `CAMERA_VIEW_STATE_FILE` | `/var/lib/dogcam/camera-view.json` | Persistent orientation selected from Camera Controls; takes precedence over `DOGCAM_CAMERA_VIEW`. |

**Image quality (ISP, ~no power cost)**

| Variable | Default | Description |
|---|---|---|
| `CAM_SHARPNESS` | `1.5` | 0–16 (1.0 = neutral). |
| `CAM_CONTRAST` | unset | 0–32 (1.0 = neutral). |
| `CAM_SATURATION` | unset | 0–32. If set, becomes the daylight anchor for the lux-adaptive curve; leave unset to use `CAM_SAT_BRIGHT`/`CAM_SAT_DIM`. |
| `CAM_BRIGHTNESS` | unset | −1.0 to 1.0. |
| `CAM_EV` | unset | Exposure bias, −8.0 to 8.0 (day mode). |
| `CAM_AWB_MODE` | unset (auto) | `auto`/`incandescent`/`tungsten`/`fluorescent`/`indoor`/`daylight`/`cloudy`/`custom`. |
| `CAM_NOISE_REDUCTION` | `fast` | `off`/`fast`/`high_quality`/`minimal`/`zsl`. |

**Automatic day/night (NoIR)**

| Variable | Default | Description |
|---|---|---|
| `DAYNIGHT_AUTO` | `1` | Enable auto colour(day)/grayscale(night) off the Lux meter. |
| `DAYNIGHT_NIGHT_LUX` | `5` | Below this (sustained) → night. |
| `DAYNIGHT_DAY_LUX` | `12` | Above this (sustained) → day. Must be > night; low so any real light shows colour. |
| `DAYNIGHT_NIGHT_AFTER` | `45` | Seconds below `NIGHT_LUX` before going grayscale (lazy). |
| `DAYNIGHT_DAY_AFTER` | `8` | Seconds above `DAY_LUX` before going colour (eager — never stuck grey in a lit room). |
| `DAYNIGHT_CHECK_INTERVAL` | `10` | Seconds between Lux checks. |
| `DAYNIGHT_MODE` | `auto` | Startup / manual override: `auto`/`day`/`night`. |

**Light-adaptive tuning**

| Variable | Default | Description |
|---|---|---|
| `CAM_SAT_BRIGHT` | `1.0` | Saturation in real daylight (≥ `CAM_BRIGHT_LUX`). |
| `CAM_SAT_DIM` | `0.7` | Saturation at the day/night edge — damped so artificial-light IR false colour isn't amplified. |
| `CAM_BRIGHT_LUX` | `400` | Lux treated as real daylight (top of the saturation ramp). |
| `NIGHT_MAX_FPS` | `6` | Night fps cap: lower = longer exposure = real sensitivity, less power. |
| `CAM_NIGHT_EV_MAX` | `1.0` | EV brightness lift at total darkness, scaled to 0 at `NIGHT_LUX` (a fixed lift blows out a lit room). |

**Digital zoom**

| Variable | Default | Description |
|---|---|---|
| `CAM_ZOOM` | `1.0` | Startup zoom (1.0 = full field of view). |
| `CAM_ZOOM_MAX` | `4.0` | Max zoom the `/camera/zoom` endpoint allows. |

Tests: `uv run --no-project --with flask --with python-dotenv python -m unittest discover -s tests` (runs off‑Pi with stubbed camera libs). Add `--with playwright` to also run the phone/desktop layout checks in `tests/test_mobile_ui.py` (Chromium + WebKit; they skip when Playwright or its browsers are missing, unless `DOGCAM_REQUIRE_BROWSER=1` makes a missing Playwright, Chromium or WebKit a failure).

## Raspberry Pi setup

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y libgpiod2 libcamera-apps-lite python3-picamera2 python3-dev
# DHT22 only: sudo apt install -y libgpiod-dev
git clone <your-repo-url> dogo-cam && cd dogo-cam
curl -LsSf https://astral.sh/uv/install.sh | sh      # ensure ~/.local/bin is on PATH
uv sync
uv run gunicorn --worker-class gthread --workers 1 --threads 6 --bind 0.0.0.0:5000 dogcam_stream:app
```

Then open `http://<pi-ip>:5000`. This quick start is for standalone mode only (local password login, all `TRUST_PROXY_*=0`). In reverse-proxy mode bind loopback plus the address the proxy uses, as in `service_startup/dog-stream-flask.service`.

## systemd

Example units are in `service_startup/`. Copy `dog-stream-flask.service` → `/etc/systemd/system/dog-stream.service`, adjust `User` / `WorkingDirectory` / `EnvironmentFile` / `ExecStart`, then:

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now dog-stream
```

The unit binds `127.0.0.1:5000` (watchdog, button) and `192.168.100.10:5000` (the Nebula overlay the proxy uses). Until nebula has brought `tun0` up, binding the overlay address fails and gunicorn exits; `Restart=always` with `StartLimitIntervalSec=0` makes systemd retry every 5 s, and a listener that is already bound survives a nebula restart. Change the overlay address if your Pi's Nebula IP differs.

Install `button-control.service` in all configurations: it uses `SWITCH_MODE=auto` to detect a connected switch and otherwise keeps the camera online. Use `SWITCH_MODE=always-on` for a deterministic switchless installation. Also install `dogcam-watchdog.{service,timer}` for stream self-healing, and optionally `cloudflared-tunnel.service` for standalone tunnel mode (after creating the tunnel + `~/.cloudflared/config.yml` and setting `ENABLE_CLOUDFLARED=1`).

## Deploying updates

**Manual:**

```bash
cd ~/dogo-cam && git pull && uv sync && sudo systemctl restart dog-stream
```

**Push to deploy (CI):** `.github/workflows/deploy.yml` runs on a **self‑hosted runner** (on an always‑on box you trust, e.g. the mini). On every push to `main` (or manual `workflow_dispatch`) it SSHes to the Pi and runs a fixed deploy script — the runner's *outbound* connection means the Pi can stay behind NAT with no inbound webhook. Because this is a **public repo**, it's locked down (see below): the runner uses a command‑locked SSH key that can only run `deploy/dogcam-deploy.sh`. Install the artifacts:

```bash
sudo install -m0755 deploy/dogcam-deploy.sh /usr/local/bin/dogcam-deploy.sh
sudo install -m0440 deploy/dogcam-deploy.sudoers /etc/sudoers.d/dogcam-deploy
# then add the forced-command line (see dogcam-deploy.sh header) for the deploy key to authorized_keys
```

Existing installations must reinstall both files after updating the repository; the checked-out copies do not replace `/usr/local/bin/dogcam-deploy.sh` or `/etc/sudoers.d/dogcam-deploy` automatically.

## Power & stability

A Pi 3B funnels all current through its micro‑USB / polyfuse (~2–2.5A), so camera + MJPEG encoding + servos can brown out the 5V rail (under‑voltage) even with a strong supply — the camera stalls or the app hangs.

- **`STREAM_MAX_FPS`** (default 15) caps the framerate to cut peak draw — the biggest lever (`vcgencmd get_throttled` non‑zero = dips).
- Camera acquisition and JPEG encoding are demand-driven: startup configures the camera but leaves it idle; the first viewer (or `/snapshot`) starts recording, and `STREAM_IDLE_TIMEOUT` (default 10s) stops it after the final viewer disconnects while keeping the camera configured for a quick restart. The page closes its MJPEG connection only on `pagehide` (unload); it deliberately ignores Page Visibility, because embedded/background browsers can report "hidden" while still on screen, which froze the feed.
- Single‑shot autofocus at startup (imx708) avoids continuous AF‑motor draw and PDAF log spam.
- Real fix: power the servos from a **separate 5V** (common ground), or use a Pi 4/5.
- **Self‑healing (three layers):**
  1. `/video_feed` gives up after `STREAM_FRAME_TIMEOUT` (5s) without a frame. Gunicorn also reserves request capacity beyond `MAX_VIEWERS`, so healthy long-lived MJPEG streams cannot starve servo controls or health checks. The page reconnects the `<img>` automatically and shows *🟡 Stream Stalled* meanwhile.
  2. If no frames arrive for `STREAM_STALL_RESTART_AFTER` (20s) the app tears down and re‑creates the Picamera2 pipeline in‑process — no service restart, the UI stays up.
  3. `dogcam-watchdog.timer` restarts the *service* only if the app is dead, or if `/stream_health` stays 503 past `STALL_ESCALATE_SECONDS` (120s), and never while the remote-power file says `OFF` (see [House modes](#house-modes-remote-power-and-lux-over-mqtt)). `dog-stream.service` uses `TimeoutStopSec=15` + `KillMode=mixed` so a stuck camera cleanup can't wedge it in `deactivating`.
- `GET /stream_health` → `{"state", "viewers", "healthy", "frames", "last_frame_age_s", ...}` for dashboards and external monitors. Intentional `idle`, `cooldown`, and `disabled` states return 200; demanded stalls/unavailability return 503.
- Blank feed but `camera_status` says available → reseat the **CSI ribbon** (a loose cable gives "Camera frontend timed out" / zero frames while the sensor still enumerates on I²C). Check `journalctl -u dog-stream | grep -i "Restarting camera"` to see how often the pipeline is stalling.
- Tests: `python3 -m unittest tests.test_stream_stall` runs the app off‑Pi with stubbed camera libs and reproduces the thread‑exhaustion bug.

## Security hardening

Important for a public repo with a self‑hosted runner:

- SSH key‑only (`PasswordAuthentication no`, `PermitRootLogin no`); minimal `authorized_keys` (admin + command‑locked deploy key).
- Never listen on the LAN in reverse-proxy mode: bind `:5000` to loopback + the overlay address (see systemd), set `TRUSTED_PROXY_ADDRS` to the proxy's address only, and let the Nebula firewall admit `:5000` only from the proxy host.
- Disable unused services (e.g. Samba on `139/445`) — keep the surface to `:22` + `:5000`.
- Scoped deploy sudo (`deploy/dogcam-deploy.sudoers`, restart only); avoid `NOPASSWD: ALL` on the service user.

## Notes

- `.env` stays only on the Pi / your local machine.
- Servo positions persist in `/tmp/servo_positions.json`; tune with `SERVO_STEP_SIZE` / `SERVO_SETTLE_SECONDS`.
