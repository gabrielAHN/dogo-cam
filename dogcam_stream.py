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


# ---------------------------------------------------------------------------
# Camera capability config (env-driven). The sensor is an OV5647 behind a
# wide-angle lens on a Pi 3B with a marginal PSU: it brown-outs intermittently
# even at idle. Bench testing (tools/camtest.py) showed resolution up to 1080p
# at a capped framerate costs no more under-voltage than VGA -- the FrameDuration
# cap, not the pixel count, bounds the peak current draw. So we raise the default
# resolution to use the lens, keep the fps cap, and expose every image-quality
# knob via env so a fresh brown-out can be fixed with .env alone (no code change).
# ---------------------------------------------------------------------------
def _env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning(f"Invalid float for {name!r}; using {default}")
        return float(default)


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning(f"Invalid int for {name!r}; using {default}")
        return int(default)


STREAM_WIDTH = max(160, _env_int("STREAM_WIDTH", 1296))
STREAM_HEIGHT = max(120, _env_int("STREAM_HEIGHT", 972))
STREAM_MAX_FPS = max(1, _env_int("STREAM_MAX_FPS", 15))

# Digital zoom (ScalerCrop). 1.0 = full field of view; higher crops toward the
# centre. Handy to counteract the wide-angle lens and frame the dog. Costs ~no
# extra power (the ISP already scales the frame).
ZOOM_MIN = 1.0
ZOOM_MAX = _env_float("CAM_ZOOM_MAX", 4.0)
ZOOM_STATE_FILE = "/tmp/dogcam_zoom.json"
current_zoom = ZOOM_MIN

# AWB / noise-reduction name -> libcamera enum int (ints are accepted directly
# by set_controls; AwbMode range is (0,7), NoiseReductionMode (0,4)).
_AWB_MODES = {
    "auto": 0, "incandescent": 1, "tungsten": 2, "fluorescent": 3,
    "indoor": 4, "daylight": 5, "cloudy": 6, "custom": 7,
}
_NR_MODES = {"off": 0, "fast": 1, "high_quality": 2, "minimal": 3, "zsl": 4}


def build_tuning_controls():
    """Assemble the libcamera image-tuning controls set from env vars.

    All are optional: unset -> leave the ISP default. Every value is clamped to
    the sensor's advertised range so a bad .env can't wedge camera init.
    """
    c = {}
    for env, key, lo, hi in (
        ("CAM_SHARPNESS", "Sharpness", 0.0, 16.0),
        ("CAM_CONTRAST", "Contrast", 0.0, 32.0),
        ("CAM_SATURATION", "Saturation", 0.0, 32.0),
    ):
        raw = os.getenv(env)
        if raw is not None:
            c[key] = max(lo, min(hi, _env_float(env, lo)))
    if os.getenv("CAM_BRIGHTNESS") is not None:
        c["Brightness"] = max(-1.0, min(1.0, _env_float("CAM_BRIGHTNESS", 0.0)))
    if os.getenv("CAM_EV") is not None:
        c["ExposureValue"] = max(-8.0, min(8.0, _env_float("CAM_EV", 0.0)))
    awb = os.getenv("CAM_AWB_MODE", "").strip().lower()
    if awb in _AWB_MODES:
        c["AwbEnable"] = True
        c["AwbMode"] = _AWB_MODES[awb]
    nr = os.getenv("CAM_NOISE_REDUCTION", "").strip().lower()
    if nr in _NR_MODES:
        c["NoiseReductionMode"] = _NR_MODES[nr]
    return c


def _clamp_zoom(zoom):
    try:
        zoom = float(zoom)
    except (TypeError, ValueError):
        return ZOOM_MIN
    return max(ZOOM_MIN, min(ZOOM_MAX, zoom))


def _scaler_crop_for_zoom(zoom):
    """ScalerCrop rectangle (in full sensor-array pixels) for a zoom factor."""
    fw, fh = camera.camera_properties.get("PixelArraySize", (2592, 1944))
    zoom = _clamp_zoom(zoom)
    cw, ch = int(fw / zoom), int(fh / zoom)
    cx, cy = (fw - cw) // 2, (fh - ch) // 2
    return (cx, cy, cw, ch)


def _save_zoom():
    try:
        with open(ZOOM_STATE_FILE, "w") as f:
            json.dump({"zoom": current_zoom}, f)
    except Exception as e:
        logger.debug(f"Could not persist zoom: {e}")


def _load_zoom():
    global current_zoom
    try:
        with open(ZOOM_STATE_FILE) as f:
            current_zoom = _clamp_zoom(json.load(f).get("zoom", ZOOM_MIN))
    except Exception:
        current_zoom = _clamp_zoom(_env_float("CAM_ZOOM", ZOOM_MIN))


def apply_zoom(zoom):
    """Apply a digital-zoom factor live via ScalerCrop. Returns the applied value."""
    global current_zoom
    with camera_lock:
        if camera is None:
            return None
        crop = _scaler_crop_for_zoom(zoom)
        camera.set_controls({"ScalerCrop": crop})
        current_zoom = _clamp_zoom(zoom)
    _save_zoom()
    logger.info(f"Digital zoom set to {current_zoom:.2f}x -> ScalerCrop {crop}")
    return current_zoom


# ---------------------------------------------------------------------------
# Automatic day / night mode (NoIR camera). The lens has no IR-cut filter, so:
#   * in daylight the colour is usable but IR-tinted (magenta cast);
#   * in the dark the sensor still sees IR, but colour is pure noise.
# So we run COLOUR by day and GRAYSCALE by night, switched off the camera's own
# AE light meter (Lux) -- the "sensor". Two thresholds with a sustained-time
# requirement give hysteresis so dusk/passing headlights can't make it flap.
# Switching is pure ISP set_controls(): no pipeline restart, no extra power.
# ---------------------------------------------------------------------------
DAYNIGHT_ENABLED = env_flag("DAYNIGHT_AUTO", "1")
# Auto day/night off the AE Lux meter. Design bias: SHOW COLOUR whenever there
# is real light. Only genuine darkness -> grayscale.
#   * lux < NIGHT_LUX (sustained DAYNIGHT_NIGHT_AFTER s) -> night (grayscale)
#   * lux > DAY_LUX   (sustained DAYNIGHT_DAY_AFTER s)   -> day   (colour)
#   * between the two: hold current mode (hysteresis, so dusk can't flap it)
# Defaults are LOW so a dim/evening room with a lamp on (~15-40 lux) counts as
# day and shows colour; only a dark room (lights off, <5 lux) goes grayscale.
# Recovery to day is EAGER (short sustain) and descent to night is LAZY (long),
# because being stuck in grayscale while there's light is the annoying failure;
# a few extra colour seconds at dusk is harmless.
NIGHT_LUX = _env_float("DAYNIGHT_NIGHT_LUX", 5.0)
DAY_LUX = _env_float("DAYNIGHT_DAY_LUX", 12.0)
# Guard: DAY_LUX must sit above NIGHT_LUX or the bands overlap and a scene could
# be stranded. If mis-set, rebuild a sane gap around the given night level.
if DAY_LUX <= NIGHT_LUX:
    logger.warning(
        f"DAYNIGHT_DAY_LUX ({DAY_LUX}) <= DAYNIGHT_NIGHT_LUX ({NIGHT_LUX}); "
        f"forcing DAY_LUX = NIGHT_LUX * 2 + 2"
    )
    DAY_LUX = NIGHT_LUX * 2 + 2
DAYNIGHT_CHECK_INTERVAL = _env_float("DAYNIGHT_CHECK_INTERVAL", 10.0)
# Back-compat: DAYNIGHT_SWITCH_AFTER is the default for both directions unless a
# direction-specific value is given.
_switch_after = _env_float("DAYNIGHT_SWITCH_AFTER", 30.0)
DAYNIGHT_NIGHT_AFTER = _env_float("DAYNIGHT_NIGHT_AFTER", _switch_after)
DAYNIGHT_DAY_AFTER = _env_float("DAYNIGHT_DAY_AFTER", min(_switch_after, 10.0))
# Manual override: "auto" | "day" | "night" (env sets the startup value).
DAYNIGHT_MODE_OVERRIDE = os.getenv("DAYNIGHT_MODE", "auto").strip().lower()
if DAYNIGHT_MODE_OVERRIDE not in {"auto", "day", "night"}:
    DAYNIGHT_MODE_OVERRIDE = "auto"

_daynight_lock = threading.Lock()
current_mode = "day"          # active image mode: "day" | "night"
_mode_override = DAYNIGHT_MODE_OVERRIDE
last_lux = None


def _frame_limits(fps):
    us = int(1_000_000 / max(1, fps))
    return (us, us)


def _lerp(x, x0, x1, y0, y1):
    """Linear interpolation of x in [x0,x1] -> [y0,y1], clamped."""
    if x1 <= x0:
        return y1
    t = max(0.0, min(1.0, (x - x0) / (x1 - x0)))
    return y0 + t * (y1 - y0)


# Lux-adaptive tuning. The NoIR sensor sees IR reflected differently off each
# surface, so under artificial light the colour is a patchwork (green ceiling,
# magenta fabric) that NO white balance can fix and saturation only amplifies.
# So: saturation is scaled DOWN as light gets dimmer/more artificial, and up in
# real daylight where IR is proportionally weaker. In night mode we lengthen
# exposure by lowering the frame rate (true light gathering, less power) and
# add a small EV bias scaled by how dark it actually is -- a fixed EV boost in
# a lit room blows the frame out completely (verified on-device).
CAM_SAT_BRIGHT = _env_float("CAM_SAT_BRIGHT", 1.0)   # saturation in real daylight
CAM_SAT_DIM = _env_float("CAM_SAT_DIM", 0.7)         # saturation at the day/night edge
CAM_BRIGHT_LUX = _env_float("CAM_BRIGHT_LUX", 400.0)  # lux considered "real daylight"
NIGHT_MAX_FPS = max(1, _env_int("NIGHT_MAX_FPS", 6))  # longer exposure in the dark
CAM_NIGHT_EV_MAX = _env_float("CAM_NIGHT_EV_MAX", 1.0)  # EV bias at total darkness
_last_adaptive = {}


def adaptive_controls(lux, mode):
    """Controls that vary continuously with measured light for the given mode."""
    if mode == "night":
        # darker -> more EV bias (0 at NIGHT_LUX, max at 0 lux)
        ev = _lerp(lux if lux is not None else 0.0, 0.0, NIGHT_LUX, CAM_NIGHT_EV_MAX, 0.0)
        return {"ExposureValue": round(ev, 2)}
    if lux is None:
        return {}
    sat = _lerp(lux, DAY_LUX, CAM_BRIGHT_LUX, CAM_SAT_DIM, CAM_SAT_BRIGHT)
    if os.getenv("CAM_SATURATION"):
        # explicit user saturation is the bright-end anchor; still damp when dim
        sat = _lerp(lux, DAY_LUX, CAM_BRIGHT_LUX, CAM_SAT_DIM, _env_float("CAM_SATURATION", 1.0))
    return {"Saturation": round(sat, 2)}


def _day_controls(lux=None):
    """Colour daytime controls: user tuning + AWB on + lux-scaled saturation,
    normal frame rate, neutral exposure."""
    c = build_tuning_controls()
    c["AwbEnable"] = True
    c["NoiseReductionMode"] = _NR_MODES.get(
        os.getenv("CAM_NOISE_REDUCTION", "fast").strip().lower(), 1)
    c["FrameDurationLimits"] = _frame_limits(STREAM_MAX_FPS)
    c["ExposureValue"] = max(-8.0, min(8.0, _env_float("CAM_EV", 0.0)))
    c["Brightness"] = max(-1.0, min(1.0, _env_float("CAM_BRIGHTNESS", 0.0)))
    c.update(adaptive_controls(lux if lux is not None else last_lux, "day"))
    c.setdefault("Saturation", CAM_SAT_DIM)
    return c


def _night_controls(lux=None):
    """Monochrome night controls. Colour is IR noise in the dark, so drop it;
    AWB off; HQ noise reduction; LOWER frame rate so exposure can lengthen
    (real sensitivity gain, and less power); darkness-scaled EV bias."""
    c = {
        "Saturation": 0.0,
        "AwbEnable": False,
        "NoiseReductionMode": _NR_MODES["high_quality"],
        "FrameDurationLimits": _frame_limits(min(NIGHT_MAX_FPS, STREAM_MAX_FPS)),
        "Brightness": 0.0,
    }
    c.update(adaptive_controls(lux if lux is not None else last_lux, "night"))
    return c


def _mode_controls(mode, lux=None):
    return _night_controls(lux) if mode == "night" else _day_controls(lux)


def apply_adaptive(lux):
    """Push lux-dependent controls for the current mode if they changed."""
    global _last_adaptive
    new = adaptive_controls(lux, current_mode)
    if not new:
        return
    changed = any(abs(new[k] - _last_adaptive.get(k, -999)) >= 0.05 for k in new)
    if not changed:
        return
    with camera_lock:
        if camera is None:
            return
        try:
            camera.set_controls(new)
        except Exception as e:
            logger.debug(f"adaptive set_controls failed: {e}")
            return
    _last_adaptive = dict(new)
    logger.info(f"Adaptive ({current_mode}, {lux:.0f} lux): {new}")


def read_lux():
    """Latest scene brightness from the camera AE meter. None if unavailable."""
    global last_lux
    if camera is None or not camera_running:
        return None
    try:
        md = camera.capture_metadata()
        lux = md.get("Lux")
        if lux is not None:
            last_lux = float(lux)
        return last_lux
    except Exception as e:
        logger.debug(f"read_lux failed: {e}")
        return None


def apply_daynight_mode(mode, reason=""):
    """Apply day/night image controls live. Returns the applied mode or None."""
    global current_mode, _last_adaptive
    if mode not in {"day", "night"}:
        return None
    with camera_lock:
        if camera is None:
            return None
        try:
            ctrls = _mode_controls(mode)
            camera.set_controls(ctrls)
        except Exception as e:
            logger.warning(f"Failed to apply {mode} mode: {e}")
            return None
        current_mode = mode
        _last_adaptive = {k: v for k, v in ctrls.items() if k in ("Saturation", "ExposureValue")}
    logger.info(f"Day/night: switched to {mode.upper()} mode{f' ({reason})' if reason else ''}")
    return mode


def set_daynight_override(value):
    """Set the manual override: auto | day | night. Applies immediately."""
    global _mode_override
    value = (value or "").strip().lower()
    if value not in {"auto", "day", "night"}:
        return None
    _mode_override = value
    if value in {"day", "night"}:
        apply_daynight_mode(value, reason="manual override")
    else:
        # back to auto: re-evaluate now against current light
        lux = read_lux()
        if lux is not None:
            target = "night" if lux < NIGHT_LUX else ("day" if lux > DAY_LUX else current_mode)
            if target != current_mode:
                apply_daynight_mode(target, reason=f"auto re-eval, {lux:.0f} lux")
    return _mode_override


def monitor_daynight():
    """Background thread: switch day<->night off the Lux meter with hysteresis."""
    below_since = None
    above_since = None
    while True:
        time.sleep(DAYNIGHT_CHECK_INTERVAL)
        if is_shutdown_pending():
            break
        try:
            lux = read_lux()
            if lux is None:
                continue
            # Light-adaptive tuning runs in every mode, including manual overrides.
            apply_adaptive(lux)
            if _mode_override != "auto":
                continue
            now = time.monotonic()
            if lux < NIGHT_LUX:
                above_since = None
                below_since = below_since or now
                if current_mode != "night" and now - below_since >= DAYNIGHT_NIGHT_AFTER:
                    apply_daynight_mode("night", reason=f"{lux:.0f} lux < {NIGHT_LUX:.0f}")
            elif lux > DAY_LUX:
                below_since = None
                above_since = above_since or now
                if current_mode != "day" and now - above_since >= DAYNIGHT_DAY_AFTER:
                    apply_daynight_mode("day", reason=f"{lux:.0f} lux > {DAY_LUX:.0f}")
            else:
                # in the hysteresis band: hold current mode, reset timers
                below_since = above_since = None
        except Exception as e:
            logger.error(f"Day/night monitor error: {e}")


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
    global current_mode

    if camera is not None:
        return camera_available

    try:
        from libcamera import Transform
        from picamera2 import Picamera2
        from picamera2.encoders import JpegEncoder
        from picamera2.outputs import FileOutput

        logger.info("Attempting to initialize camera")
        # NoIR camera: load the NoIR-specific libcamera tuning file so the ISP's
        # colour-correction matrix subtracts the IR contamination that otherwise
        # gives daylight a heavy magenta cast (the standard ov5647.json can't).
        # CAM_TUNING_FILE overrides; empty string forces the sensor default.
        #
        # IMPORTANT: the tuned Picamera2 MUST be constructed before any other
        # camera-manager call (e.g. global_camera_info()). That call initializes
        # the CameraManager singleton with the DEFAULT tuning and the tuning=
        # argument is then silently ignored -- verified on-device: doing the info
        # call first left AWB at the standard-tuning gains (~1.31, 1.48 = magenta)
        # instead of the NoIR gains (~1.06, 1.27 = neutral).
        tuning = None
        tuning_name = os.getenv("CAM_TUNING_FILE", "ov5647_noir.json").strip()
        if tuning_name:
            try:
                tuning = Picamera2.load_tuning_file(tuning_name)
                logger.info(f"Loaded camera tuning file: {tuning_name}")
            except Exception as _tfe:
                logger.warning(f"Could not load tuning file {tuning_name!r}, using sensor default: {_tfe}")
                tuning = None
        camera = Picamera2(0, tuning=tuning) if tuning is not None else Picamera2(0)
        logger.info(f"Camera initialized: {getattr(camera, 'camera_properties', {}).get('Model')}")
        view = camera_view()
        # Cap the framerate to limit the camera+encoder peak current draw. On a
        # Pi 3B the aggregate load can spike the 5V rail into brown-out
        # (under-voltage); a lower, fixed framerate keeps it comfortably on.
        max_fps = STREAM_MAX_FPS
        frame_us = int(1_000_000 / max_fps)
        config = camera.create_video_configuration(
            main={"size": (STREAM_WIDTH, STREAM_HEIGHT)},
            transform=Transform(hflip=view == "upside_down", vflip=view == "upside_down"),
            controls={"FrameDurationLimits": (frame_us, frame_us)},
        )
        camera.configure(config)
        camera.start_recording(JpegEncoder(), FileOutput(output))
        # Image mode (day colour / night grayscale). Day mode carries the user's
        # image tuning (sharpness/contrast/saturation/AWB/NR/EV) -- all ISP-side,
        # no measurable extra power. Applied inline (init_camera may run while
        # camera_lock is already held by restart_camera).
        try:
            if _mode_override in {"day", "night"}:
                _initial_mode = _mode_override
            else:
                _initial_mode = current_mode  # keep last mode across a restart
            camera.set_controls(_mode_controls(_initial_mode))
            current_mode = _initial_mode
            logger.info(f"Applied {_initial_mode} image mode at init")
        except Exception as _te:
            logger.warning(f"Image mode init failed: {_te}")
        # Restore persisted digital zoom (ScalerCrop). Applied inline rather than
        # via apply_zoom() because restart_camera() calls init_camera() while
        # already holding camera_lock.
        _load_zoom()
        if current_zoom > ZOOM_MIN:
            try:
                camera.set_controls({"ScalerCrop": _scaler_crop_for_zoom(current_zoom)})
                logger.info(f"Restored digital zoom {current_zoom:.2f}x")
            except Exception as _ze:
                logger.warning(f"Zoom restore failed: {_ze}")
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
        logger.info(
            f"Camera initialized: {STREAM_WIDTH}x{STREAM_HEIGHT} @<= {max_fps}fps, "
            f"{view} view, zoom {current_zoom:.2f}x"
        )
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

if DAYNIGHT_ENABLED:
    daynight_monitor = threading.Thread(target=monitor_daynight, daemon=True, name="daynight-monitor")
    daynight_monitor.start()
    logger.info(
        f"Day/night auto-switch enabled: night<{NIGHT_LUX:.0f} lux (after {DAYNIGHT_NIGHT_AFTER:.0f}s) "
        f"/ day>{DAY_LUX:.0f} lux (after {DAYNIGHT_DAY_AFTER:.0f}s), override={_mode_override}"
    )

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
        daynight_available=camera_available and DAYNIGHT_ENABLED,
        can_control_camera=can_control_camera(),
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


@app.route("/camera/info")
@login_required
def camera_info():
    """Report the camera's active configuration and capabilities for the UI."""
    return jsonify({
        "available": camera_available,
        "width": STREAM_WIDTH,
        "height": STREAM_HEIGHT,
        "max_fps": STREAM_MAX_FPS,
        "zoom": round(current_zoom, 2),
        "zoom_min": ZOOM_MIN,
        "zoom_max": ZOOM_MAX,
        "tuning": build_tuning_controls(),
        "view": camera_view(),
        "daynight": {
            "enabled": DAYNIGHT_ENABLED,
            "mode": current_mode,
            "override": _mode_override,
            "lux": None if last_lux is None else round(last_lux, 1),
            "night_lux": NIGHT_LUX,
            "day_lux": DAY_LUX,
            "adaptive": _last_adaptive,
            "night_max_fps": NIGHT_MAX_FPS,
        },
    })


@app.route("/camera/daynight", methods=["GET", "POST"])
def camera_daynight():
    """Get day/night status, or set the override (auto|day|night).

    GET is read-only for any viewer. POST changes state -> camera-control perm.
    """
    if not is_authenticated():
        return redirect(url_for("login", next=request.url))
    if request.method == "GET":
        return jsonify({
            "enabled": DAYNIGHT_ENABLED, "mode": current_mode, "override": _mode_override,
            "lux": None if last_lux is None else round(last_lux, 1),
            "night_lux": NIGHT_LUX, "day_lux": DAY_LUX, "adaptive": _last_adaptive,
        })
    if not can_control_camera():
        return jsonify({"error": "Camera control is not allowed for this user"}), 403
    if not camera_available:
        return jsonify({"error": "Camera not available"}), 503
    data = request.get_json(silent=True) or {}
    result = set_daynight_override(data.get("mode"))
    if result is None:
        return jsonify({"error": "mode must be one of auto|day|night"}), 400
    return jsonify({"success": True, "override": result, "mode": current_mode,
                    "lux": None if last_lux is None else round(last_lux, 1)})


@app.route("/camera/zoom", methods=["GET", "POST"])
def camera_zoom():
    """Get or set the digital zoom (ScalerCrop). Live, no restart, ~no power cost.

    GET is read-only (any logged-in viewer). POST changes state and requires
    camera-control permission, matching the servo endpoints.
    """
    if request.method == "GET":
        if not is_authenticated():
            return redirect(url_for("login", next=request.url))
        return jsonify({"zoom": round(current_zoom, 2), "min": ZOOM_MIN, "max": ZOOM_MAX})
    # POST
    if not is_authenticated():
        return redirect(url_for("login", next=request.url))
    if not can_control_camera():
        return jsonify({"error": "Camera control is not allowed for this user"}), 403
    if not camera_available:
        return jsonify({"error": "Camera not available"}), 503
    data = request.get_json(silent=True) or {}
    if "zoom" in data:
        target = data.get("zoom")
    else:
        # relative step, e.g. {"step": 0.5} or {"step": -0.5}
        try:
            target = current_zoom + float(data.get("step", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid step"}), 400
    applied = apply_zoom(target)
    if applied is None:
        return jsonify({"error": "Camera not available"}), 503
    return jsonify({"success": True, "zoom": round(applied, 2), "min": ZOOM_MIN, "max": ZOOM_MAX})


@app.route("/snapshot")
@login_required
def snapshot():
    """Return the latest stream frame as a still JPEG.

    Serves the frame the MJPEG pipeline already produced, so it costs no extra
    camera work or power (no full-res still capture, which would stop the stream
    and spike the 5V rail). Good enough for 'grab a photo of the dog'.
    """
    if not camera_available:
        return "Camera not available", 503
    _id, frame = output.wait_for_frame(0, STREAM_FRAME_TIMEOUT)
    if frame is None:
        frame = output.frame
    if not frame:
        return "No frame available yet", 503, {"Retry-After": "2"}
    filename = f"dogcam_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
    return Response(bytes(frame), mimetype="image/jpeg",
                    headers={"Content-Disposition": f'inline; filename="{filename}"'})


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
