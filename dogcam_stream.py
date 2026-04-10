import io
import os
import time
import threading
import atexit
import logging
from datetime import timedelta

import adafruit_dht
import board
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from functools import wraps

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24).hex())
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=1)
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
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


output = StreamingOutput()


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.url))
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
        camera = Picamera2()
        config = camera.create_video_configuration(
            main={"size": (640, 480)},
            transform=Transform(hflip=True, vflip=True),
        )
        camera.configure(config)
        camera.start_recording(JpegEncoder(), FileOutput(output))
        camera_available = True
        camera_running = True
        logger.info("Camera initialized successfully")
        return True
    except Exception as e:
        logger.error(f"Camera initialization failed: {e}")
        camera_available = False
        camera_running = False
        return False


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

if servo_available and servo_controller:
    servo_controller.initialize()

shutdown_monitor = threading.Thread(target=check_shutdown_and_stop_camera, daemon=True)
shutdown_monitor.start()

atexit.register(cleanup)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        if username == os.getenv("BASIC_AUTH_USERNAME") and password == os.getenv("BASIC_AUTH_PASSWORD"):
            session["logged_in"] = True
            session.permanent = True
            next_page = request.args.get("next")
            if next_page:
                return redirect(next_page)
            return redirect(url_for("index"))
        error = "Invalid username or password. Please try again."

    return render_template("login.html", error=error)


def gen():
    if not camera_available:
        yield b""
        return

    while True:
        with output.condition:
            output.condition.wait()
            frame = output.frame
        yield b"--frame\r\n" b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"


@app.route("/")
@login_required
def index():
    dog_name = os.getenv("DOG_NAME", "Dog")
    return render_template(
        "index.html",
        dog_name=dog_name,
        camera_available=camera_available,
        servo_available=servo_available,
    )


@app.route("/video_feed")
@login_required
def video_feed():
    if not get_stream_state():
        return "Stream is currently disabled. Press the button to enable.", 503
    if not camera_available:
        return "Camera not available. Please check camera connection.", 503
    if not viewer_semaphore.acquire(blocking=False):
        return "Max viewers reached. Try again later.", 503
    try:
        return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")
    finally:
        viewer_semaphore.release()


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
    return "🟢 Stream Active" if get_stream_state() else "🔴 Stream Paused"


@app.route("/camera_status")
@login_required
def camera_status():
    return {"available": camera_available}, 200 if camera_available else 503


@app.route("/servo/move", methods=["POST"])
@login_required
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
@login_required
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
