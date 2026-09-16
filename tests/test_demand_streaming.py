import os
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _install_hardware_stubs():
    for name in ("adafruit_dht", "board"):
        sys.modules[name] = types.ModuleType(name)
    sys.modules["board"].D4 = 4

    libcamera = types.ModuleType("libcamera")
    libcamera.Transform = lambda **kw: types.SimpleNamespace(kw=kw)
    libcamera.controls = types.SimpleNamespace(
        AfModeEnum=types.SimpleNamespace(Auto=1),
        AfTriggerEnum=types.SimpleNamespace(Start=1),
    )
    sys.modules["libcamera"] = libcamera

    state = {"instances": [], "starts": 0, "stops": 0, "output": None}
    picamera2 = types.ModuleType("picamera2")

    class Picamera2:
        def __init__(self, idx=0, tuning=None):
            self.camera_properties = {"Model": "stub", "PixelArraySize": (2592, 1944)}
            self.configured = None
            self.closed = False
            state["instances"].append(self)

        @staticmethod
        def load_tuning_file(name):
            return {"name": name}

        def create_video_configuration(self, **kw):
            return kw

        def configure(self, config):
            self.configured = config

        def start_recording(self, encoder, output):
            state["starts"] += 1
            state["output"] = output.file

        def stop_recording(self):
            state["stops"] += 1

        def set_controls(self, controls):
            pass

        def capture_metadata(self):
            return {"Lux": 100.0}

        def close(self):
            self.closed = True

    picamera2.Picamera2 = Picamera2
    encoder = types.ModuleType("picamera2.encoders")
    encoder.JpegEncoder = lambda: object()
    outputs = types.ModuleType("picamera2.outputs")
    outputs.FileOutput = lambda value: types.SimpleNamespace(file=value)
    sys.modules["picamera2"] = picamera2
    sys.modules["picamera2.encoders"] = encoder
    sys.modules["picamera2.outputs"] = outputs
    return state


class DemandDrivenStreamingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environ = patch.dict(os.environ, {
            "TRUST_PROXY_AUTH_HEADERS": "1",
            "SECRET_KEY": "test",
            "DAYNIGHT_AUTO": "0",
            "STREAM_FRAME_TIMEOUT": "0.2",
            "STREAM_IDLE_TIMEOUT": "0.05",
            "STREAM_STALL_RESTART_AFTER": "1",
            "STREAM_STALL_CHECK_INTERVAL": "60",
        }, clear=False)
        cls.environ.start()
        cls.hw = _install_hardware_stubs()
        for name in ("dogcam_stream", "servo_control_rpigpio"):
            sys.modules.pop(name, None)
        import dogcam_stream
        cls.mod = dogcam_stream

    @classmethod
    def tearDownClass(cls):
        cls.mod.cleanup()
        cls.environ.stop()

    def setUp(self):
        m = self.mod
        with m.camera_lock:
            if m.camera_running:
                m._stop_recording_locked()
            m.active_viewers = 0
            m.snapshot_demands = 0
            m._invalidate_idle_stop_locked()
            m.output.clear()
        self.hw["starts"] = 0
        self.hw["stops"] = 0

    def _headers(self):
        return {"Remote-User": "test"}

    def test_startup_configures_camera_without_recording(self):
        self.assertTrue(self.mod.camera_available)
        self.assertIsNotNone(self.mod.camera)
        self.assertIsNotNone(self.hw["instances"][0].configured)
        self.assertFalse(self.mod.camera_running)

    def test_first_viewer_starts_and_final_release_stops_after_timeout(self):
        lease = self.mod.acquire_stream_demand("viewer")
        self.assertIsNotNone(lease)
        self.assertTrue(self.mod.camera_running)
        self.assertEqual(self.mod.active_viewers, 1)
        self.assertEqual(self.hw["starts"], 1)

        lease.release()
        self.assertTrue(self.mod.camera_running, "idle timeout should provide a reconnect cooldown")
        time.sleep(self.mod.STREAM_IDLE_TIMEOUT + 0.08)
        self.assertFalse(self.mod.camera_running)
        self.assertIsNotNone(self.mod.camera, "idle stop must keep the camera configured")
        self.assertEqual(self.hw["stops"], 1)

    def test_reconnect_invalidates_stale_idle_timer(self):
        first = self.mod.acquire_stream_demand("viewer")
        first.release()
        second = self.mod.acquire_stream_demand("viewer")
        time.sleep(self.mod.STREAM_IDLE_TIMEOUT + 0.08)
        self.assertTrue(self.mod.camera_running)
        self.assertEqual(self.mod.active_viewers, 1)
        second.release()

    def test_viewer_release_is_idempotent_across_generator_and_response_close(self):
        with self.mod.app.test_request_context("/video_feed", headers=self._headers()):
            response = self.mod.video_feed()
        self.assertEqual(self.mod.active_viewers, 1)
        iterator = iter(response.response)
        self.mod.output.write(b"fresh")
        self.assertIn(b"fresh", next(iterator))
        iterator.close()  # generator finally
        response.close()  # Response.call_on_close
        self.assertEqual(self.mod.active_viewers, 0)
        self.assertEqual(self.mod.viewer_slots_in_use, 0)

    def test_restart_clears_cached_frame_and_generator_waits_for_fresh_frame(self):
        self.mod.output.write(b"stale")
        lease = self.mod.acquire_stream_demand("viewer")
        self.assertIsNone(self.mod.output.frame)
        self.assertIsNone(self.mod.output.seconds_since_frame())
        generator = self.mod.gen(lease)
        result = {}

        def consume():
            try:
                result["chunk"] = next(generator)
            except StopIteration:
                result["chunk"] = None

        thread = threading.Thread(target=consume)
        thread.start()
        time.sleep(0.03)
        self.assertTrue(thread.is_alive(), "cached pre-start frame must not be served")
        self.mod.output.write(b"fresh")
        thread.join(1)
        self.assertIn(b"fresh", result["chunk"])
        generator.close()

    def test_stall_detection_ignores_intentional_idle(self):
        self.mod.output.write(b"old")
        with patch.object(self.mod.output, "seconds_since_frame", return_value=999):
            self.assertFalse(self.mod.stream_is_stalled())
            lease = self.mod.acquire_stream_demand("viewer")
            self.assertTrue(self.mod.stream_is_stalled())
            lease.release()

    def test_health_reports_idle_cooldown_disabled_and_demanded_failure(self):
        with self.mod.app.test_client() as client:
            idle = client.get("/stream_health", headers=self._headers())
            self.assertEqual(idle.status_code, 200)
            self.assertEqual(idle.get_json()["state"], "idle")
            self.assertEqual(idle.get_json()["viewers"], 0)

            lease = self.mod.acquire_stream_demand("viewer")
            self.mod.output.write(b"frame")
            lease.release()
            cooldown = client.get("/stream_health", headers=self._headers())
            self.assertEqual(cooldown.status_code, 200)
            self.assertEqual(cooldown.get_json()["state"], "cooldown")

            with patch.object(self.mod, "get_stream_state", return_value=False):
                disabled = client.get("/stream_health", headers=self._headers())
            self.assertEqual(disabled.status_code, 200)
            self.assertEqual(disabled.get_json()["state"], "disabled")

            demanded = self.mod.acquire_stream_demand("viewer")
            with patch.object(self.mod, "camera_available", False):
                failed = client.get("/stream_health", headers=self._headers())
            self.assertEqual(failed.status_code, 503)
            self.assertEqual(failed.get_json()["state"], "unavailable")
            demanded.release()

    def test_snapshot_takes_temporary_demand_and_returns_only_fresh_frame(self):
        self.mod.output.write(b"stale")
        result = {}

        def request_snapshot():
            with self.mod.app.test_client() as client:
                response = client.get("/snapshot", headers=self._headers())
                result["status"] = response.status_code
                result["body"] = response.data

        thread = threading.Thread(target=request_snapshot)
        thread.start()
        deadline = time.time() + 1
        while time.time() < deadline and self.mod.snapshot_demands == 0:
            time.sleep(0.005)
        self.assertEqual(self.mod.snapshot_demands, 1)
        self.assertTrue(self.mod.camera_running)
        self.mod.output.write(b"fresh-snapshot")
        thread.join(1)
        self.assertEqual(result, {"status": 200, "body": b"fresh-snapshot"})
        self.assertEqual(self.mod.snapshot_demands, 0)

    def test_ui_disconnects_while_hidden_and_never_reconnects_hidden(self):
        with self.mod.app.test_client() as client:
            html = client.get("/", headers=self._headers()).get_data(as_text=True)
        for marker in (
            "document.addEventListener('visibilitychange'",
            "window.addEventListener('pagehide'",
            "document.hidden",
            "img.removeAttribute('src')",
            "if (document.hidden) return",
            "connect(true)",
        ):
            self.assertIn(marker, html)

    def test_ui_resumes_feed_when_visible_and_server_reports_no_demand(self):
        # idle returns healthy:true now, so an unhealthy->healthy transition no
        # longer covers a clean stream-end into idle. A visible tab must reattach
        # whenever the server reports no active demand (state idle/cooldown or
        # viewers === 0), otherwise the feed freezes and the camera never resumes.
        with self.mod.app.test_client() as client:
            html = client.get("/", headers=self._headers()).get_data(as_text=True)
        self.assertIn("data.viewers === 0", html)
        self.assertIn("data.state !== 'disabled'", html)

    def test_idle_timeout_is_documented_and_watchdog_accepts_health_200(self):
        env_text = (ROOT / ".env.example").read_text()
        readme = (ROOT / "README.md").read_text()
        watchdog = (ROOT / "dogcam-watchdog.sh").read_text()
        self.assertIn("STREAM_IDLE_TIMEOUT=10", env_text)
        self.assertIn("STREAM_IDLE_TIMEOUT", readme)
        self.assertRegex(watchdog, r"200\)\s*\n\s*rm -f")


if __name__ == "__main__":
    unittest.main(verbosity=2)
