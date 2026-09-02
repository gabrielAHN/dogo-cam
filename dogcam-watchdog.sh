#!/bin/bash
# Restart the dog-stream service if the Flask/gunicorn app stops responding.
#
# The gunicorn worker (1 worker / 4 threads) can deadlock when the camera
# pipeline stalls: the listening socket still accepts connections but no
# thread is free to serve them, so the app hangs and the stream freezes.
# Gunicorn's own --timeout does not catch this (the arbiter keeps
# heartbeating), so an external health check restarts the service.
set -u

PORT="${PORT:-5000}"
URL="http://127.0.0.1:${PORT}/"

# Any HTTP reply (200/302/401/...) means the app is alive. Only a connection
# timeout / refusal ("000") counts as unhealthy. Require 3 consecutive
# failures before restarting to avoid acting on a transient blip.
for _ in 1 2 3; do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$URL" 2>/dev/null)"
  if [ -n "$code" ] && [ "$code" != "000" ]; then
    exit 0
  fi
  sleep 3
done

logger -t dogcam-watchdog "dog-stream unresponsive (code=${code:-timeout}); restarting"
systemctl restart dog-stream.service
