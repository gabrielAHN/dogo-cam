import io
import os
import time
import threading
import atexit
import logging
import board
import adafruit_dht
from dotenv import load_dotenv
from flask import Flask, Response, render_template, request
from flask_httpauth import HTTPBasicAuth
from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder
from picamera2.outputs import FileOutput

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

app = Flask(__name__)
auth = HTTPBasicAuth()
camera = Picamera2()
viewer_semaphore = threading.Semaphore(int(os.getenv('MAX_VIEWERS', 3)))

dht_device = None
dht_lock = threading.Lock()
last_dht_read = 0
cached_temp = None
cached_humidity = None

STREAM_STATE_FILE = '/tmp/stream_enabled'
SHUTDOWN_STATE_FILE = '/tmp/shutdown_pending'
stream_lock = threading.Lock()


class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


output = StreamingOutput()


def get_stream_state():
    """Read current stream state from file (managed by button daemon)"""
    try:
        with open(STREAM_STATE_FILE, 'r') as f:
            return f.read().strip() == '1'
    except:
        return True  # Default to enabled if file doesn't exist


def is_shutdown_pending():
    """Check if shutdown is pending"""
    try:
        with open(SHUTDOWN_STATE_FILE, 'r') as f:
            return f.read().strip() == '1'
    except:
        return False


camera.configure(camera.create_video_configuration(main={"size": (640, 480)}))
camera.start_recording(JpegEncoder(), FileOutput(output))

camera_running = True
camera_lock = threading.Lock()


def check_shutdown_and_stop_camera():
    """Monitor for shutdown signal and stop camera before shutdown"""
    global camera_running
    while True:
        if is_shutdown_pending():
            with camera_lock:
                if camera_running:
                    logger.info("Shutdown pending - stopping camera...")
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
        if camera_running:
            try:
                camera.stop_recording()
                camera_running = False
            except:
                pass


# Start shutdown monitor thread
shutdown_monitor = threading.Thread(target=check_shutdown_and_stop_camera, daemon=True)
shutdown_monitor.start()

atexit.register(cleanup)


@auth.verify_password
def verify_password(username, password):
    return username == os.getenv('BASIC_AUTH_USERNAME') and password == os.getenv('BASIC_AUTH_PASSWORD')


def gen():
    while True:
        with output.condition:
            output.condition.wait()
            frame = output.frame
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')


@app.route('/')
@auth.login_required
def index():
    dog_name = os.getenv('DOG_NAME', 'Dog')
    return render_template('index.html', dog_name=dog_name)


@app.route('/video_feed')
@auth.login_required
def video_feed():
    if not get_stream_state():
        return "Stream is currently disabled. Press the button to enable.", 503

    if not viewer_semaphore.acquire(blocking=False):
        return "Max viewers reached. Try again later.", 503
    try:
        return Response(gen(), mimetype='multipart/x-mixed-replace; boundary=frame')
    finally:
        viewer_semaphore.release()


def init_dht_sensor():
    global dht_device
    if dht_device is None:
        dht_device = adafruit_dht.DHT22(board.D4)
    return dht_device


@app.route('/temp')
@auth.login_required
def temp():
    global last_dht_read, cached_temp, cached_humidity

    current_time = time.time()

    with dht_lock:

        sensor = init_dht_sensor()


        if current_time - last_dht_read < 3.0 and cached_temp is not None:
            return f"Room Temp: {cached_temp}°C ({cached_temp * 9/5 + 32}°F) | Humidity: {cached_humidity}%"


        max_retries = 5
        for attempt in range(max_retries):
            try:
                temperature = sensor.temperature
                humidity = sensor.humidity
                if temperature is not None and humidity is not None:
                    cached_temp = temperature
                    cached_humidity = humidity
                    last_dht_read = current_time
                    return f"Room Temp: {temperature}°C ({temperature * 9/5 + 32}°F) | Humidity: {humidity}%"
            except (RuntimeError, OSError):
                if attempt < max_retries - 1:
                    time.sleep(2.5)
                continue
            except Exception as error:

                if cached_temp is not None:
                    return f"Room Temp: {cached_temp}°C ({cached_temp * 9/5 + 32}°F) | Humidity: {cached_humidity}% (cached)"
                return f"Error: {str(error)}"


        if cached_temp is not None:
            return f"Room Temp: {cached_temp}°C ({cached_temp * 9/5 + 32}°F) | Humidity: {cached_humidity}% (cached)"

    return "Data unavailable. Retrying soon..."


@app.route('/stream_status')
@auth.login_required
def stream_status():
    if is_shutdown_pending():
        return "⚠️ Shutting Down..."
    status = "🟢 Stream Active" if get_stream_state() else "🔴 Stream Paused"
    return status
