#!/bin/bash
# Restart the dog-stream service if the Flask/gunicorn app stops responding.
#
# Two failure modes, two responses:
#
# 1. App unresponsive (connection refused/timeout on every request). The
#    gunicorn worker (1 worker / 4 threads) can wedge so the socket accepts
#    but nothing is served. gunicorn's own --timeout does not catch this, so
#    restart the whole service after 3 consecutive failures.
#
# 2. App alive but camera stalled (/stream_health -> 503, "healthy": false).
#    The app's in-process stall monitor (STREAM_STALL_RESTART_AFTER, default
#    20s) restarts the camera pipeline itself, which is far cheaper than a
#    service restart and keeps the UI up. Only escalate to a service restart
#    if the stall persists well past that window (STALL_ESCALATE_SECONDS).
set -u

PORT="${PORT:-5000}"
BASE="http://127.0.0.1:${PORT}"
STALL_ESCALATE_SECONDS="${STALL_ESCALATE_SECONDS:-120}"
STALL_STAMP="/tmp/dogcam-stall-since"

http_code() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$1" 2>/dev/null
}

# --- 1. liveness -----------------------------------------------------------
alive=0
for _ in 1 2 3; do
  code="$(http_code "$BASE/")"
  if [ -n "$code" ] && [ "$code" != "000" ]; then
    alive=1
    break
  fi
  sleep 3
done

if [ "$alive" -eq 0 ]; then
  logger -t dogcam-watchdog "dog-stream unresponsive (code=${code:-timeout}); restarting"
  rm -f "$STALL_STAMP"
  systemctl restart dog-stream.service
  exit 0
fi

# --- 2. stream health -------------------------------------------------------
# Remote-User lets the check pass login_required only when the app is
# configured to trust proxy auth headers; otherwise it redirects (302) and we
# treat "alive" as good enough, matching the old behaviour.
health="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -H 'Remote-User: watchdog' "$BASE/stream_health" 2>/dev/null)"

case "$health" in
  200)
    rm -f "$STALL_STAMP"
    ;;
  503)
    now="$(date +%s)"
    if [ ! -f "$STALL_STAMP" ]; then
      echo "$now" > "$STALL_STAMP"
      logger -t dogcam-watchdog "stream stalled; in-process recovery has ${STALL_ESCALATE_SECONDS}s before service restart"
      exit 0
    fi
    since="$(cat "$STALL_STAMP" 2>/dev/null || echo "$now")"
    if [ $((now - since)) -ge "$STALL_ESCALATE_SECONDS" ]; then
      logger -t dogcam-watchdog "stream stalled for $((now - since))s despite in-process recovery; restarting service"
      rm -f "$STALL_STAMP"
      systemctl restart dog-stream.service
    fi
    ;;
  *)
    # 302 (auth) or anything else: app is up; nothing more we can tell.
    rm -f "$STALL_STAMP"
    ;;
esac
exit 0
