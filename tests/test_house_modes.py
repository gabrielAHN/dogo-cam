"""House modes: dogo-cam remote power (ky004-control) and lux metering (dogcam_stream).

Wire contract: jg-casa/dogo-cam/{power/set, power/state, status, meter/set, lux}.
Every test runs without a broker and without the real paho client: MQTT is
exercised through FakePaho, installed under the real import names so the lazy
``import paho.mqtt.client`` path in dogcam_mqtt is what gets tested.
"""

import contextlib
import importlib.util
import json
import logging
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Safety: the whole discover run imports dogcam_stream (and may run
# ky004-control helpers) many times. MQTT is on by default in production, so
# without this every such import would try to reach the real broker. Discovery
# imports every test module before any test runs, so this covers the suite.
os.environ["DOGCAM_MQTT"] = "0"

import dogcam_mqtt  # noqa: E402

POWER_SET = "jg-casa/dogo-cam/power/set"
POWER_STATE = "jg-casa/dogo-cam/power/state"
STATUS = "jg-casa/dogo-cam/status"
METER_SET = "jg-casa/dogo-cam/meter/set"
LUX = "jg-casa/dogo-cam/lux"

_MISSING = object()
_PAHO_KEYS = ("paho", "paho.mqtt", "paho.mqtt.client")


class FakeReasonCode:
    def __init__(self, value=0):
        self.value = value
        self.is_failure = value != 0

    def __repr__(self):
        return f"FakeReasonCode({self.value})"


def make_fake_paho(reachable=False, retained=None, v2=True):
    """A stand-in for paho.mqtt.client that records calls and models a broker.

    reachable=True: loop_start() "connects" on a background thread, and every
    subscribe() immediately delivers the broker's retained message for that
    topic (if any), the way mosquitto does.
    """
    module = types.ModuleType("paho.mqtt.client")
    module.instances = []
    module.reachable = reachable
    module.retained = dict(retained or {})
    if v2:
        module.CallbackAPIVersion = types.SimpleNamespace(VERSION1="v1", VERSION2="v2")

    class Client:
        def __init__(self, callback_api_version=None, client_id="", **_kwargs):
            self.callback_api_version = callback_api_version
            self.client_id = client_id
            self.calls = []
            self.will = None
            self.connect_args = None
            self.subscribed = []
            self.published = []
            self.on_connect = None
            self.on_disconnect = None
            self.on_message = None
            self.loop_started = False
            self.disconnected = False
            module.instances.append(self)

        def will_set(self, topic, payload=None, qos=0, retain=False):
            self.calls.append("will_set")
            self.will = (topic, payload, qos, retain)

        def reconnect_delay_set(self, min_delay=1, max_delay=120):
            self.calls.append("reconnect_delay_set")

        def connect_async(self, host, port=1883, keepalive=60, **_kwargs):
            self.calls.append("connect_async")
            self.connect_args = (host, port, keepalive)

        def loop_start(self):
            self.calls.append("loop_start")
            self.loop_started = True
            if module.reachable:
                threading.Thread(target=self.fire_connect, daemon=True).start()

        def loop_stop(self):
            self.calls.append("loop_stop")
            self.loop_started = False

        def disconnect(self):
            self.calls.append("disconnect")
            self.disconnected = True

        def subscribe(self, topic, qos=0):
            self.subscribed.append((topic, qos))
            if topic in module.retained:
                self.deliver(topic, module.retained[topic])

        def publish(self, topic, payload=None, qos=0, retain=False):
            self.published.append((topic, payload, qos, retain))

        # -- test helpers -------------------------------------------------
        def fire_connect(self, rc=0):
            if v2:
                self.on_connect(self, None, {}, FakeReasonCode(rc), None)
            else:
                self.on_connect(self, None, {}, rc)

        def deliver(self, topic, payload, retain=True):
            message = types.SimpleNamespace(topic=topic, payload=payload, retain=retain, qos=1)
            self.on_message(self, None, message)

    module.Client = Client
    return module


@contextlib.contextmanager
def _paho_modules(client_module):
    saved = {key: sys.modules.get(key, _MISSING) for key in _PAHO_KEYS}
    if client_module is None:
        replacement = {key: None for key in _PAHO_KEYS}  # import raises ImportError
    else:
        paho = types.ModuleType("paho")
        mqtt_pkg = types.ModuleType("paho.mqtt")
        paho.mqtt = mqtt_pkg
        mqtt_pkg.client = client_module
        replacement = {"paho": paho, "paho.mqtt": mqtt_pkg, "paho.mqtt.client": client_module}
    sys.modules.update(replacement)
    try:
        yield client_module
    finally:
        for key, value in saved.items():
            if value is _MISSING:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


def fake_paho(**kwargs):
    return _paho_modules(make_fake_paho(**kwargs))


def blocked_paho():
    """paho is not installed: any import of it raises ImportError."""
    return _paho_modules(None)


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ---------------------------------------------------------------------------
# Shared MQTT helper
# ---------------------------------------------------------------------------
class SharedMqttHelperTest(unittest.TestCase):
    def test_topics_defaults_and_client_ids_match_the_contract(self):
        self.assertEqual(dogcam_mqtt.POWER_SET_TOPIC, POWER_SET)
        self.assertEqual(dogcam_mqtt.POWER_STATE_TOPIC, POWER_STATE)
        self.assertEqual(dogcam_mqtt.STATUS_TOPIC, STATUS)
        self.assertEqual(dogcam_mqtt.METER_SET_TOPIC, METER_SET)
        self.assertEqual(dogcam_mqtt.LUX_TOPIC, LUX)
        self.assertEqual(dogcam_mqtt.POWER_DEFAULT, "ON")
        self.assertEqual(dogcam_mqtt.METER_DEFAULT, "OFF")
        self.assertEqual(dogcam_mqtt.POWER_CLIENT_ID, "dogo-cam-power")
        self.assertEqual(dogcam_mqtt.STREAM_CLIENT_ID, "dogo-cam-stream")

    def test_on_off_payloads_are_stripped_and_case_insensitive(self):
        for payload in (b"ON", b"on", b" On\n", "on ", bytearray(b"oN")):
            self.assertEqual(dogcam_mqtt.parse_on_off(payload, "OFF", POWER_SET), "ON", payload)
        for payload in (b"OFF", b"off", b"\toFf \r\n", "OFF"):
            self.assertEqual(dogcam_mqtt.parse_on_off(payload, "ON", POWER_SET), "OFF", payload)

    def test_empty_payload_means_the_topic_default(self):
        for payload in (b"", b"   ", "", None):
            self.assertEqual(dogcam_mqtt.parse_on_off(payload, "ON", POWER_SET), "ON")
            self.assertEqual(dogcam_mqtt.parse_on_off(payload, "OFF", METER_SET), "OFF")

    def test_unknown_payload_is_ignored_and_logged_once(self):
        with self.assertLogs("dogcam_mqtt", level="WARNING") as logs:
            self.assertIsNone(dogcam_mqtt.parse_on_off(b"toggle-once", "ON", POWER_SET))
            self.assertIsNone(dogcam_mqtt.parse_on_off(b" TOGGLE-ONCE ", "ON", POWER_SET))
            # keep assertLogs satisfied even if a regression logs nothing
            logging.getLogger("dogcam_mqtt").warning("sentinel")
        mentions = [line for line in logs.output if "toggle-once" in line.lower()]
        self.assertEqual(len(mentions), 1, logs.output)

    def test_mqtt_is_enabled_unless_dogcam_mqtt_is_zero(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOGCAM_MQTT", None)
            self.assertTrue(dogcam_mqtt.enabled())
        for value in ("1", "yes", "on", "true"):
            with patch.dict(os.environ, {"DOGCAM_MQTT": value}):
                self.assertTrue(dogcam_mqtt.enabled(), value)
        for value in ("0", "false", "no", "off", " 0 "):
            with patch.dict(os.environ, {"DOGCAM_MQTT": value}):
                self.assertFalse(dogcam_mqtt.enabled(), value)

    def test_broker_host_and_port_come_from_env_with_contract_defaults(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOGCAM_MQTT_HOST", None)
            os.environ.pop("DOGCAM_MQTT_PORT", None)
            self.assertEqual(dogcam_mqtt.broker(), ("192.168.50.211", 1883))
        with patch.dict(os.environ, {"DOGCAM_MQTT_HOST": "10.1.2.3", "DOGCAM_MQTT_PORT": "1884"}):
            self.assertEqual(dogcam_mqtt.broker(), ("10.1.2.3", 1884))
        with patch.dict(os.environ, {"DOGCAM_MQTT_HOST": "", "DOGCAM_MQTT_PORT": "nope"}):
            self.assertEqual(dogcam_mqtt.broker(), ("192.168.50.211", 1883))

    def test_client_ids_are_env_overridable(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOGCAM_MQTT_POWER_CLIENT_ID", None)
            self.assertEqual(
                dogcam_mqtt.client_id(dogcam_mqtt.POWER_CLIENT_ID_ENV, dogcam_mqtt.POWER_CLIENT_ID),
                "dogo-cam-power",
            )
        with patch.dict(os.environ, {"DOGCAM_MQTT_STREAM_CLIENT_ID": "bench-stream"}):
            self.assertEqual(
                dogcam_mqtt.client_id(dogcam_mqtt.STREAM_CLIENT_ID_ENV, dogcam_mqtt.STREAM_CLIENT_ID),
                "bench-stream",
            )

    def test_link_sets_lwt_before_connecting_and_announces_online(self):
        received = []
        connected = []
        with patch.dict(os.environ, {"DOGCAM_MQTT_HOST": "10.9.8.7", "DOGCAM_MQTT_PORT": "1999"}), \
                fake_paho() as paho:
            link = dogcam_mqtt.MqttLink(
                "dogo-cam-power",
                subscriptions={POWER_SET: received.append},
                will=(STATUS, "offline"),
                online=(STATUS, "online"),
                on_connect=lambda: connected.append(True),
            )
            self.assertTrue(link.start())
            self.assertTrue(link.start(), "a second start must reuse the client")
        self.assertEqual(len(paho.instances), 1)
        client = paho.instances[0]
        self.assertEqual(client.client_id, "dogo-cam-power")
        self.assertEqual(client.callback_api_version, "v2")
        self.assertEqual(client.will, (STATUS, "offline", 1, True))
        self.assertLess(client.calls.index("will_set"), client.calls.index("connect_async"))
        self.assertEqual(client.connect_args, ("10.9.8.7", 1999, 60))
        self.assertTrue(client.loop_started)
        self.assertEqual(client.published, [], "nothing is published before the broker accepts us")

        client.fire_connect()
        self.assertTrue(link.connected.is_set())
        self.assertIn((STATUS, "online", 1, True), client.published)
        self.assertEqual(client.subscribed, [(POWER_SET, 1)])
        self.assertEqual(connected, [True])

        client.deliver(POWER_SET, b"OFF")
        client.deliver("jg-casa/other", b"OFF")
        self.assertEqual(received, [b"OFF"])

    def test_refused_connect_does_not_subscribe_or_announce(self):
        with fake_paho() as paho:
            link = dogcam_mqtt.MqttLink("x", subscriptions={POWER_SET: print}, online=(STATUS, "online"))
            link.start()
        client = paho.instances[0]
        client.fire_connect(rc=5)
        self.assertFalse(link.connected.is_set())
        self.assertEqual(client.subscribed, [])
        self.assertEqual(client.published, [])

    def test_link_without_will_never_calls_will_set(self):
        with fake_paho() as paho:
            dogcam_mqtt.MqttLink("dogo-cam-stream", subscriptions={METER_SET: print}).start()
        self.assertIsNone(paho.instances[0].will)

    def test_publish_is_retained_qos1_and_a_noop_before_start(self):
        link = dogcam_mqtt.MqttLink("x")
        self.assertFalse(link.publish(LUX, "{}"))
        with fake_paho() as paho:
            link.start()
        self.assertTrue(link.publish(LUX, '{"lux": 1.0}'))
        self.assertEqual(paho.instances[0].published, [(LUX, '{"lux": 1.0}', 1, True)])

    def test_handler_errors_do_not_escape_into_the_network_thread(self):
        def broken(_payload):
            raise RuntimeError("boom")

        with fake_paho() as paho:
            dogcam_mqtt.MqttLink("x", subscriptions={METER_SET: broken}).start()
        with self.assertLogs("dogcam_mqtt", level="ERROR"):
            paho.instances[0].deliver(METER_SET, b"ON")

    def test_missing_paho_disables_mqtt_without_raising(self):
        with blocked_paho(), self.assertLogs("dogcam_mqtt", level="WARNING"):
            link = dogcam_mqtt.MqttLink("x")
            self.assertFalse(link.start())
        self.assertIsNone(link.client)

    def test_paho_1x_clients_are_supported(self):
        with fake_paho(v2=False) as paho:
            link = dogcam_mqtt.MqttLink("x", subscriptions={POWER_SET: print}, online=(STATUS, "online"))
            self.assertTrue(link.start())
        client = paho.instances[0]
        self.assertIsNone(client.callback_api_version)
        client.fire_connect(rc=0)
        self.assertTrue(link.connected.is_set())
        self.assertEqual(client.subscribed, [(POWER_SET, 1)])

    def test_stop_disconnects_and_stops_the_loop(self):
        with fake_paho() as paho:
            link = dogcam_mqtt.MqttLink("x")
            link.start()
        client = paho.instances[0]
        link.stop()
        self.assertTrue(client.disconnected)
        self.assertFalse(client.loop_started)
        self.assertIsNone(link.client)
        self.assertFalse(link.publish(LUX, "{}"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
