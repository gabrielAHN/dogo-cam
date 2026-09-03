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
sudo systemctl restart dog-stream
echo "deployed $(git rev-parse --short HEAD)"
