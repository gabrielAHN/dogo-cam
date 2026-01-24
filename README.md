# Dogo Cam Project 🐶

This project sets up a Raspberry Pi 3 to stream live video from a camera module (e.g., for monitoring a pet like a dog) and display environmental temperature/humidity data from a sensor.

![raspberry-pi-image](https://github.com/gabrielAHN/dogo-cam-project/blob/main/img/raspberry-pi-cam.png?raw=true)

The stream is accessible via a web interface with basic authentication, limited to a maximum of 3 concurrent viewers. It uses Flask for the web app, Picamera2 for video, and Adafruit libraries for the sensor.

![Dog View](https://github.com/gabrielAHN/dogo-cam-project/blob/main/img/dog-stream.png?raw=true)

Dependencies are managed with UV via a `pyproject.toml` file. The app runs on boot using systemd, and optionally, you can expose it securely via Cloudflare Tunnel (also boot-enabled).

## Prerequisites
- [Raspberry Pi 3](https://www.amazon.com/Raspberry-Pi-Model-Board-Plus/dp/B0BNJPL4MW?sr=8-1) with [Raspberry Pi OS (64-bit recommended)](https://www.raspberrypi.com/software/operating-systems/).
- [Picamera Module V3 (or compatible) connected and enabled](https://www.amazon.com/Arducam-Raspberry-Camera-Autofocus-15-22pin/dp/B0C9PYCV9S?sr=8-1).
- [OSOYOO DHT22 (or standard DHT22) temperature/humidity sensor](https://www.amazon.com/Gowoops-Temperature-Humidity-Measurement-Raspberry/dp/B073F472JL?sr=8-1).
- (OPTIONAL) A domain with DNS hosted (e.g., via AWS Route 53) for remote access—use a placeholder like `your-domain.com`.
- Basic tools: Git, Python 3.12+.

## Hardware Setup

### 1. Connect the Camera Module
- Attach the Picamera V3 to the Raspberry Pi's CSI port.
- Enable the camera: Run `sudo raspi-config`, navigate to Interface Options > Camera > Enable, then reboot.
- Test: Run `libcamera-hello` in terminal—if you see a preview, it's working.

### 2. Connect the DHT22 Sensor
- Wiring (using GPIO pins; power off Pi before connecting):

| Sensor Pin | Raspberry Pi Physical Pin | Function | Notes |
|------------|---------------------------|----------|-------|
| VCC | Pin 1 | 3.3V Power | Supplies power to the sensor. Although the manufacturer's tutorial suggests connecting to 5V (Pi Pin 2 or 4), using 3.3V is safer and recommended to prevent potential voltage mismatch that could damage the Pi's GPIO pins. The sensor operates fine at 3.3V. |
| Data | Pin 7 | GPIO4 | The data signal pin. GPIO4 is commonly used in tutorials, but you can choose any available GPIO pin and adjust your code accordingly. |
| GND | Pin 6 (or Pin 9) | Ground | Completes the circuit. Pin 6 is adjacent for easier wiring without crossing wires much. |

- Add a 4.7kΩ-10kΩ pull-up resistor between VCC and Data pins for signal stability.
- Test: After software setup, run a simple script to verify readings (see Software Installation).

### 3. Connect the KY-004 Button Module (Optional - Stream On/Off Control)
- **OPTIONAL**: Only connect this if you want physical button control to save power
- **Product Used**: [Youliang KY-004 3-Pin Button Key Tactile Switch Sensor](https://www.amazon.co.jp/dp/B07TBKTGR3)
- **Reference**: [KY-004 Key Switch Module Documentation](https://arduinomodules.info/ky-004-key-switch-module/)
- Wiring (power off Pi before connecting):

| KY-004 Pin | Raspberry Pi Physical Pin | Function | Notes |
|------------|---------------------------|----------|-------|
| S (Signal) | Pin 11 | GPIO17 | Signal pin that goes HIGH when button is pressed |
| Middle (VCC) | Pin 17 | 3.3V Power | Powers the button module |
| - (GND) | Pin 14 | Ground | Completes the circuit |

- **How It Works**:
  - Set `USE_BUTTON=true` in `.env` to enable button control
  - When enabled, stream starts OFF (saves power) - press button to turn ON
  - Press button again to toggle back to OFF
  - When disabled (`USE_BUTTON=false`), stream is always ON (no button needed)
- **Specifications**:
  - Operating voltage: 3.3V-5V
  - Tactile button rated for 100,000 cycles
  - Built-in 10kΩ resistor
- Test: After software setup, pressing the button should toggle the stream on/off.

## Software Installation

### 1. Raspberry Pi APT Installs
- Update system and install required packages for GPIO, camera, and monitoring:
  ```
  sudo apt update && sudo apt upgrade -y
  sudo apt install libgpiod2 libcamera-apps-lite python3-picamera2 htop -y
  ```

### 2. Install UV (Package Manager)
- UV is used for faster dependency management. Install globally:
  ```
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- If installed via pipx, path might be `~/.local/bin/uv`—add to PATH if needed: `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc`.

### 3. Set Up the Project
- Clone the project repository:
  ```
  git clone https://github.com/gabrielAHN/dogo-cam-project.git
  cd dogo-cam-project
  ```
- Create `.env` for secrets (do not commit to Git):
  ```
  BASIC_AUTH_USERNAME=your_username
  BASIC_AUTH_PASSWORD=your_password
  MAX_VIEWERS=3
  PORT=5000
  USE_BUTTON=true  # Set to 'true' to enable button control (stream starts OFF), 'false' for always-on stream
  ```

### 4. Manage Dependencies with `pyproject.toml`
- Create `pyproject.toml` in the project root:
  ```
  [project]
  name = "dog-cam-stream"
  version = "0.1.0"
  requires-python = ">=3.12"

  [tool.uv]
  dependencies = [
      "flask",
      "flask-httpauth",
      "picamera2",
      "python-dotenv",
      "adafruit-circuitpython-dht",
      "adafruit-blinka",
      "gunicorn",  # For production server
      "RPi.GPIO",  # For button control
  ]
  ```
- Sync dependencies (creates/manages venv):
  ```
  uv sync
  ```

### 5. Application Code
- Main file: `dogcam_stream.py` (Flask app with streaming, auth, sensor reads—code as per your setup).
- Template: `templates/index.html` (HTMX for dynamic temp/humidity display).
- Test locally: `uv run gunicorn --worker-class gthread --workers 1 --threads 4 --bind 0.0.0.0:5000 dogcam_stream:app`
  - Access `http://your-pi-ip:5000` (e.g., `http://192.168.1.100:5000`), enter auth creds.

## Setting Up on Boot (Systemd Service)

### 1. Create the Service File
- Edit `/etc/systemd/system/dog-stream.service` (use a generic name; sudo required):
  ```
  [Unit]
  Description=Dog Stream Flask App
  After=network.target

  [Service]
  User=your-user
  WorkingDirectory=/home/your-user/dogo-cam-project
  EnvironmentFile=/home/your-user/dogo-cam-project/.env
  ExecStart=/home/your-user/.local/bin/uv run gunicorn --worker-class gthread --workers 1 --threads 4 --bind 0.0.0.0:5000 dogcam_stream:app
  Restart=always
  RestartSec=10
  LimitNOFILE=4096
  OOMScoreAdjust=-1000

  [Install]
  WantedBy=multi-user.target
  ```
- Reload and enable:
  ```
  sudo systemctl daemon-reload
  sudo systemctl enable --now dog-stream.service
  ```
- Check: `sudo systemctl status dog-stream.service`—should be active.

## Optional: Cloudflare Tunnel Setup for Remote Access
Expose the local app securely to your domain (e.g., `your-domain.com`) without port forwarding. Cloudflare handles HTTPS and DNS (delegate nameservers from your hosted zone provider like AWS Route 53).

### 1. Install Cloudflared
- Download for ARM: `wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm -O cloudflared`
- Make executable and move: `chmod +x cloudflared && sudo mv cloudflared /usr/local/bin/`

### 2. Authenticate and Create Tunnel
- Log in: `cloudflared tunnel login`
- Create: `cloudflared tunnel create dog-stream-tunnel`
- Config file (`~/.cloudflared/config.yml`):
  ```
  tunnel: dog-stream-tunnel
  credentials-file: /home/your-user/.cloudflared/<tunnel-uuid>.json
  ingress:
    - hostname: your-domain.com
      service: http://localhost:5000
    - service: http_status:404
  ```
- Run manually: `cloudflared tunnel run dog-stream-tunnel`

### 3. DNS Setup
- In Cloudflare Dashboard: Add site > Enter `your-domain.com` > Update nameservers in your hosted zone (e.g., AWS Route 53) to Cloudflare's.
- Add CNAME: Name `@` (root), Target `<tunnel-uuid>.cfargotunnel.com`, Proxied (orange cloud).

### 4. Boot Setup for Tunnel (Systemd Service)
- Create `/etc/systemd/system/cloudflared.service`:
  ```
  [Unit]
  Description=Cloudflare Tunnel for Dog Stream
  After=network.target

  [Service]
  User=your-user
  ExecStart=/usr/local/bin/cloudflared tunnel --config /home/your-user/.cloudflared/config.yml run dog-stream-tunnel
  Restart=always
  RestartSec=10

  [Install]
  WantedBy=multi-user.target
  ```
- Enable: `sudo systemctl enable --now cloudflared.service`
- Check: `sudo systemctl status cloudflared.service`

## Using the Button to Control the Stream

The KY-004 button provides physical on/off control for the camera stream:

1. **Toggle Stream**: Press the button to toggle between streaming ON and OFF states
2. **Visual Feedback**: The web interface shows the current stream status:
   - 🟢 Stream Active - Camera is streaming
   - 🔴 Stream Paused - Camera is paused (button pressed to disable)
3. **Status Updates**: The status indicator updates every 2 seconds via HTMX
4. **Default State**: Stream starts enabled when the service boots

### Testing the Button

After completing the hardware and software setup:

1. Start the service: `sudo systemctl start dog-stream.service`
2. Access the web interface: `http://your-pi-ip:5000`
3. Observe the stream status indicator in the top-left corner
4. Press the physical button - the status should change and the stream should pause/resume
5. Check logs for confirmation: `journalctl -u dog-stream.service -f`
   - You should see messages like "Stream enabled" or "Stream disabled"

## Troubleshooting
- Camera not starting: Ensure enabled in `raspi-config`; test with `libcamera-hello`.
- Sensor errors: Verify pull-up resistor; retry logic in code handles checksum issues.
- Access issues: Check auth creds in `.env`; monitor logs with `journalctl -u dog-stream.service`.
- Performance: On RPi 3, lower video resolution if CPU/RAM high (edit code: `{"size": (320, 240)}`).
- Button not working: Check GPIO connections; verify RPi.GPIO is installed; check logs for GPIO errors.