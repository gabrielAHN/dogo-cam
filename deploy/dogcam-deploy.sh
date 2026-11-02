#!/usr/bin/env bash
# Command-locked deploy action for the dogo cam.
#
# Install on the Pi as /usr/local/bin/dogcam-deploy.sh (chmod 0755) and pin it
# as the forced command for a DEDICATED deploy SSH key in the deploy user's
# ~/.ssh/authorized_keys, e.g.:
#
#   command="/usr/local/bin/dogcam-deploy.sh",no-agent-forwarding,\
#   no-port-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA... deploy-key
#
# The SSH forced command ignores whatever the client sends, so this key can do
# nothing on the Pi but run exactly this sequence. Pair it with the scoped
# sudoers rule in deploy/dogcam-deploy.sudoers so it never needs broad sudo.
set -euo pipefail
cd "${DOGCAM_DIR:-$HOME/dogo-cam}"
git fetch --all --quiet
git reset --hard origin/main
# The services run this checkout's virtualenv (.venv/bin/python, .venv/bin/gunicorn),
# which has include-system-site-packages = true and uses the apt camera stack
# (python3-picamera2 and its numpy, av, pillow, ...). Install ONLY paho-mqtt here:
# installing the full pinned dependency list would put pip builds of picamera2,
# av, numpy etc. into the venv and shadow the apt packages. A failed install stops
# here, so nothing restarts onto a missing paho-mqtt.
.venv/bin/python -m pip install --quiet 'paho-mqtt==2.1.0'
sudo systemctl restart dog-stream
sudo systemctl try-restart button-control
echo "deployed $(git rev-parse --short HEAD)"
