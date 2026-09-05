import io
import os
import time
import threading
import atexit
import logging
from datetime import timedelta
from urllib.parse import urlsplit
import json
import urllib.request

import adafruit_dht
import board
from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for
from functools import wraps
from werkzeug.middleware.proxy_fix import ProxyFix

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()


def env_flag(name, default="0"):
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24).hex())
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=1)
if env_flag("TRUST_PROXY_HEADERS"):
    proxy_prefix_count = 1 if env_flag("TRUST_PROXY_PREFIX_HEADERS") else 0
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=proxy_prefix_count)
viewer_semaphore = threading.Semaphore(int(os.getenv("MAX_VIEWERS", 3)))

camera = None
camera_available = False
camera_running = False
camera_lock = threading.Lock()

dht_device = None
dht_lock = threading.Lock()
last_dht_read = 0
cached_temp = None
cached_humidity = None

STREAM_STATE_FILE = "/tmp/stream_enabled"
SHUTDOWN_STATE_FILE = "/tmp/shutdown_pending"

TEMP_SOURCE = os.environ.get("TEMP_SOURCE", "sensor").strip().lower()
HA_URL = os.environ.get("HA_URL", "").strip()
HA_TOKEN = os.environ.get("HA_TOKEN", "").strip()
HA_TEMP_ENTITY = os.environ.get("HA_TEMP_ENTITY", "sensor.casa_sensor_temperature")
HA_HUMIDITY_ENTITY = os.environ.get("HA_HUMIDITY_ENTITY", "sensor.casa_sensor_humidity")

try:
    from servo_control_rpigpio import servo_controller

    servo_available = True
    logger.info("Servo control loaded")
except Exception as e:
    logger.error(f"Servo control not available: {e}")
    servo_available = False
    servo_controller = None


class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.frame_id = 0
        self.last_frame_at = 0.0
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.frame_id += 1
            self.last_frame_at = time.monotonic()
            self.condition.notify_all()

    def wait_for_frame(self, last_id, timeout):
        """Block until a frame newer than last_id exists, or timeout.

        Returns (frame_id, frame) or (last_id, None) on timeout. Never blocks
        forever: a stalled camera must not pin a gunicorn thread indefinitely.
        """
        with self.condition:
            if self.frame_id == last_id:
                self.condition.wait(timeout)
            if self.frame_id == last_id:
                return last_id, None
            return self.frame_id, self.frame

    def seconds_since_frame(self):
        with self.condition:
            if self.frame_id == 0:
                return None
            return time.monotonic() - self.last_frame_at


output = StreamingOutput()

# Frame-stall handling. The camera can enumerate + "start" fine yet deliver
# zero frames (loose CSI ribbon, under-voltage stall, pipeline wedge). Without
# a timeout each /video_feed request waits forever on the frame condition, and
# with gunicorn --workers 1 --threads 4 four such requests (one <img> plus a
# couple of reloads) make the whole app unresponsive. The external watchdog
# then restarts it and the proxy bounces the user to the home page: "crash".
STREAM_FRAME_TIMEOUT = float(os.getenv("STREAM_FRAME_TIMEOUT", "5"))
STREAM_STALL_RESTART_AFTER = float(os.getenv("STREAM_STALL_RESTART_AFTER", "20"))
STREAM_STALL_CHECK_INTERVAL = float(os.getenv("STREAM_STALL_CHECK_INTERVAL", "5"))


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_authenticated():
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)

    return decorated_function


def authelia_user():
    if not trust_proxy_auth_headers():
        return None
    return request.headers.get("Remote-User")


def authelia_groups():
    if not trust_proxy_auth_headers():
        return set()
    groups = request.headers.get("Remote-Groups", "")
    return {group.strip() for group in groups.split(",") if group.strip()}


def env_set(name, default):
    return {item.strip() for item in os.getenv(name, default).split(",") if item.strip()}


def trust_proxy_auth_headers():
    return env_flag("TRUST_PROXY_AUTH_HEADERS")


def is_authenticated():
    return bool(authelia_user()) or bool(session.get("logged_in"))


def can_control_camera():
    if authelia_user():
        groups = authelia_groups()
        return bool(groups & env_set("DOGCAM_CONTROL_GROUPS", "admin,admins,dogo_operators"))
    return bool(session.get("logged_in"))


def env_url(name, default=""):
    return os.getenv(name, default).strip()


def camera_view():
    value = os.getenv("DOGCAM_CAMERA_VIEW", "normal").strip().lower().replace("-", "_")
    if value in {"", "normal"}:
        return "normal"
    if value in {"upside_down", "inverted", "rotated_180", "180"}:
        return "upside_down"
    logger.warning(f"Unsupported DOGCAM_CAMERA_VIEW={value!r}; using normal")
    return "normal"


def is_local_logout_url(value):
    parsed = urlsplit(value)
    if parsed.path != url_for("logout"):
        return False
    return not parsed.scheme and not parsed.netloc


def logout_redirect_url():
    configured_url = env_url("DOGCAM_LOGOUT_URL")
    if configured_url and not is_local_logout_url(configured_url):
        return configured_url
    return url_for("index")


def camera_control_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_authenticated():
            return redirect(url_for("login", next=request.url))
        if not can_control_camera():
            return jsonify({"error": "Camera control is not allowed for this user"}), 403
        return f(*args, **kwargs)

    return decorated_function


def init_camera():
    global camera
    global camera_available
    global camera_running

    if camera is not None:
        return camera_available

    try:
        from libcamera import Transform
        from picamera2 import Picamera2
        from picamera2.encoders import JpegEncoder
        from picamera2.outputs import FileOutput

        logger.info("Attempting to initialize camera")
        camera_info = Picamera2.global_camera_info()
        if not camera_info:
            raise RuntimeError("No cameras detected by Picamera2")
        logger.info(f"Detected cameras: {camera_info}")
        camera = Picamera2(0)
        view = camera_view()
        # Cap the framerate to limit the camera+encoder peak current draw. On a
        # Pi 3B the aggregate load can spike the 5V rail into brown-out
        # (under-voltage); a lower, fixed framerate keeps it comfortably on.
        max_fps = max(1, int(os.getenv("STREAM_MAX_FPS", "15")))
        frame_us = int(1_000_000 / max_fps)
        config = camera.create_video_configuration(
            main={"size": (640, 480)},
            transform=Transform(hflip=view == "upside_down", vflip=view == "upside_down"),
            controls={"FrameDurationLimits": (frame_us, frame_us)},
        )
        camera.configure(config)
        camera.start_recording(JpegEncoder(), FileOutput(output))
        # Single-shot autofocus at startup, then hold: avoids the imx708 AF motor
        # hunting continuously (extra draw + PDAF log spam) while still focusing
        # the real scene so the image stays sharp. No-op on fixed-focus cameras.
        try:
            from libcamera import controls as _af
            camera.set_controls({"AfMode": _af.AfModeEnum.Auto, "AfTrigger": _af.AfTriggerEnum.Start})
            logger.info("Autofocus: single-shot at startup")
        except Exception as _afe:
            logger.debug(f"Autofocus not available (fixed-focus camera?): {_afe}")
        camera_available = True
        camera_running = True
        logger.info(f"Camera initialized successfully with {view} view")
        return True
    except Exception as e:
        logger.error(f"Camera initialization failed: {e}")
        camera_available = False
        camera_running = False
        return False


def _teardown_camera():
    """Best-effort stop+close of the current Picamera2 instance. Caller holds camera_lock."""
    global camera, camera_running
    if camera is None:
        return
    for step in ("stop_recording", "close"):
        try:
            getattr(camera, step)()
        except Exception as e:
            logger.warning(f"Camera {step} during restart failed: {e}")
    camera = None
    camera_running = False


def restart_camera(reason):
    """Tear down and re-create the camera pipeline in-process.

    Used when the pipeline is 'running' but no frames arrive. Much cheaper than
    letting the external watchdog kill the whole gunicorn process, and it does
    not drop the HTTP listener, so the UI shows a stalled-stream notice instead
    of a proxy error page.
    """
    with camera_lock:
        logger.warning(f"Restarting camera pipeline: {reason}")
        _teardown_camera()
        ok = init_camera()
        logger.warning(f"Camera pipeline restart {'succeeded' if ok else 'FAILED'}")
        return ok


def stream_is_stalled():
    """True when the camera claims to be running but frames stopped arriving."""
    if not camera_available or not camera_running or not get_stream_state():
        return False
    age = output.seconds_since_frame()
    if age is None:
        # Never produced a frame since (re)start. Give it the same grace period.
        return _camera_started_at is not None and time.monotonic() - _camera_started_at > STREAM_STALL_RESTART_AFTER
    return age > STREAM_STALL_RESTART_AFTER


_camera_started_at = None


def _mark_camera_started():
    global _camera_started_at
    _camera_started_at = time.monotonic()


def monitor_stream_stall():
    """Background thread: restart the camera pipeline if frames stop arriving."""
    consecutive_failures = 0
    while True:
        time.sleep(STREAM_STALL_CHECK_INTERVAL)
        if is_shutdown_pending():
            break
        try:
            if not stream_is_stalled():
                consecutive_failures = 0
                continue
            age = output.seconds_since_frame()
            desc = "no frames since start" if age is None else f"last frame {age:.0f}s ago"
            if restart_camera(desc):
                _mark_camera_started()
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                # Back off so a dead camera doesn't spin the CPU (and power) forever.
                time.sleep(min(60, STREAM_STALL_CHECK_INTERVAL * (2**consecutive_failures)))
        except Exception as e:
            logger.error(f"Stall monitor error: {e}")


def get_stream_state():
    try:
        with open(STREAM_STATE_FILE, "r") as f:
            return f.read().strip() == "1"
    except Exception:
        return True


def is_shutdown_pending():
    try:
        with open(SHUTDOWN_STATE_FILE, "r") as f:
            return f.read().strip() == "1"
    except Exception:
        return False


def check_shutdown_and_stop_camera():
    global camera_running

    while True:
        if is_shutdown_pending():
            with camera_lock:
                if camera_running and camera is not None:
                    logger.info("Shutdown pending - stopping camera")
                    try:
                        camera.stop_recording()
                        camera_running = False
                        logger.info("Camera stopped successfully")
                    except Exception as e:
                        logger.error(f"Error stopping camera: {e}")
            break
        time.sleep(0.5)


def cleanup():
    global camera_running

    with camera_lock:
        if camera_running and camera is not None:
            try:
                camera.stop_recording()
                camera_running = False
            except Exception:
                pass

    if servo_available and servo_controller:
        servo_controller.cleanup()


init_camera()
_mark_camera_started()

if servo_available and servo_controller:
    servo_controller.initialize()

shutdown_monitor = threading.Thread(target=check_shutdown_and_stop_camera, daemon=True)
shutdown_monitor.start()

stall_monitor = threading.Thread(target=monitor_stream_stall, daemon=True, name="stream-stall-monitor")
stall_monitor.start()

atexit.register(cleanup)


@app.route("/login", methods=["GET", "POST"])
def login():
    next_page = request.args.get("next")
    if request.method == "GET" and authelia_user():
        return redirect(next_page or url_for("index"))

    error = None

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        if username == os.getenv("BASIC_AUTH_USERNAME") and password == os.getenv("BASIC_AUTH_PASSWORD"):
            session["logged_in"] = True
            session.permanent = True
            if next_page:
                return redirect(next_page)
            return redirect(url_for("index"))
        error = "Invalid username or password. Please try again."

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    has_authelia_user = bool(authelia_user())
    has_local_session = bool(session.get("logged_in"))
    if not has_authelia_user and not has_local_session:
        abort(403)
    session.clear()
    if has_authelia_user:
        return redirect(logout_redirect_url())
    return redirect(url_for("login"))


def gen():
    """MJPEG frame generator.

    Bounded waits: if no new frame arrives within STREAM_FRAME_TIMEOUT the
    response ends cleanly so the <img> onerror fires client-side and, more
    importantly, the gunicorn thread is released. Previously this waited
    forever, so a stalled camera wedged one thread per request until the app
    stopped answering entirely.
    """
    last_id = 0
    while True:
        if not camera_available or not camera_running:
            return
        last_id, frame = output.wait_for_frame(last_id, STREAM_FRAME_TIMEOUT)
        if frame is None:
            logger.warning(f"video_feed: no frame for {STREAM_FRAME_TIMEOUT:.0f}s, closing stream")
            return
        yield b"--frame\r\n" b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"


def gen_with_viewer_slot():
    """Hold the viewer semaphore for the lifetime of the stream, not just the
    handler call. The old code released it in a `finally` before the generator
    ran, so MAX_VIEWERS was never actually enforced."""
    try:
        yield from gen()
    finally:
        viewer_semaphore.release()


@app.route("/")
@login_required
def index():
    dog_name = os.getenv("DOG_NAME", "Dog")
    return render_template(
        "index.html",
        dog_name=dog_name,
        camera_available=camera_available,
        servo_available=servo_available and can_control_camera(),
        home_url=env_url("DOGCAM_HOME_URL", "/"),
        auth_settings_url=env_url("DOGCAM_AUTH_SETTINGS_URL"),
        logout_url=url_for("logout"),
        camera_view=camera_view(),
    )


@app.route("/video_feed")
@login_required
def video_feed():
    if not get_stream_state():
        return "Stream is currently disabled. Press the button to enable.", 503
    if not camera_available:
        return "Camera not available. Please check camera connection.", 503
    # Fail fast when the pipeline is stalled instead of holding the connection
    # open for nothing; the client retries and the stall monitor restarts the camera.
    age = output.seconds_since_frame()
    if age is not None and age > STREAM_FRAME_TIMEOUT:
        return "Camera stream stalled; recovering.", 503, {"Retry-After": "3"}
    if not viewer_semaphore.acquire(blocking=False):
        return "Max viewers reached. Try again later.", 503
    # The generator releases the slot when the stream ends (see gen_with_viewer_slot).
    return Response(gen_with_viewer_slot(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/stream_health")
@login_required
def stream_health():
    """Machine-readable stream health for the UI and external watchdogs."""
    age = output.seconds_since_frame()
    healthy = camera_available and camera_running and age is not None and age <= STREAM_FRAME_TIMEOUT
    body = {
        "camera_available": camera_available,
        "camera_running": camera_running,
        "stream_enabled": get_stream_state(),
        "frames": output.frame_id,
        "last_frame_age_s": None if age is None else round(age, 1),
        "healthy": healthy,
    }
    return jsonify(body), 200 if healthy else 503


def read_ha_entity(entity_id):
    """Read a single entity state from Home Assistant."""
    url = f"{HA_URL}/api/states/{entity_id}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
        return float(data["state"])


def read_temp_from_ha():
    """Read temperature and humidity from Home Assistant."""
    global cached_temp, cached_humidity, last_dht_read
    current_time = time.time()

    if current_time - last_dht_read < 10.0 and cached_temp is not None:
        temp_f = cached_temp * 9 / 5 + 32
        return f"Room Temp: {cached_temp:.2f}\u00b0C ({temp_f:.2f}\u00b0F) | Humidity: {cached_humidity:.1f}%"

    try:
        temperature = read_ha_entity(HA_TEMP_ENTITY)
        humidity = read_ha_entity(HA_HUMIDITY_ENTITY)
        cached_temp = temperature
        cached_humidity = humidity
        last_dht_read = current_time
        temp_f = temperature * 9 / 5 + 32
        return f"Room Temp: {temperature:.2f}\u00b0C ({temp_f:.2f}\u00b0F) | Humidity: {humidity:.1f}%"
    except Exception as e:
        if cached_temp is not None:
            temp_f = cached_temp * 9 / 5 + 32
            return f"Room Temp: {cached_temp:.2f}\u00b0C ({temp_f:.2f}\u00b0F) | Humidity: {cached_humidity:.1f}% (cached)"
        return f"HA Error: {e}"


def init_dht_sensor():
    global dht_device

    if dht_device is None:
        dht_device = adafruit_dht.DHT22(board.D4)
    return dht_device


@app.route("/temp")
@login_required
def temp():
    global last_dht_read
    global cached_temp
    global cached_humidity

    if TEMP_SOURCE == "ha":
        return read_temp_from_ha()

    current_time = time.time()

    with dht_lock:
        sensor = init_dht_sensor()

        if current_time - last_dht_read < 3.0 and cached_temp is not None:
            temp_f = cached_temp * 9 / 5 + 32
            return f"Room Temp: {cached_temp:.2f}°C ({temp_f:.2f}°F) | Humidity: {cached_humidity:.1f}%"

        for attempt in range(5):
            try:
                temperature = sensor.temperature
                humidity = sensor.humidity
                if temperature is not None and humidity is not None:
                    cached_temp = temperature
                    cached_humidity = humidity
                    last_dht_read = current_time
                    temp_f = temperature * 9 / 5 + 32
                    return f"Room Temp: {temperature:.2f}°C ({temp_f:.2f}°F) | Humidity: {humidity:.1f}%"
            except (RuntimeError, OSError):
                if attempt < 4:
                    time.sleep(2.5)
            except Exception as e:
                if cached_temp is not None:
                    temp_f = cached_temp * 9 / 5 + 32
                    return f"Room Temp: {cached_temp:.2f}°C ({temp_f:.2f}°F) | Humidity: {cached_humidity:.1f}% (cached)"
                return f"Error: {e}"

        if cached_temp is not None:
            temp_f = cached_temp * 9 / 5 + 32
            return f"Room Temp: {cached_temp:.2f}°C ({temp_f:.2f}°F) | Humidity: {cached_humidity:.1f}% (cached)"

    return "Data unavailable. Retrying soon..."


@app.route("/stream_status")
@login_required
def stream_status():
    if is_shutdown_pending():
        return "⚠️ Shutting Down..."
    if not camera_available:
        return "⚠️ Camera Not Connected"
    if not get_stream_state():
        return "🔴 Stream Paused"
    age = output.seconds_since_frame()
    if age is None or age > STREAM_FRAME_TIMEOUT:
        return "🟡 Stream Stalled — reconnecting…"
    return "🟢 Stream Active"


@app.route("/camera_status")
@login_required
def camera_status():
    return {"available": camera_available}, 200 if camera_available else 503


@app.route("/servo/move", methods=["POST"])
@camera_control_required
def servo_move():
    if not servo_available:
        return jsonify({"error": "Servo control not available"}), 503

    data = request.get_json()
    axis = data.get("axis")
    direction = data.get("direction")

    if axis == "servo1":
        success, angle, can_up, can_down = servo_controller.move_servo1(direction)
        if success:
            pos = servo_controller.get_position()
            return jsonify(
                {
                    "success": True,
                    "axis": "servo1",
                    "angle": angle,
                    "servo1": angle,
                    "servo2": pos["servo2"],
                    "can_servo1_up": can_up,
                    "can_servo1_down": can_down,
                    "can_servo2_left": pos["can_servo2_left"],
                    "can_servo2_right": pos["can_servo2_right"],
                }
            )

    if axis == "servo2":
        success, angle, can_left, can_right = servo_controller.move_servo2(direction)
        if success:
            pos = servo_controller.get_position()
            return jsonify(
                {
                    "success": True,
                    "axis": "servo2",
                    "angle": angle,
                    "servo1": pos["servo1"],
                    "servo2": angle,
                    "can_servo1_up": pos["can_servo1_up"],
                    "can_servo1_down": pos["can_servo1_down"],
                    "can_servo2_left": can_left,
                    "can_servo2_right": can_right,
                }
            )

    return jsonify({"error": "Invalid request"}), 400


@app.route("/servo/position")
@login_required
def servo_position():
    if not servo_available:
        return jsonify({"error": "Servo control not available"}), 503
    return jsonify(servo_controller.get_position())


@app.route("/servo/reset", methods=["POST"])
@camera_control_required
def servo_reset():
    if not servo_available:
        return jsonify({"error": "Servo control not available"}), 503

    success = servo_controller.reset_to_home()
    if success:
        pos = servo_controller.get_position()
        return jsonify(
            {
                "success": True,
                "servo1": pos["servo1"],
                "servo2": pos["servo2"],
                "can_servo1_up": pos["can_servo1_up"],
                "can_servo1_down": pos["can_servo1_down"],
                "can_servo2_left": pos["can_servo2_left"],
                "can_servo2_right": pos["can_servo2_right"],
            }
        )

    return jsonify({"error": "Failed to reset servos"}), 500
