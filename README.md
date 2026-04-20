# Dogo Cam

Manual Raspberry Pi dog camera with:

- Flask web UI
- live Picamera2 stream
- pan and tilt servo controls
- keyboard and on-screen arrow controls
- touch drag controls on mobile
- optional DHT22 temperature and humidity readout
- optional Cloudflare Tunnel exposure

Tracking has been removed. The current app is manual-only.

## Project Layout

- `dogcam_stream.py`: Flask app and camera endpoints
- `servo_control_rpigpio.py`: MG90S pan and tilt servo control
- `templates/index.html`: main camera UI
- `templates/login.html`: login screen
- `ky004-control.py`: optional GPIO button power control
- `service_startup/`: example systemd unit files
- `PIN_DIAGRAM.md`: wiring reference

## Hardware

- Raspberry Pi with Raspberry Pi OS
- CSI camera module
- 2 MG90S servos for pan and tilt
- GPIO toggle switch for turning the site stack on and off
- optional DHT22 sensor on `GPIO4`
- optional Cloudflare Tunnel

See `PIN_DIAGRAM.md` for wiring.

### Raspberry Pi Wiring

| Part | Signal Pin | Raspberry Pi Pin | Notes |
|------|------------|------------------|-------|
| CSI camera | Ribbon cable | CSI camera connector | Uses the dedicated camera port, not GPIO |
| Tilt servo | Signal | `GPIO18` / physical `Pin 12` | Servo 1 in the app |
| Tilt servo | V+ | physical `Pin 2` (`5V`) | Servo 1 power from Raspberry Pi |
| Tilt servo | GND | physical `Pin 14` | Shared ground for tilt servo |
| Pan servo | Signal | `GPIO19` / physical `Pin 35` | Servo 2 in the app |
| Pan servo | V+ | physical `Pin 4` (`5V`) | Servo 2 power from Raspberry Pi |
| Pan servo | GND | physical `Pin 39` | Shared ground for pan servo |
| Toggle switch | Signal | `GPIO17` / physical `Pin 11` | 3-pin switch module signal |
| Toggle switch | VCC | physical `Pin 17` (`3.3V`) | Use `3.3V` for GPIO-safe switching |
| Toggle switch | GND | physical `Pin 25` | Module ground |
| DHT22 VCC | Power | physical `Pin 1` (`3.3V`) | Optional sensor power |
| DHT22 data | Data | `GPIO4` / physical `Pin 7` | Optional sensor |
| DHT22 GND | Ground | physical `Pin 9` | Optional sensor ground |
| Cooling fan | Power | physical `Pin 4` (`5V`) split with pan servo power | Always-on Pi cooling fan |
| Cooling fan | Ground | physical `Pin 6` | Ground from Raspberry Pi |

### Servo Power Notes

- Both servos are wired directly to the Raspberry Pi `5V` header pins in this wiring map.
- The cooling fan is also wired to the Raspberry Pi by splitting `Pin 4` (`5V`) with the pan servo power lead.
- Share ground between all servos, the fan, and the Raspberry Pi.
- The app maps `servo1` to tilt on `GPIO18` and `servo2` to pan on `GPIO19`.
- The camera mount is inverted, so the stream is flipped in software.
- If you add a two-wire fan, wire it to `Pin 4` and `Pin 6`, or to `Pin 1` and ground if it is a `3.3V` fan.

### Switch Behavior

The deployed Raspberry Pi uses a simple on/off switch on `GPIO17`, monitored by `ky004-control.py`.

- switch ON, `GPIO17` reads low:
  - start `dog-stream.service`
  - wait for the Flask app to respond
  - start `cloudflared-tunnel.service`
- switch OFF, `GPIO17` reads high:
  - signal the Flask app to stop the camera cleanly
  - stop the Cloudflare tunnel
  - stop the Flask app

If your switch module reports the opposite value, set `SWITCH_ON_VALUE=1` in the environment used by `button-control.service`.

## Manual Controls

- Desktop:
  - click and hold the arrow buttons
  - use `↑ ↓ ← →`
  - use `W A S D`
- Mobile:
  - tap or hold above/below center for tilt
  - tap or hold left/right of center for pan

## Environment

Create a local `.env` in the project root on the Raspberry Pi. Do not commit it.

```env
BASIC_AUTH_USERNAME=your_username
BASIC_AUTH_PASSWORD=your_password
SECRET_KEY=replace_me
MAX_VIEWERS=3
PORT=5000
DOG_NAME=Kotaro
```

## Raspberry Pi Setup

### 1. System packages

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y libgpiod2 libcamera-apps-lite python3-picamera2 python3-dev
```

If you use the DHT22:

```bash
sudo apt install -y libgpiod-dev
```

### 2. Clone the repo

```bash
git clone <your-repo-url> dogo-cam
cd dogo-cam
```

### 3. Install `uv`

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Restart the shell or add `~/.local/bin` to `PATH` if needed.

### 4. Install Python dependencies

```bash
uv sync
```

You can also use:

```bash
uv run python -m py_compile dogcam_stream.py servo_control_rpigpio.py
```

### 5. Run locally

```bash
uv run gunicorn --worker-class gthread --workers 1 --threads 4 --bind 0.0.0.0:5000 dogcam_stream:app
```

Open `http://<pi-ip>:5000`.

## systemd

Example units are in `service_startup/`.

### Flask app

Copy `service_startup/dog-stream-flask.service` to `/etc/systemd/system/dog-stream.service`, then adjust:

- `User`
- `WorkingDirectory`
- `EnvironmentFile`
- `ExecStart`

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dog-stream.service
sudo systemctl status dog-stream.service
```

### Switch control service

If you use the GPIO switch on `GPIO17`:

```bash
sudo cp service_startup/button-control.service /etc/systemd/system/button-control.service
sudo systemctl daemon-reload
sudo systemctl enable --now button-control.service
```

This service runs `ky004-control.py`, which monitors the switch and starts or stops the camera stack.

Update the unit path if your project directory is different.

### Optional Cloudflare Tunnel

Copy `service_startup/cloudflared-tunnel.service` after you have:

- installed `cloudflared`
- created a tunnel
- created `~/.cloudflared/config.yml`

Then:

```bash
sudo cp service_startup/cloudflared-tunnel.service /etc/systemd/system/cloudflared-tunnel.service
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared-tunnel.service
```

## Deploying Updates To The Pi

On the Pi:

```bash
cd ~/dogo-cam
git pull
uv sync
sudo systemctl restart dog-stream.service
```

## Notes

- `.env` should stay only on the Pi or your local machine.
- Servo positions are persisted in `/tmp/servo_positions.json`.
- The camera stream is flipped because the camera mount is inverted.
