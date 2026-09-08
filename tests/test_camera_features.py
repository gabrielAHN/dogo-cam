"""Tests for the camera capability features (resolution, tuning, zoom, snapshot).

Runs off-Pi with stub hardware modules, mirroring tests/test_stream_stall.py.
The stub Picamera2 here additionally exposes camera_properties (PixelArraySize)
and records set_controls() calls so we can assert on ScalerCrop / tuning.

Run:  python3 -m unittest tests.test_camera_features
"""

import http.client
import os
import socket
import sys
import threading
import time
import types
import unittest
from wsgiref.simple_server import WSGIServer, make_server
from socketserver import ThreadingMixIn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PIXEL_ARRAY = (2592, 1944)


def _install_hardware_stubs():
    for name in ("adafruit_dht", "board"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["board"].D4 = 4

    libcamera = types.ModuleType("libcamera")

    class Transform:
        def __init__(self, **kw):
            self.kw = kw

    libcamera.Transform = Transform
    libcamera.controls = types.SimpleNamespace(
        AfModeEnum=types.SimpleNamespace(Auto=1),
        AfTriggerEnum=types.SimpleNamespace(Start=1),
    )
    sys.modules["libcamera"] = libcamera

    picamera2 = types.ModuleType("picamera2")
    state = {"instances": [], "controls": []}

    class Picamera2:
        def __init__(self, idx=0, tuning=None):
            state["instances"].append(self)
            self.closed = False
            self.tuning = tuning
            self.camera_properties = {"Model": "ov5647", "PixelArraySize": PIXEL_ARRAY}
            self.configured = None

        @staticmethod
        def global_camera_info():
            return [{"Model": "ov5647"}]

        @staticmethod
        def load_tuning_file(name):
            return {"tuning_file": name}

        def create_video_configuration(self, **kw):
            return kw

        def configure(self, cfg):
            self.configured = cfg

        def start_recording(self, encoder, output):
            pass

        def set_controls(self, controls):
            state["controls"].append(dict(controls))

        def capture_metadata(self):
            return {"Lux": state.get("lux", 200.0)}

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
    request_queue_size = 32


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class CameraFeatureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["TRUST_PROXY_AUTH_HEADERS"] = "1"
        os.environ["TRUST_PROXY_HEADERS"] = "1"
        os.environ["STREAM_FRAME_TIMEOUT"] = "1"
        os.environ["STREAM_STALL_RESTART_AFTER"] = "60"
        os.environ["STREAM_STALL_CHECK_INTERVAL"] = "30"
        os.environ["SECRET_KEY"] = "test"
        os.environ["DOGCAM_CONTROL_GROUPS"] = "admins"
        # Feature config under test:
        os.environ["STREAM_WIDTH"] = "1296"
        os.environ["STREAM_HEIGHT"] = "972"
        os.environ["CAM_SHARPNESS"] = "2.0"
        os.environ["CAM_CONTRAST"] = "1.2"
        os.environ["CAM_AWB_MODE"] = "indoor"
        os.environ["CAM_NOISE_REDUCTION"] = "high_quality"
        # Day/night: fast timings so the monitor thread acts within the test.
        os.environ["DAYNIGHT_AUTO"] = "1"
        os.environ["DAYNIGHT_NIGHT_LUX"] = "5"
        os.environ["DAYNIGHT_DAY_LUX"] = "12"
        os.environ["DAYNIGHT_CHECK_INTERVAL"] = "0.3"
        os.environ["DAYNIGHT_NIGHT_AFTER"] = "0.6"
        os.environ["DAYNIGHT_DAY_AFTER"] = "0.6"
        os.environ.pop("DAYNIGHT_SWITCH_AFTER", None)
        # start from a clean zoom state
        for f in ("/tmp/dogcam_zoom.json",):
            try:
                os.remove(f)
            except OSError:
                pass
        cls.hw = _install_hardware_stubs()
        # Force a fresh dogcam_stream import bound to THIS module's stubs, so the
        # suite is order-independent under `unittest discover` (dogcam_stream is a
        # module-level singleton that captures its stubs + env at import time).
        for _m in ("dogcam_stream", "servo_control_rpigpio"):
            sys.modules.pop(_m, None)
        import dogcam_stream
        cls.mod = dogcam_stream
        cls.port = _free_port()
        cls.server = make_server("127.0.0.1", cls.port, dogcam_stream.app,
                                 server_class=_ThreadedWSGI)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _req(self, method, path, body=None, admin=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Remote-User": "test"}
        if admin:
            headers["Remote-Groups"] = "admins"
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    # ---- resolution ----
    def test_stream_configured_at_env_resolution(self):
        self.assertEqual(self.mod.STREAM_WIDTH, 1296)
        self.assertEqual(self.mod.STREAM_HEIGHT, 972)
        cfg = self.hw["instances"][0].configured
        self.assertEqual(cfg["main"]["size"], (1296, 972))

    def test_camera_info_reports_resolution_and_zoom(self):
        import json
        status, body = self._req("GET", "/camera/info")
        self.assertEqual(status, 200)
        info = json.loads(body)
        self.assertEqual(info["width"], 1296)
        self.assertEqual(info["height"], 972)
        self.assertEqual(info["max_fps"], 15)
        self.assertIn("zoom", info)

    # ---- tuning ----
    def test_tuning_controls_applied_at_init(self):
        applied = {}
        for c in self.hw["controls"]:
            applied.update(c)
        self.assertEqual(applied.get("Sharpness"), 2.0)
        self.assertEqual(applied.get("Contrast"), 1.2)
        self.assertTrue(applied.get("AwbEnable"))
        self.assertEqual(applied.get("AwbMode"), 4)   # indoor
        self.assertEqual(applied.get("NoiseReductionMode"), 2)  # high_quality

    def test_build_tuning_clamps_out_of_range(self):
        os.environ["CAM_SHARPNESS"] = "999"
        try:
            c = self.mod.build_tuning_controls()
            self.assertLessEqual(c["Sharpness"], 16.0)
        finally:
            os.environ["CAM_SHARPNESS"] = "2.0"

    # ---- digital zoom ----
    def test_zoom_scalercrop_math_centered(self):
        crop = self.mod._scaler_crop_for_zoom(2.0)
        fw, fh = PIXEL_ARRAY
        self.assertEqual(crop, ((fw - fw // 2) // 2, (fh - fh // 2) // 2, fw // 2, fh // 2))

    def test_zoom_post_sets_scalercrop_and_clamps(self):
        import json
        n_before = len(self.hw["controls"])
        status, body = self._req("POST", "/camera/zoom", body=json.dumps({"zoom": 2.0}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["zoom"], 2.0)
        # a ScalerCrop control was pushed
        crops = [c for c in self.hw["controls"][n_before:] if "ScalerCrop" in c]
        self.assertTrue(crops, "no ScalerCrop control was applied for zoom")
        # clamp above max
        status, body = self._req("POST", "/camera/zoom", body=json.dumps({"zoom": 999}))
        self.assertEqual(json.loads(body)["zoom"], self.mod.ZOOM_MAX)
        # reset
        self._req("POST", "/camera/zoom", body=json.dumps({"zoom": 1.0}))

    def test_zoom_relative_step(self):
        import json
        self._req("POST", "/camera/zoom", body=json.dumps({"zoom": 1.0}))
        status, body = self._req("POST", "/camera/zoom", body=json.dumps({"step": 0.5}))
        self.assertAlmostEqual(json.loads(body)["zoom"], 1.5, places=2)
        self._req("POST", "/camera/zoom", body=json.dumps({"zoom": 1.0}))

    def test_zoom_post_forbidden_without_control_group(self):
        import json
        status, _ = self._req("POST", "/camera/zoom", body=json.dumps({"zoom": 2.0}), admin=False)
        self.assertEqual(status, 403)

    def test_zoom_persists_across_reload(self):
        import json
        self._req("POST", "/camera/zoom", body=json.dumps({"zoom": 3.0}))
        self.assertTrue(os.path.exists(self.mod.ZOOM_STATE_FILE))
        with open(self.mod.ZOOM_STATE_FILE) as f:
            self.assertEqual(self.mod._clamp_zoom(json.load(f)["zoom"]), 3.0)
        self._req("POST", "/camera/zoom", body=json.dumps({"zoom": 1.0}))

    # ---- snapshot ----
    def test_snapshot_returns_latest_frame_as_jpeg(self):
        self.mod.output.write(b"\xff\xd8snapshot\xff\xd9")
        status, body = self._req("GET", "/snapshot")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"\xff\xd8"))

    # ---- day / night auto-switch (NoIR) ----
    def test_night_controls_are_grayscale(self):
        c = self.mod._night_controls()
        self.assertEqual(c["Saturation"], 0.0)
        self.assertFalse(c["AwbEnable"])

    def test_day_controls_are_colour(self):
        c = self.mod._day_controls()
        self.assertTrue(c["AwbEnable"])
        self.assertGreater(c["Saturation"], 0.0)

    def test_manual_override_forces_mode(self):
        import json
        status, body = self._req("POST", "/camera/daynight", body=json.dumps({"mode": "night"}))
        self.assertEqual(status, 200)
        self.assertEqual(self.mod.current_mode, "night")
        # a Saturation=0 control was pushed
        self.assertTrue(any(c.get("Saturation") == 0.0 for c in self.hw["controls"]))
        status, body = self._req("POST", "/camera/daynight", body=json.dumps({"mode": "day"}))
        self.assertEqual(self.mod.current_mode, "day")

    def test_override_forbidden_without_control_group(self):
        import json
        status, _ = self._req("POST", "/camera/daynight", body=json.dumps({"mode": "night"}), admin=False)
        self.assertEqual(status, 403)

    def test_auto_switch_follows_lux_with_hysteresis(self):
        import json
        # back to auto
        self._req("POST", "/camera/daynight", body=json.dumps({"mode": "auto"}))
        # simulate darkness -> should flip to night within a few check cycles
        self.hw["lux"] = 2.0
        self._wait_for(lambda: self.mod.current_mode == "night", 5)
        self.assertEqual(self.mod.current_mode, "night")
        # a value inside the hysteresis band must NOT flip it back
        self.hw["lux"] = 8.0  # between NIGHT_LUX(5) and DAY_LUX(12)
        time.sleep(1.5)
        self.assertEqual(self.mod.current_mode, "night", "hysteresis band should hold mode")
        # bright -> back to day
        self.hw["lux"] = 300.0
        self._wait_for(lambda: self.mod.current_mode == "day", 5)
        self.assertEqual(self.mod.current_mode, "day")

    def test_lit_room_recovers_from_night_to_day(self):
        """Regression: stuck in grayscale while the room is clearly lit.

        A normal lit room (~230 lux) is far above DAY_LUX, so returning to auto
        from night MUST recover to colour, not stay grayscale.
        """
        import json
        # Force night, then hand control back to auto with a well-lit room.
        self._req("POST", "/camera/daynight", body=json.dumps({"mode": "night"}))
        self.assertEqual(self.mod.current_mode, "night")
        self.hw["lux"] = 230.0
        self._req("POST", "/camera/daynight", body=json.dumps({"mode": "auto"}))
        self._wait_for(lambda: self.mod.current_mode == "day", 5)
        self.assertEqual(self.mod.current_mode, "day",
                         "lit room must show colour, not stay stuck in night grayscale")

    def test_day_lux_must_exceed_night_lux(self):
        # The module guards against an overlapping/inverted band.
        self.assertGreater(self.mod.DAY_LUX, self.mod.NIGHT_LUX)

    # ---- lux-adaptive tuning ----
    def test_day_saturation_scales_with_light(self):
        m = self.mod
        dim = m.adaptive_controls(m.DAY_LUX, "day")["Saturation"]
        mid = m.adaptive_controls((m.DAY_LUX + m.CAM_BRIGHT_LUX) / 2, "day")["Saturation"]
        bright = m.adaptive_controls(m.CAM_BRIGHT_LUX * 2, "day")["Saturation"]
        self.assertLess(dim, mid)
        self.assertLess(mid, bright)
        self.assertAlmostEqual(dim, m.CAM_SAT_DIM, places=2)
        self.assertAlmostEqual(bright, m.CAM_SAT_BRIGHT, places=2)

    def test_night_ev_scales_with_darkness_and_lowers_fps(self):
        m = self.mod
        pitch = m.adaptive_controls(0.0, "night")["ExposureValue"]
        edge = m.adaptive_controls(m.NIGHT_LUX, "night")["ExposureValue"]
        self.assertGreater(pitch, edge)
        self.assertAlmostEqual(edge, 0.0, places=2)
        night = m._night_controls(1.0)
        us = night["FrameDurationLimits"][0]
        self.assertGreaterEqual(us, int(1_000_000 / m.NIGHT_MAX_FPS) - 1)
        day = m._day_controls(200.0)
        self.assertLess(day["FrameDurationLimits"][0], us, "day must run faster than night")

    def test_adaptive_applied_by_monitor_on_lux_change(self):
        import json
        self._req("POST", "/camera/daynight", body=json.dumps({"mode": "day"}))
        self.hw["lux"] = 20.0
        self._wait_for(lambda: any(
            "Saturation" in c and abs(c["Saturation"] - self.mod.CAM_SAT_DIM) < 0.1
            for c in self.hw["controls"][-6:]), 5)
        n = len(self.hw["controls"])
        self.hw["lux"] = 2000.0
        self._wait_for(lambda: any(
            "Saturation" in c and abs(c["Saturation"] - self.mod.CAM_SAT_BRIGHT) < 0.1
            for c in self.hw["controls"][n:]), 5)
        pushed = [c["Saturation"] for c in self.hw["controls"][n:] if "Saturation" in c]
        self.assertTrue(pushed and abs(pushed[-1] - self.mod.CAM_SAT_BRIGHT) < 0.1, pushed)
        self._req("POST", "/camera/daynight", body=json.dumps({"mode": "auto"}))

    def test_daynight_status_in_camera_info(self):
        import json
        _, body = self._req("GET", "/camera/info")
        dn = json.loads(body)["daynight"]
        self.assertIn(dn["mode"], ("day", "night"))
        self.assertIn("lux", dn)

    def test_index_page_has_daynight_controls(self):
        # The settings modal should render the auto/day/night buttons and wire
        # the /camera/daynight route into the page JS.
        status, body = self._req("GET", "/")
        self.assertEqual(status, 200)
        html = body.decode()
        for marker in ('id="dn-auto"', 'id="dn-day"', 'id="dn-night"',
                       'data-mode="auto"', 'data-mode="day"', 'data-mode="night"',
                       'id="servo-controls-modal"'):
            self.assertIn(marker, html, marker)

    def test_noir_tuning_file_loaded(self):
        # The active Picamera2 instance should have been created with the NoIR
        # tuning file (default CAM_TUNING_FILE=ov5647_noir.json).
        inst = self.hw["instances"][-1]
        self.assertIsNotNone(inst.tuning)
        self.assertEqual(inst.tuning.get("tuning_file"), "ov5647_noir.json")

    def _wait_for(self, pred, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return
            time.sleep(0.2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
