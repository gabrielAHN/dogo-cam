import io
import os
import time
import threading
import atexit
import board
import adafruit_dht
import RPi.GPIO as GPIO
from dotenv import load_dotenv
from flask import Flask, Response, render_template, request
from flask_httpauth import HTTPBasicAuth
from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder
from picamera2.outputs import FileOutput

load_dotenv()

app = Flask(__name__)
auth = HTTPBasicAuth()
camera = Picamera2()
viewer_semaphore = threading.Semaphore(int(os.getenv('MAX_VIEWERS', 3)))

dht_device = adafruit_dht.DHT22(board.D4)
dht_lock = threading.Lock()

# Button setup for stream control (optional)
BUTTON_PIN = 17  # GPIO17 (Physical Pin 11)
USE_BUTTON = os.getenv('USE_BUTTON', 'false').lower() == 'true'
stream_enabled = not USE_BUTTON  # If button enabled, stream starts OFF to save power
stream_lock = threading.Lock()
gpio_initialized = False


class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


output = StreamingOutput()


camera.configure(camera.create_video_configuration(main={"size": (640, 480)}))
camera.start_recording(JpegEncoder(), FileOutput(output))


def button_callback(channel):
    """Toggle stream on/off when button is pressed"""
    global stream_enabled
    time.sleep(0.05)  # Debounce delay
    if GPIO.input(BUTTON_PIN) == GPIO.HIGH:
        with stream_lock:
            stream_enabled = not stream_enabled
            print(f"Stream {'enabled' if stream_enabled else 'disabled'}")


def initialize_gpio():
    """Initialize GPIO after worker fork (for gunicorn compatibility)"""
    global gpio_initialized, stream_enabled, USE_BUTTON
    if USE_BUTTON and not gpio_initialized:
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

            # Test if button is actually connected by reading initial state
            test_read = GPIO.input(BUTTON_PIN)

            GPIO.add_event_detect(BUTTON_PIN, GPIO.RISING, callback=button_callback, bouncetime=300)
            gpio_initialized = True
            print("Button hardware detected: Stream starts OFF. Press button to turn ON.")
        except Exception as e:
            print(f"Button hardware not detected or GPIO init failed: {e}")
            print("Falling back to always-on stream mode (no button control)")
            USE_BUTTON = False
            stream_enabled = True  # Enable stream since no button is available


def cleanup():
    camera.stop_recording()
    if USE_BUTTON:
        GPIO.cleanup()


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


@app.before_request
def before_first_request():
    """Initialize GPIO on first request (after worker fork)"""
    initialize_gpio()


@app.route('/')
@auth.login_required
def index():
    dog_name = os.getenv('DOG_NAME', 'Dog')
    return render_template('index.html', dog_name=dog_name)


@app.route('/video_feed')
@auth.login_required
def video_feed():
    with stream_lock:
        if not stream_enabled:
            return "Stream is currently disabled. Press the button to enable.", 503

    if not viewer_semaphore.acquire(blocking=False):
        return "Max viewers reached. Try again later.", 503
    try:
        return Response(gen(), mimetype='multipart/x-mixed-replace; boundary=frame')
    finally:
        viewer_semaphore.release()


@app.route('/temp')
@auth.login_required
def temp():
    max_retries = 5
    with dht_lock:
        for attempt in range(max_retries):
            try:
                temperature = dht_device.temperature
                humidity = dht_device.humidity
                if temperature is not None and humidity is not None:
                    return f"Room Temp: {temperature}°C ({temperature * 9/5 + 32}°F) | Humidity: {humidity}%"
            except RuntimeError:
                if attempt < max_retries - 1:
                    time.sleep(2)
                continue
            except Exception as error:
                return f"Error: {str(error)}"
    return "Data unavailable. Retrying soon..."


@app.route('/stream_status')
@auth.login_required
def stream_status():
    with stream_lock:
        status = "🟢 Stream Active" if stream_enabled else "🔴 Stream Paused"
        return status
