#!/usr/bin/env python3
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import lgpio

# dogcam_mqtt sits next to this script; tests load the script by path.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import dogcam_mqtt  # noqa: E402

SWITCH_PIN = int(os.getenv("SWITCH_PIN", "17"))
SWITCH_ON_VALUE = int(os.getenv("SWITCH_ON_VALUE", "0"))
# The GPIO level apply_state() treats as OFF (any level other than SWITCH_ON_VALUE).
SWITCH_OFF_VALUE = 0 if SWITCH_ON_VALUE else 1
SWITCH_MODE = os.getenv("SWITCH_MODE", "auto").strip().lower()
SWITCH_LOG_INTERVAL = float(os.getenv("SWITCH_LOG_INTERVAL", "10"))
SWITCH_RECONCILE_INTERVAL = float(os.getenv("SWITCH_RECONCILE_INTERVAL", "5"))
STREAM_SVC = "dog-stream.service"
CF_SVC = "cloudflared-tunnel.service"
URL = "http://localhost:5000/login"
LED_PATH = "/sys/class/leds/PWR"
ENABLE_CLOUDFLARED = os.getenv("ENABLE_CLOUDFLARED", "1").lower() in {"1", "true", "yes", "on"}

# Remote power (house modes): jg-casa/dogo-cam/power/set ON|OFF, retained.
DEFAULT_REMOTE_POWER_FILE = "/var/lib/dogcam/remote-power"
REMOTE_POWER_FILE = os.getenv("DOGCAM_REMOTE_POWER_FILE", DEFAULT_REMOTE_POWER_FILE)
POWER_STATE_INTERVAL = 300  # seconds between unchanged power/state republishes
REMOTE_START_WAIT = 5.0  # seconds to wait at start for the broker's retained power/set

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


def reset_failed_services():
    run(["sudo", "systemctl", "reset-failed"], timeout=5)


def stable_gpio_value(values):
    if values and all(value == values[0] for value in values):
        return values[0]
    return None


def read_pull_value(handle, pin, pull, samples=5, settle=0.05):
    try:
        lgpio.gpio_free(handle, pin)
    except Exception:
        pass
    lgpio.gpio_claim_input(handle, pin, pull)
    time.sleep(settle)
    return stable_gpio_value([lgpio.gpio_read(handle, pin) for _ in range(samples)])


def resolve_switch_mode(mode, pull_down=None, pull_up=None):
    if mode not in {"auto", "gpio", "always-on"}:
        raise ValueError("SWITCH_MODE must be auto, gpio, or always-on")
    if mode != "auto":
        return mode
    if pull_down == 0 and pull_up == 1:
        return "always-on"
    return "gpio"


def detect_switch_mode(handle):
    if SWITCH_MODE != "auto":
        return resolve_switch_mode(SWITCH_MODE)
    pull_down = read_pull_value(handle, SWITCH_PIN, lgpio.SET_PULL_DOWN)
    pull_up = read_pull_value(handle, SWITCH_PIN, lgpio.SET_PULL_UP)
    mode = resolve_switch_mode(SWITCH_MODE, pull_down=pull_down, pull_up=pull_up)
    log.info(
        f"Switch auto-detection: pull-down={pull_down}, pull-up={pull_up}, mode={mode}"
    )
    return mode


def switch_label(state):
    return "ON" if state == SWITCH_ON_VALUE else "OFF"


# ---------------------------------------------------------------------------
# Remote power (house modes). Effective camera power = hardware switch ON AND
# remote not OFF; in always-on mode the switch counts as ON. The retained
# jg-casa/dogo-cam/power/set value arrives on paho's thread; the main loop
# applies it through apply_state() on its next 0.1 s tick, so every service
# start/stop still happens on one thread.
# ---------------------------------------------------------------------------
def effective_power(switch_on, remote):
    return bool(switch_on) and remote != "OFF"


def effective_state(switch_state, remote=None):
    """The GPIO-style level apply_state() should enforce for this switch level."""
    if remote is None:
        remote = remote_power.remote
    on = effective_power(switch_state == SWITCH_ON_VALUE, remote)
    return SWITCH_ON_VALUE if on else SWITCH_OFF_VALUE


def load_remote_power(path):
    """Last persisted remote value; ON when there is no (valid) file."""
    try:
        with open(path) as state_file:
            text = state_file.read()
    except FileNotFoundError:
        return dogcam_mqtt.POWER_DEFAULT
    except OSError as e:
        log.warning(f"Could not read remote power file {path}: {e}; using ON")
        return dogcam_mqtt.POWER_DEFAULT
    value = text.strip().upper()
    if value in {"ON", "OFF"}:
        return value
    log.warning(f"Ignoring remote power file {path} with content {text.strip()!r}; using ON")
    return dogcam_mqtt.POWER_DEFAULT


def save_remote_power(path, value):
    """Atomically replace the one-word remote power file."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".remote-power-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w") as state_file:
            state_file.write(f"{value}\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.remove(temporary)
            except OSError:
                pass


class RemotePower:
    """Remote ON/OFF from MQTT, its persisted copy, and the power/state topic."""

    def __init__(self, path):
        self.path = path
        self.remote = dogcam_mqtt.POWER_DEFAULT
        self.switch_on = None  # unknown until the main loop reads the switch
        self.link = None
        self._persisted = None
        self._lock = threading.Lock()
        self._changed = threading.Event()  # main loop must re-apply
        self._received = threading.Event()  # the broker delivered a power/set
        self._publish = threading.Event()  # publisher: publish state now
        self._stopped = threading.Event()
        self._publisher = None

    def load(self):
        with self._lock:
            self.remote = load_remote_power(self.path)
            self._persisted = self.remote if os.path.exists(self.path) else None
        return self.remote

    def handle_set(self, payload):
        """paho thread: a jg-casa/dogo-cam/power/set message."""
        value = dogcam_mqtt.parse_on_off(
            payload, dogcam_mqtt.POWER_DEFAULT, dogcam_mqtt.POWER_SET_TOPIC
        )
        if value is None:
            return
        with self._lock:
            changed = value != self.remote
            self.remote = value
            if value != self._persisted:
                try:
                    save_remote_power(self.path, value)
                    self._persisted = value
                except OSError as e:
                    log.error(f"Could not persist remote power {value} to {self.path}: {e}")
        self._received.set()
        if changed:
            log.info(f"Remote power set to {value}")
            self._changed.set()
            self._publish.set()

    def take_change(self):
        """True once per remote change; the caller then applies the current value."""
        if self._changed.is_set():
            self._changed.clear()
            return True
        return False

    def wait_for_remote(self, timeout):
        return self._received.wait(timeout)

    def set_switch(self, on):
        on = bool(on)
        if on != self.switch_on:
            self.switch_on = on
            self._publish.set()

    def camera_on(self):
        return effective_power(self.switch_on, self.remote)

    def state_payload(self):
        return {
            "remote": self.remote,
            "switch": "ON" if self.switch_on else "OFF",
            "camera": "ON" if self.camera_on() else "OFF",
            "t": int(time.time()),
        }

    def request_publish(self):
        self._publish.set()

    def publish_state(self):
        if self.link is None or self.switch_on is None:
            return False
        return self.link.publish(
            dogcam_mqtt.POWER_STATE_TOPIC, json.dumps(self.state_payload()), retain=True
        )

    def start_publisher(self):
        if self._publisher is not None:
            return
        self._publish.set()
        self._publisher = threading.Thread(target=self._publish_loop, daemon=True, name="power-state")
        self._publisher.start()

    def _publish_loop(self):
        while not self._stopped.is_set():
            self._publish.wait(POWER_STATE_INTERVAL)
            if self._stopped.is_set():
                return
            self._publish.clear()
            try:
                self.publish_state()
            except Exception as e:
                log.warning(f"Power state publish failed: {e}")

    def stop(self):
        self._stopped.set()
        self._publish.set()
        if self._publisher is not None:
            self._publisher.join(timeout=1)
        if self.link is not None:
            self.link.stop()


remote_power = RemotePower(REMOTE_POWER_FILE)


def start_remote_power(power=None, wait=None):
    """Load the persisted remote value, then follow the broker if MQTT is on.

    The persisted value (no file -> ON) stays in force while the broker is
    unreachable; a retained power/set delivered within `wait` seconds is in
    place before the first apply_state().
    """
    power = power or remote_power
    power.load()
    log.info(f"Remote power from {power.path}: {power.remote}")
    if not dogcam_mqtt.enabled():
        log.info("MQTT disabled (DOGCAM_MQTT=0); remote power stays at the persisted value")
        return None
    link = dogcam_mqtt.MqttLink(
        dogcam_mqtt.client_id(dogcam_mqtt.POWER_CLIENT_ID_ENV, dogcam_mqtt.POWER_CLIENT_ID),
        subscriptions={dogcam_mqtt.POWER_SET_TOPIC: power.handle_set},
        will=(dogcam_mqtt.STATUS_TOPIC, "offline"),
        online=(dogcam_mqtt.STATUS_TOPIC, "online"),
        on_connect=power.request_publish,
    )
    if not link.start():
        return None
    power.link = link
    power.start_publisher()
    if not power.wait_for_remote(REMOTE_START_WAIT if wait is None else wait):
        log.info(f"No power/set from the broker yet; remote power stays {power.remote}")
    return link


def start():
    log.info("ON: starting Flask...")
    led_flicker()
    reset_failed_services()
    run(["sudo", "systemctl", "start", STREAM_SVC])

    for i in range(60):
        if site_up():
            if ENABLE_CLOUDFLARED:
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
    if ENABLE_CLOUDFLARED:
        run(["sudo", "systemctl", "--no-block", "stop", CF_SVC], timeout=5)
    run(["sudo", "systemctl", "--no-block", "stop", STREAM_SVC], timeout=5)
    if ENABLE_CLOUDFLARED:
        stop_service(CF_SVC)
    stop_service(STREAM_SVC)
    reset_failed_services()

    try:
        os.remove(SHUTDOWN_FILE)
    except Exception:
        pass

    led_stop_flicker()
    led_off()
    log.info("Services stopped")


def active_or_stopping(name):
    return service_state(name) in {"active", "activating", "deactivating"}


def apply_state(state, force_led=False):
    if state == SWITCH_ON_VALUE:
        cf_ready = not ENABLE_CLOUDFLARED or service_state(CF_SVC) == "active"
        if not site_up() or service_state(STREAM_SVC) != "active" or not cf_ready:
            start()
        elif force_led:
            led_on()
        return

    cf_active = ENABLE_CLOUDFLARED and active_or_stopping(CF_SVC)
    if site_up() or active_or_stopping(STREAM_SVC) or cf_active:
        stop()
    else:
        if force_led:
            led_off()


def run_always_on():
    log.info("No GPIO switch detected; keeping camera services on unless remote power is OFF")
    remote_power.set_switch(True)
    remote_power.take_change()
    if remote_power.remote == "OFF":
        log.info("Remote power is OFF: keeping the camera off")
    apply_state(effective_state(SWITCH_ON_VALUE), force_led=True)
    last_reconcile = time.time()
    try:
        while True:
            if remote_power.take_change():
                log.info(f"Remote power {remote_power.remote}: applying now")
                apply_state(effective_state(SWITCH_ON_VALUE), force_led=True)
            if time.time() - last_reconcile >= SWITCH_RECONCILE_INTERVAL:
                apply_state(effective_state(SWITCH_ON_VALUE))
                last_reconcile = time.time()
            time.sleep(0.1)
    except KeyboardInterrupt:
        log.info("Stopped")


def main():
    if SWITCH_MODE == "always-on":
        run_always_on()
        return

    h = lgpio.gpiochip_open(0)
    mode = detect_switch_mode(h)

    if mode == "always-on":
        lgpio.gpiochip_close(h)
        run_always_on()
        return

    read_pull_value(h, SWITCH_PIN, lgpio.SET_PULL_UP)

    def read():
        values = []
        for _ in range(3):
            values.append(lgpio.gpio_read(h, SWITCH_PIN))
            time.sleep(0.017)
        return stable_gpio_value(values)

    state = None
    while state is None:
        state = read()

    log.info(f"Switch is {switch_label(state)} on startup (GPIO{SWITCH_PIN}={state})")

    remote_power.set_switch(state == SWITCH_ON_VALUE)
    remote_power.take_change()
    if remote_power.remote == "OFF":
        log.info("Remote power is OFF: keeping the camera off")
    apply_state(effective_state(state), force_led=True)

    log.info(f"Monitoring GPIO{SWITCH_PIN} for switch changes...")
    last_log = time.time()
    last_reconcile = time.time()

    try:
        while True:
            new = read()
            if new is not None and new != state:
                state = new
                remote_power.set_switch(state == SWITCH_ON_VALUE)
                log.info(f"Switch changed to {switch_label(state)} (GPIO{SWITCH_PIN}={state})")
                apply_state(effective_state(state))
            if remote_power.take_change():
                log.info(f"Remote power {remote_power.remote}: applying now")
                apply_state(effective_state(state), force_led=True)
            if time.time() - last_log >= SWITCH_LOG_INTERVAL:
                log.info(f"Switch raw state: GPIO{SWITCH_PIN}={state} ({switch_label(state)})")
                last_log = time.time()
            if time.time() - last_reconcile >= SWITCH_RECONCILE_INTERVAL:
                apply_state(effective_state(state))
                last_reconcile = time.time()
            time.sleep(0.1)
    except KeyboardInterrupt:
        log.info("Stopped")
    finally:
        lgpio.gpiochip_close(h)


if __name__ == "__main__":
    start_remote_power()
    main()
