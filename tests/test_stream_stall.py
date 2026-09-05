"""Regression test for the stalled-camera thread-exhaustion bug.

Scenario (as observed in production): Picamera2 initializes fine, so the app
reports camera_available=True, but the pipeline delivers zero frames. Every
/video_feed request then blocked forever on the frame condition, and because
gunicorn runs 1 worker x 4 threads, four such requests made the whole app
unresponsive -> external watchdog restart -> proxy error page.

This test runs the real Flask app under a real threaded WSGI server with the
same 4-thread budget, using stub modules for the Pi-only hardware libraries.
It must FAIL on the old code and PASS on the fix.

Run:  python3 -m pytest tests/ -v      (or: python3 tests/test_stream_stall.py)
"""

import http.client
import os
import socket
import sys
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from wsgiref.simple_server import WSGIServer, make_server
from socketserver import ThreadingMixIn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _install_hardware_stubs():
    """Fake adafruit_dht / board / picamera2 / libcamera so the app imports off-Pi."""
    for name in ("adafruit_dht", "board"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["board"].D4 = 4

    libcamera = types.ModuleType("libcamera")

    class Transform:
        def __init__(self, **kw):
            self.kw = kw

    libcamera.Transform = Transform
    libcamera.controls = types.SimpleNamespace(
        AfModeEnum=types.SimpleNamespace(Auto=1), AfTriggerEnum=types.SimpleNamespace(Start=1)
    )
    sys.modules["libcamera"] = libcamera

    picamera2 = types.ModuleType("picamera2")
    state = {"instances": [], "outputs": []}

    class Picamera2:
        def __init__(self, idx=0):
            state["instances"].append(self)
            self.closed = False

        @staticmethod
        def global_camera_info():
            return [{"Model": "stub"}]

        def create_video_configuration(self, **kw):
            return kw

        def configure(self, cfg):
            pass

        def start_recording(self, encoder, output):
            # Real camera: frames arrive asynchronously via output.write().
            # Stalled camera: nothing ever arrives. We simply record the output
            # so the test can drive it (or not).
            state["outputs"].append(output.file if hasattr(output, "file") else output)

        def set_controls(self, controls):
            pass

        def stop_recording(self):
            pass

        def close(self):
            self.closed = True

    picamera2.Picamera2 = Picamera2
    enc = types.ModuleType("picamera2.encoders")
    enc.JpegEncoder = lambda: object()
    out = types.ModuleType("picamera2.outputs")

    class FileOutput:
        def __init__(self, f):
            self.file = f

    out.FileOutput = FileOutput
    sys.modules["picamera2"] = picamera2
    sys.modules["picamera2.encoders"] = enc
    sys.modules["picamera2.outputs"] = out
    return state


class _ThreadedWSGI(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    # Mirror gunicorn --threads 4 : at most 4 concurrent request handlers.
    request_queue_size = 32


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class StalledCameraTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["TRUST_PROXY_AUTH_HEADERS"] = "1"
        os.environ["STREAM_FRAME_TIMEOUT"] = "1"
        os.environ["STREAM_STALL_RESTART_AFTER"] = "3"
        os.environ["STREAM_STALL_CHECK_INTERVAL"] = "1"
        os.environ["SECRET_KEY"] = "test"
        cls.hw = _install_hardware_stubs()
        # Servo module imports RPi.GPIO; let it fail like on a dev box.
        import dogcam_stream

        cls.mod = dogcam_stream
        cls.port = _free_port()

        # Emulate gunicorn's fixed thread pool: a semaphore around the handler.
        pool = threading.Semaphore(4)

        def limited_app(environ, start_response):
            # Hold a pool slot for the whole response iteration, exactly like a
            # gunicorn gthread worker does — but stream chunks as they are
            # yielded (don't drain into a list, or an infinite MJPEG body would
            # never send its headers).
            pool.acquire()
            try:
                body = dogcam_stream.app(environ, start_response)
            except BaseException:
                pool.release()
                raise

            def stream():
                try:
                    yield from body
                finally:
                    if hasattr(body, "close"):
                        body.close()
                    pool.release()

            return stream()

        cls.server = make_server("127.0.0.1", cls.port, limited_app, server_class=_ThreadedWSGI)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _get(self, path, timeout=3):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request("GET", path, headers={"Remote-User": "test"})
            resp = conn.getresponse()
            return resp.status
        finally:
            conn.close()

    def _open_stream(self, timeout=8):
        """Start a /video_feed request and hold it open (don't read to EOF)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        conn.request("GET", "/video_feed", headers={"Remote-User": "test"})
        return conn

    def test_camera_reports_available_but_no_frames(self):
        self.assertTrue(self.mod.camera_available)
        self.assertEqual(self.mod.output.frame_id, 0)
        self.assertEqual(self._get("/camera_status"), 200)

    def test_stalled_video_feed_does_not_exhaust_worker_threads(self):
        """THE bug. Four stalled /video_feed requests must not make '/' hang."""
        conns = []
        try:
            # Fire 5 concurrent viewers (more than the 4-thread budget) that
            # will never receive a frame.
            with ThreadPoolExecutor(max_workers=5) as ex:
                futs = [ex.submit(self._open_stream) for _ in range(5)]
                time.sleep(0.5)
                # While they are all "streaming", a plain page request must
                # still be answered promptly.
                t0 = time.time()
                try:
                    status = self._get("/", timeout=6)
                except (socket.timeout, TimeoutError, OSError) as e:
                    self.fail(f"app wedged: '/' did not answer within 6s while 5 stalled viewers were open ({e!r})")
                elapsed = time.time() - t0
                self.assertEqual(status, 200)
                self.assertLess(elapsed, 5, f"'/' took {elapsed:.1f}s: threads are being held by stalled streams")
                for f in futs:
                    try:
                        conns.append(f.result(timeout=10))
                    except Exception:
                        pass
        finally:
            for c in conns:
                try:
                    c.close()
                except Exception:
                    pass

    def test_stalled_stream_reports_unhealthy_and_fails_fast(self):
        # Never had a frame -> unhealthy.
        self.assertEqual(self._get("/stream_health"), 503)
        # Once we've observed a stall (frame timeout elapsed since last frame),
        # /video_feed fails fast with 503 rather than holding the connection.
        self.mod.output.write(b"\xff\xd8fake\xff\xd9")
        time.sleep(float(os.environ["STREAM_FRAME_TIMEOUT"]) + 0.5)
        t0 = time.time()
        self.assertEqual(self._get("/video_feed"), 503)
        self.assertLess(time.time() - t0, 2)

    def test_stall_monitor_restarts_camera_pipeline(self):
        before = len(self.hw["instances"])
        # Stall condition: camera available, no frames for > STREAM_STALL_RESTART_AFTER.
        deadline = time.time() + 15
        while time.time() < deadline and len(self.hw["instances"]) == before:
            time.sleep(0.5)
        self.assertGreater(len(self.hw["instances"]), before, "stall monitor never re-created the camera pipeline")
        self.assertTrue(self.hw["instances"][before - 1].closed, "old pipeline was not closed before re-creating")

    def test_live_frames_stream_and_release_thread(self):
        # Simulate a healthy camera: pump frames on a background thread.
        stop = threading.Event()

        def pump():
            while not stop.is_set():
                self.mod.output.write(b"\xff\xd8frame\xff\xd9")
                time.sleep(0.05)

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        try:
            time.sleep(0.2)
            self.assertEqual(self._get("/stream_health"), 200)
            conn = self._open_stream()
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertIn("multipart/x-mixed-replace", resp.getheader("Content-Type"))
            chunk = resp.read(200)
            self.assertIn(b"--frame", chunk)
            conn.close()
        finally:
            stop.set()
            t.join()


if __name__ == "__main__":
    unittest.main(verbosity=2)
