#!/usr/bin/env python3
import logging
import os
import subprocess
import threading
import time
import urllib.request

import lgpio

SWITCH_PIN = 17
SWITCH_ON_VALUE = int(os.getenv("SWITCH_ON_VALUE", "0"))
STREAM_SVC = "dog-stream.service"
CF_SVC = "cloudflared-tunnel.service"
URL = "http://localhost:5000/login"
LED_PATH = "/sys/class/leds/PWR"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def led_write(f, v):
    os.system(f'echo {v} | sudo tee {LED_PATH}/{f} > /dev/null 2>&1')


def led_on():
    led_write("trigger", "none")
    led_write("brightness", 1)


def led_off():
    led_write("trigger", "none")
    led_write("brightness", 0)


_flicker_stop = threading.Event()
_flicker_thread = None


def led_flicker():
    global _flicker_thread
    _flicker_stop.clear()

    def _loop():
        led_write("trigger", "none")
        s = 1
        while not _flicker_stop.is_set():
            led_write("brightness", s)
            s ^= 1
            time.sleep(0.3)

    _flicker_thread = threading.Thread(target=_loop, daemon=True)
    _flicker_thread.start()


def led_stop_flicker():
    _flicker_stop.set()
    if _flicker_thread:
        _flicker_thread.join(timeout=1)


def site_up():
    try:
        urllib.request.urlopen(URL, timeout=1)
        return True
    except Exception:
        return False


def run(cmd, timeout=15):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning(f"Timed out running: {' '.join(cmd)}")
        return False
    except Exception as e:
        log.warning(f"Failed running {' '.join(cmd)}: {e}")
        return False

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        log.warning(f"Command failed ({result.returncode}): {' '.join(cmd)} {detail}")
    return result.returncode == 0


def service_state(name):
    try:
        result = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def wait_inactive(name, seconds):
    for _ in range(seconds * 2):
        if service_state(name) in {"inactive", "failed", "unknown"}:
            return True
        time.sleep(0.5)
    return False


def stop_service(name):
    run(["sudo", "systemctl", "--no-block", "stop", name], timeout=5)
    if wait_inactive(name, 12):
        return

    log.warning(f"{name} did not stop cleanly, killing it")
    run(["sudo", "systemctl", "kill", "--signal=SIGKILL", name], timeout=5)
    run(["sudo", "systemctl", "--no-block", "stop", name], timeout=5)
    wait_inactive(name, 5)


def switch_label(state):
    return "ON" if state == SWITCH_ON_VALUE else "OFF"


def start():
    log.info("ON: starting Flask...")
    led_flicker()
    run(["sudo", "systemctl", "start", STREAM_SVC])

    for i in range(60):
        if site_up():
            log.info(f"Flask ready ({i}s), starting Cloudflare...")
            run(["sudo", "systemctl", "start", CF_SVC])
            led_stop_flicker()
            led_on()
            log.info("Website live")
            return
        time.sleep(1)
    log.error("Flask did not respond in 60s")


SHUTDOWN_FILE = "/tmp/shutdown_pending"


def stop():
    log.info("OFF: signalling camera to stop...")
    led_flicker()

    try:
        with open(SHUTDOWN_FILE, "w") as f:
            f.write("1")
    except Exception as e:
        log.error(f"Could not write shutdown file: {e}")

    for _ in range(10):
        time.sleep(0.5)
        try:
            with open(SHUTDOWN_FILE) as f:
                if f.read().strip() == "1":
                    continue
        except Exception:
            break
    time.sleep(1)

    log.info("Camera stopped, stopping services...")
    run(["sudo", "systemctl", "--no-block", "stop", CF_SVC], timeout=5)
    run(["sudo", "systemctl", "--no-block", "stop", STREAM_SVC], timeout=5)
    stop_service(CF_SVC)
    stop_service(STREAM_SVC)

    try:
        os.remove(SHUTDOWN_FILE)
    except Exception:
        pass

    led_stop_flicker()
    led_off()
    log.info("Services stopped")


def main():
    h = lgpio.gpiochip_open(0)
    lgpio.gpio_claim_input(h, SWITCH_PIN, lgpio.SET_PULL_UP)

    def read():
        vals = []
        for _ in range(3):
            vals.append(lgpio.gpio_read(h, SWITCH_PIN))
            time.sleep(0.017)
        return 0 if all(v == 0 for v in vals) else 1 if all(v == 1 for v in vals) else None

    state = None
    while state is None:
        state = read()

    log.info(f"Switch is {switch_label(state)} on startup (GPIO{SWITCH_PIN}={state})")

    if state == SWITCH_ON_VALUE:
        start() if not site_up() else (led_on() or log.info("Site already up"))
    else:
        stop()

    log.info("Monitoring GPIO17 for switch changes...")

    try:
        while True:
            new = read()
            if new is not None and new != state:
                state = new
                log.info(f"Switch changed to {switch_label(state)} (GPIO{SWITCH_PIN}={state})")
                if state == SWITCH_ON_VALUE:
                    start()
                else:
                    stop()
            time.sleep(0.1)
    except KeyboardInterrupt:
        log.info("Stopped")
    finally:
        lgpio.gpiochip_close(h)


if __name__ == "__main__":
    main()
