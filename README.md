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
- optional DHT22 sensor on `GPIO4`
- optional KY-004 button

See `PIN_DIAGRAM.md` for wiring.

## Manual Controls

- Desktop:
  - click and hold the arrow buttons
  - use `↑ ↓ ← →`
  - use `W A S D`
- Mobile:
  - drag up and down on the screen for tilt
  - drag left and right on the screen for pan

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

### Optional button service

If you use the KY-004 button:

```bash
sudo cp service_startup/button-control.service /etc/systemd/system/button-control.service
sudo systemctl daemon-reload
sudo systemctl enable --now button-control.service
```

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
