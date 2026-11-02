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
import re
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

# MQTT is opt-in (DOGCAM_MQTT=1/true/yes/on), so importing dogcam_stream or
# ky004-control here never reaches a broker. Tests that exercise MQTT set
# DOGCAM_MQTT=1 themselves, always together with FakePaho.

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

    def test_mqtt_is_opt_in(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOGCAM_MQTT", None)
            self.assertFalse(dogcam_mqtt.enabled(), "unset DOGCAM_MQTT must leave MQTT off")
        for value in ("1", "true", "yes", "on", "TRUE", "Yes", "ON", " 1 ", "on\n"):
            with patch.dict(os.environ, {"DOGCAM_MQTT": value}):
                self.assertTrue(dogcam_mqtt.enabled(), repr(value))
        for value in ("", " ", "0", "false", "no", "off", " 0 ", "2", "enabled", "y", "1.0", "onn"):
            with patch.dict(os.environ, {"DOGCAM_MQTT": value}):
                self.assertFalse(dogcam_mqtt.enabled(), repr(value))

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


# ---------------------------------------------------------------------------
# ky004-control.py: remote power
# ---------------------------------------------------------------------------
KY004_PATH = ROOT / "ky004-control.py"


def load_control():
    previous = sys.modules.get("lgpio")
    sys.modules["lgpio"] = types.ModuleType("lgpio")
    try:
        spec = importlib.util.spec_from_file_location("ky004_control_house_modes", KY004_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("lgpio", None)
        else:
            sys.modules["lgpio"] = previous


def loop_sleep(on_tick):
    """time.sleep stand-in that reports only the 0.1 s main-loop sleeps."""
    ticks = {"n": 0}

    def sleep(seconds):
        if seconds != 0.1:
            return
        ticks["n"] += 1
        on_tick(ticks["n"])

    return sleep


def stop_at(tick, actions=None):
    """on_tick: run actions[n] at loop tick n, raise KeyboardInterrupt at `tick`."""
    actions = actions or {}

    def on_tick(n):
        if n in actions:
            actions[n]()
        if n >= tick:
            raise KeyboardInterrupt

    return on_tick


class RemotePowerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.control = load_control()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "remote-power")
        self.ON = self.control.SWITCH_ON_VALUE
        self.OFF = 0 if self.ON else 1  # any GPIO level that is not "ON"

    def new_power(self, path=None):
        power = self.control.RemotePower(path or self.path)
        self.addCleanup(power.stop)
        return power

    def write_file(self, text):
        with open(self.path, "w") as handle:
            handle.write(text)

    def read_file(self):
        with open(self.path) as handle:
            return handle.read()

    # -- contract constants ------------------------------------------------
    def test_remote_power_file_and_state_interval_defaults(self):
        self.assertEqual(self.control.DEFAULT_REMOTE_POWER_FILE, "/var/lib/dogcam/remote-power")
        self.assertEqual(self.control.POWER_STATE_INTERVAL, 300)
        with patch.dict(os.environ, {"DOGCAM_REMOTE_POWER_FILE": self.path}):
            self.assertEqual(load_control().REMOTE_POWER_FILE, self.path)

    # -- effective state -----------------------------------------------------
    def test_effective_power_truth_table(self):
        table = {
            (True, "ON"): True,
            (True, "OFF"): False,
            (False, "ON"): False,
            (False, "OFF"): False,
        }
        for (switch_on, remote), expected in table.items():
            self.assertIs(self.control.effective_power(switch_on, remote), expected, (switch_on, remote))
        self.assertEqual(self.control.SWITCH_OFF_VALUE, self.OFF)
        self.assertEqual(self.control.effective_state(self.ON, "ON"), self.ON)
        self.assertEqual(self.control.effective_state(self.ON, "OFF"), self.OFF)
        self.assertEqual(self.control.effective_state(self.OFF, "ON"), self.OFF)
        self.assertEqual(self.control.effective_state(self.OFF, "OFF"), self.OFF)

    def test_effective_state_reads_the_current_remote_by_default(self):
        power = self.new_power()
        with patch.object(self.control, "remote_power", power):
            self.assertEqual(self.control.effective_state(self.ON), self.ON)
            power.handle_set(b"OFF")
            self.assertEqual(self.control.effective_state(self.ON), self.OFF)

    # -- payloads --------------------------------------------------------------
    def test_power_set_payloads_and_empty_default_on(self):
        power = self.new_power()
        self.assertEqual(power.remote, "ON")
        power.handle_set(b" off\n")
        self.assertEqual(power.remote, "OFF")
        power.handle_set(b"")
        self.assertEqual(power.remote, "ON", "a cleared retained message means the default ON")
        power.handle_set(b"Off")
        power.handle_set(b"On")
        self.assertEqual(power.remote, "ON")

    def test_unknown_power_payload_keeps_the_current_mode(self):
        power = self.new_power()
        power.handle_set(b"OFF")
        power.take_change()
        with self.assertLogs("dogcam_mqtt", level="WARNING"):
            power.handle_set(b"standby-unknown")
        self.assertEqual(power.remote, "OFF")
        self.assertFalse(power.take_change())

    def test_a_remote_change_is_flagged_once_for_the_main_loop(self):
        power = self.new_power()
        self.assertFalse(power.take_change())
        power.handle_set(b"OFF")
        self.assertTrue(power.take_change())
        self.assertFalse(power.take_change())
        power.handle_set(b"OFF")
        self.assertFalse(power.take_change(), "a repeated value is not a change")

    # -- persistence -----------------------------------------------------------
    def test_remote_value_is_persisted_as_one_word_atomically(self):
        power = self.new_power()
        real_replace = os.replace
        with patch.object(self.control.os, "replace", side_effect=real_replace) as replace:
            power.handle_set(b"off")
        self.assertEqual(self.read_file(), "OFF\n")
        source, target = replace.call_args.args
        self.assertEqual(target, self.path)
        self.assertEqual(os.path.dirname(source), self.dir, "temp file must be on the same filesystem")
        self.assertEqual(os.listdir(self.dir), ["remote-power"], "no temp files left behind")
        power.handle_set(b"ON")
        self.assertEqual(self.read_file(), "ON\n")

    def test_load_remote_power_reads_the_file_and_defaults_to_on(self):
        self.assertEqual(self.control.load_remote_power(self.path), "ON", "no file -> ON")
        self.write_file(" off \n")
        self.assertEqual(self.control.load_remote_power(self.path), "OFF")
        self.write_file("ON")
        self.assertEqual(self.control.load_remote_power(self.path), "ON")
        self.write_file("garbage")
        with self.assertLogs(level="WARNING"):
            self.assertEqual(self.control.load_remote_power(self.path), "ON")

    def test_undecodable_remote_power_file_means_on_with_a_warning(self):
        # SD corruption or a manual edit: invalid UTF-8 is treated like a
        # missing file instead of crashing the always-running control process.
        for content in (b"\xff\xfeOFF\n", b"\x80", b"OFF\xc3"):
            with self.subTest(content=content):
                with open(self.path, "wb") as handle:
                    handle.write(content)
                try:
                    with self.assertLogs(level="WARNING") as logs:
                        value = self.control.load_remote_power(self.path)
                except ValueError as error:
                    self.fail(f"load_remote_power crashed on {content!r}: {error!r}")
                self.assertEqual(value, "ON")
                self.assertTrue(any(self.path in line for line in logs.output), logs.output)

    def test_startup_with_an_undecodable_file_falls_back_to_on(self):
        with open(self.path, "wb") as handle:
            handle.write(b"\xff\xfeOFF\n")
        power = self.new_power()
        try:
            with patch.dict(os.environ, {"DOGCAM_MQTT": "0"}), self.assertLogs(level="WARNING"):
                link = self.control.start_remote_power(power, wait=0)
        except ValueError as error:
            self.fail(f"start_remote_power crashed on an undecodable file: {error!r}")
        self.assertIsNone(link)
        self.assertEqual(power.remote, "ON")

    def test_an_unwritable_state_file_does_not_block_the_remote_command(self):
        blocker = os.path.join(self.dir, "not-a-dir")
        with open(blocker, "w") as handle:
            handle.write("x")
        power = self.new_power(os.path.join(blocker, "remote-power"))
        with self.assertLogs(level="ERROR"):
            power.handle_set(b"OFF")
        self.assertEqual(power.remote, "OFF")
        self.assertTrue(power.take_change())

    # -- startup -----------------------------------------------------------------
    def test_mqtt_disabled_uses_the_persisted_file_and_builds_no_client(self):
        self.write_file("OFF\n")
        power = self.new_power()
        with patch.dict(os.environ, {"DOGCAM_MQTT": "0"}), fake_paho() as paho:
            link = self.control.start_remote_power(power, wait=0)
        self.assertIsNone(link)
        self.assertEqual(power.remote, "OFF")
        self.assertEqual(paho.instances, [])

    def test_mqtt_disabled_never_imports_paho(self):
        self.write_file("OFF\n")
        power = self.new_power()
        with patch.dict(os.environ, {"DOGCAM_MQTT": "0"}), blocked_paho():
            self.assertIsNone(self.control.start_remote_power(power, wait=0))
        self.assertEqual(power.remote, "OFF")

    def test_unset_dogcam_mqtt_import_and_start_build_no_client(self):
        # MQTT is opt-in: with DOGCAM_MQTT unset, neither importing the script
        # nor its __main__ start path constructs a client.
        self.write_file("OFF\n")
        power = self.new_power()
        with patch.dict(os.environ, {}), fake_paho(reachable=True) as paho:
            os.environ.pop("DOGCAM_MQTT", None)
            control = load_control()
            link = control.start_remote_power(power, wait=0)
        self.assertIsNone(link)
        self.assertEqual(paho.instances, [])
        self.assertEqual(power.remote, "OFF", "the persisted value still applies")

    def test_unreachable_broker_falls_back_to_the_persisted_file(self):
        self.write_file("OFF\n")
        power = self.new_power()
        with patch.dict(os.environ, {"DOGCAM_MQTT": "1"}), fake_paho(reachable=False) as paho:
            link = self.control.start_remote_power(power, wait=0.05)
        self.assertIsNotNone(link)
        self.assertEqual(len(paho.instances), 1)
        self.assertFalse(link.connected.is_set())
        self.assertEqual(power.remote, "OFF")

    def test_no_file_and_no_broker_means_on(self):
        power = self.new_power()
        with patch.dict(os.environ, {"DOGCAM_MQTT": "1"}), fake_paho(reachable=False):
            self.control.start_remote_power(power, wait=0.05)
        self.assertEqual(power.remote, "ON")
        self.assertFalse(os.path.exists(self.path), "nothing to persist without a broker value")

    def test_missing_paho_falls_back_to_the_persisted_file(self):
        self.write_file("OFF\n")
        power = self.new_power()
        with patch.dict(os.environ, {"DOGCAM_MQTT": "1"}), blocked_paho():
            self.assertIsNone(self.control.start_remote_power(power, wait=0))
        self.assertEqual(power.remote, "OFF")

    def test_retained_broker_value_wins_at_start_and_is_persisted(self):
        self.write_file("ON\n")
        power = self.new_power()
        started = time.monotonic()
        with patch.dict(os.environ, {"DOGCAM_MQTT": "1"}), \
                fake_paho(reachable=True, retained={POWER_SET: b"OFF"}):
            self.control.start_remote_power(power, wait=3)
        self.assertLess(time.monotonic() - started, 2, "start must not wait once the value arrived")
        self.assertEqual(power.remote, "OFF")
        self.assertEqual(self.read_file(), "OFF\n")

    def test_power_client_identity_lwt_and_online_status(self):
        power = self.new_power()
        env = {"DOGCAM_MQTT": "1"}
        with patch.dict(os.environ, env), fake_paho(reachable=True) as paho:
            os.environ.pop("DOGCAM_MQTT_HOST", None)
            os.environ.pop("DOGCAM_MQTT_PORT", None)
            os.environ.pop("DOGCAM_MQTT_POWER_CLIENT_ID", None)
            link = self.control.start_remote_power(power, wait=0)
            self.assertTrue(wait_until(link.connected.is_set))
        client = paho.instances[0]
        self.assertEqual(client.client_id, "dogo-cam-power")
        self.assertEqual(client.connect_args, ("192.168.50.211", 1883, 60))
        self.assertEqual(client.will, (STATUS, "offline", 1, True))
        self.assertLess(client.calls.index("will_set"), client.calls.index("connect_async"))
        self.assertIn((STATUS, "online", 1, True), client.published)
        self.assertIn((POWER_SET, 1), client.subscribed)

    def test_power_client_id_is_env_overridable(self):
        power = self.new_power()
        with patch.dict(os.environ, {"DOGCAM_MQTT": "1", "DOGCAM_MQTT_POWER_CLIENT_ID": "bench-power"}), \
                fake_paho() as paho:
            self.control.start_remote_power(power, wait=0)
        self.assertEqual(paho.instances[0].client_id, "bench-power")

    # -- state topic ---------------------------------------------------------------
    def test_state_json_has_exactly_the_contract_keys(self):
        power = self.new_power()
        cases = [
            (True, "ON", {"remote": "ON", "switch": "ON", "camera": "ON"}),
            (True, "OFF", {"remote": "OFF", "switch": "ON", "camera": "OFF"}),
            (False, "ON", {"remote": "ON", "switch": "OFF", "camera": "OFF"}),
            (False, "OFF", {"remote": "OFF", "switch": "OFF", "camera": "OFF"}),
        ]
        for switch_on, remote, expected in cases:
            power.set_switch(switch_on)
            power.remote = remote
            payload = power.state_payload()
            self.assertEqual(set(payload), {"remote", "switch", "camera", "t"})
            self.assertIsInstance(payload["t"], int)
            self.assertLessEqual(abs(payload["t"] - time.time()), 5)
            self.assertEqual({k: payload[k] for k in ("remote", "switch", "camera")}, expected)

    def _state_messages(self, client):
        return [
            (json.loads(payload), qos, retain)
            for topic, payload, qos, retain in list(client.published)
            if topic == POWER_STATE
        ]

    def test_state_is_published_retained_on_connect_and_on_every_change(self):
        power = self.new_power()
        power.set_switch(True)
        with patch.dict(os.environ, {"DOGCAM_MQTT": "1"}), fake_paho(reachable=True) as paho:
            self.control.start_remote_power(power, wait=0)
        client = paho.instances[0]
        self.assertTrue(wait_until(lambda: self._state_messages(client)))
        first, qos, retain = self._state_messages(client)[0]
        self.assertEqual((qos, retain), (1, True))
        self.assertEqual(set(first), {"remote", "switch", "camera", "t"})
        self.assertEqual(first["camera"], "ON")

        client.deliver(POWER_SET, b"OFF")
        self.assertTrue(wait_until(
            lambda: any(m["remote"] == "OFF" and m["camera"] == "OFF" for m, _, _ in self._state_messages(client))
        ))
        power.set_switch(False)
        self.assertTrue(wait_until(
            lambda: any(m["switch"] == "OFF" for m, _, _ in self._state_messages(client))
        ))

    def test_state_is_republished_every_interval_without_changes(self):
        power = self.new_power()
        power.set_switch(True)
        with patch.object(self.control, "POWER_STATE_INTERVAL", 0.05), \
                patch.dict(os.environ, {"DOGCAM_MQTT": "1"}), fake_paho(reachable=True) as paho:
            self.control.start_remote_power(power, wait=0)
            client = paho.instances[0]
            self.assertTrue(wait_until(lambda: len(self._state_messages(client)) >= 3))

    def test_state_is_not_published_before_the_switch_is_known(self):
        power = self.new_power()
        with patch.dict(os.environ, {"DOGCAM_MQTT": "1"}), fake_paho(reachable=True) as paho:
            link = self.control.start_remote_power(power, wait=0)
            self.assertTrue(wait_until(link.connected.is_set))
        time.sleep(0.05)
        self.assertEqual(self._state_messages(paho.instances[0]), [])

    # -- main loops ----------------------------------------------------------------
    def _run_always_on(self, power, moments, on_tick):
        applied = []
        fake_time = types.SimpleNamespace(time=moments, sleep=loop_sleep(on_tick))
        with patch.multiple(
            self.control,
            remote_power=power,
            time=fake_time,
            apply_state=lambda state, force_led=False: applied.append((state, force_led)),
        ):
            self.control.run_always_on()
        return applied

    def test_always_on_mode_follows_the_remote_and_reconcile_keeps_it_off(self):
        power = self.new_power()
        power.handle_set(b"OFF")
        moments = iter([0.0, 6.0, 6.0])
        applied = self._run_always_on(power, lambda: next(moments), stop_at(1))
        self.assertEqual(applied, [(self.OFF, True), (self.OFF, False)])
        self.assertTrue(power.switch_on, "always-on mode reports the switch as ON")

    def test_always_on_mode_applies_a_remote_change_at_once(self):
        power = self.new_power()
        actions = {1: lambda: power.handle_set(b"OFF"), 2: lambda: power.handle_set(b"ON")}
        applied = self._run_always_on(power, lambda: 0.0, stop_at(3, actions))
        self.assertEqual(applied, [(self.ON, True), (self.OFF, True), (self.ON, True)])

    def _run_gpio(self, power, switch_value, on_tick, clock=None):
        applied = []
        closed = []

        class FakeGPIO:
            SET_PULL_UP = 32
            SET_PULL_DOWN = 64

            def gpiochip_open(self, _chip):
                return 3

            def gpio_read(self, _handle, _pin):
                return switch_value()

            def gpiochip_close(self, handle):
                closed.append(handle)

        fake_time = types.SimpleNamespace(time=clock or (lambda: 0.0), sleep=loop_sleep(on_tick))
        with patch.multiple(
            self.control,
            remote_power=power,
            lgpio=FakeGPIO(),
            SWITCH_MODE="gpio",
            read_pull_value=lambda *_args, **_kwargs: 0,
            time=fake_time,
            apply_state=lambda state, force_led=False: applied.append((state, force_led)),
        ):
            self.control.main()
        self.assertEqual(closed, [3])
        return applied

    def test_gpio_switch_on_with_remote_off_stays_off_through_the_reconcile(self):
        power = self.new_power()
        power.handle_set(b"OFF")
        now = {"t": 0.0}

        def advance():
            now["t"] += 6.0

        applied = self._run_gpio(
            power, lambda: self.ON, stop_at(2, {1: advance}), clock=lambda: now["t"]
        )
        self.assertEqual(applied, [(self.OFF, True), (self.OFF, False)])
        self.assertTrue(power.switch_on)

    def test_gpio_remote_change_is_applied_before_the_next_reconcile(self):
        power = self.new_power()
        actions = {1: lambda: power.handle_set(b"OFF"), 2: lambda: power.handle_set(b"")}
        applied = self._run_gpio(power, lambda: self.ON, stop_at(3, actions))
        self.assertEqual(applied, [(self.ON, True), (self.OFF, True), (self.ON, True)])

    def test_gpio_switch_off_keeps_the_camera_off_whatever_the_remote(self):
        power = self.new_power()
        applied = self._run_gpio(power, lambda: self.OFF, stop_at(2, {1: lambda: power.handle_set(b"ON")}))
        self.assertEqual(applied, [(self.OFF, True)])
        self.assertFalse(power.switch_on)

    def test_led_shows_the_effective_state(self):
        calls = []
        with patch.multiple(
            self.control,
            site_up=lambda: False,
            service_state=lambda _name: "inactive",
            led_on=lambda: calls.append("led_on"),
            led_off=lambda: calls.append("led_off"),
            start=lambda: calls.append("start"),
            stop=lambda: calls.append("stop"),
        ):
            self.control.apply_state(self.control.effective_state(self.ON, "OFF"), force_led=True)
        self.assertEqual(calls, ["led_off"], "switch ON + remote OFF must show OFF and start nothing")

    def test_main_itself_never_opens_mqtt_only_the_script_entry_does(self):
        # start_remote_power runs from the __main__ block only, so importing the
        # module or calling main() (as test_switch_mode does) never opens MQTT.
        with patch.dict(os.environ, {"DOGCAM_MQTT": "1"}), fake_paho(reachable=True) as paho, \
                patch.multiple(self.control, SWITCH_MODE="always-on", run_always_on=lambda: None):
            self.control.main()
        self.assertEqual(paho.instances, [])
        main_block = KY004_PATH.read_text().split('if __name__ == "__main__":', 1)[1]
        self.assertLess(main_block.index("start_remote_power()"), main_block.index("main()"))


# ---------------------------------------------------------------------------
# dogcam_stream.py: lux metering
# ---------------------------------------------------------------------------
from test_demand_streaming import _install_hardware_stubs  # noqa: E402

STREAM_ENV = {
    "TRUST_PROXY_AUTH_HEADERS": "1",
    "SECRET_KEY": "test",
    "DAYNIGHT_AUTO": "0",
    "STREAM_FRAME_TIMEOUT": "0.2",
    "STREAM_IDLE_TIMEOUT": "0.05",
    "STREAM_STALL_RESTART_AFTER": "60",
    "STREAM_STALL_CHECK_INTERVAL": "60",
}


def import_stream():
    """Fresh dogcam_stream import on stub camera libs (as test_demand_streaming)."""
    hw = _install_hardware_stubs()
    for name in ("dogcam_stream", "servo_control_rpigpio"):
        sys.modules.pop(name, None)
    import dogcam_stream

    return dogcam_stream, hw


def thread_names():
    return [thread.name for thread in threading.enumerate()]


class FakeLink:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=True):
        self.published.append((topic, payload, retain))
        return True

    def lux_messages(self):
        return [json.loads(payload) for topic, payload, _retain in list(self.published) if topic == LUX]


class LuxMeterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environ = patch.dict(os.environ, dict(STREAM_ENV, DOGCAM_MQTT="0"))
        cls.environ.start()
        os.environ.pop("DOGCAM_METER_INTERVAL", None)
        lux_threads_before = thread_names().count("lux-meter")
        with fake_paho(reachable=True) as paho:
            cls.mod, cls.hw = import_stream()
        cls.paho = paho
        # getattr: keep the fixture independent of the API under test.
        cls.link_at_import = getattr(cls.mod, "mqtt_link", "<missing>")
        cls.meter_thread_at_import = getattr(cls.mod, "meter_thread", "<missing>")
        cls.new_lux_threads = thread_names().count("lux-meter") - lux_threads_before

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
        if hasattr(m, "meter_enabled"):
            m.meter_enabled.clear()
        m.last_lux = None
        m.last_lux_at = None
        self.link = FakeLink()
        patcher = patch.object(m, "mqtt_link", self.link, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def demand_spies(self):
        m = self.mod
        acquire = patch.object(m, "acquire_stream_demand", wraps=m.acquire_stream_demand)
        release = patch.object(m, "release_stream_demand", wraps=m.release_stream_demand)
        return acquire, release

    def metadata(self, **kwargs):
        return patch.object(self.mod.camera, "capture_metadata", **kwargs)

    # -- contract constants and import behaviour ---------------------------
    def test_meter_defaults(self):
        self.assertEqual(self.mod.METER_INTERVAL, 60)
        self.assertEqual(self.mod.METER_FRESH_SECONDS, 15)
        self.assertEqual(self.mod.METER_SAMPLE_TIMEOUT, 5)
        self.assertFalse(self.mod.meter_enabled.is_set(), "metering is OFF until meter/set says ON")

    def test_mqtt_disabled_import_builds_no_client_and_starts_no_thread(self):
        self.assertEqual(self.paho.instances, [])
        self.assertIsNone(self.link_at_import)
        self.assertIsNone(self.meter_thread_at_import)
        self.assertEqual(self.new_lux_threads, 0)

    def test_no_new_http_routes(self):
        rules = [rule.rule for rule in self.mod.app.url_map.iter_rules()]
        for word in ("lux", "meter", "mqtt", "power"):
            self.assertFalse([rule for rule in rules if word in rule.lower()], word)

    # -- meter/set ------------------------------------------------------------
    def test_meter_set_payloads_and_empty_default_off(self):
        m = self.mod
        m.on_meter_set(b" on \n")
        self.assertTrue(m.meter_enabled.is_set())
        m.on_meter_set(b"")
        self.assertFalse(m.meter_enabled.is_set(), "a cleared retained message means the default OFF")
        m.on_meter_set(b"On")
        with self.assertLogs("dogcam_mqtt", level="WARNING"):
            m.on_meter_set(b"sometimes-meter")
        self.assertTrue(m.meter_enabled.is_set(), "unknown payload keeps the current mode")
        m.on_meter_set(b"OFF")
        self.assertFalse(m.meter_enabled.is_set())

    # -- sampling ---------------------------------------------------------------
    def test_meter_off_takes_no_sample(self):
        acquire, release = self.demand_spies()
        with acquire as acquired, release, self.metadata(return_value={"Lux": 10.0}) as capture:
            self.assertIsNone(self.mod.meter_tick())
        acquired.assert_not_called()
        capture.assert_not_called()
        self.assertEqual(self.hw["starts"], 0)
        self.assertEqual(self.link.published, [])

    def test_idle_camera_samples_through_a_released_snapshot_demand(self):
        self.mod.on_meter_set(b"ON")
        acquire, release = self.demand_spies()
        with acquire as acquired, release as released, self.metadata(return_value={"Lux": 123.44}):
            payload = self.mod.meter_tick()
        acquired.assert_called_once_with("snapshot")
        released.assert_called_once_with("snapshot")
        self.assertEqual(self.mod.snapshot_demands, 0)
        self.assertEqual(self.hw["starts"], 1, "an idle camera is started for the sample")
        self.assertEqual(set(payload), {"lux", "t", "source"})
        self.assertEqual(payload["lux"], 123.4)
        self.assertEqual(payload["source"], "sample")
        self.assertIsInstance(payload["t"], int)
        self.assertLessEqual(abs(payload["t"] - time.time()), 5)
        self.assertEqual(self.link.published, [(LUX, json.dumps(payload), True)])

    def test_sample_timeout_releases_the_demand_and_publishes_nothing(self):
        self.mod.on_meter_set(b"ON")
        acquire, release = self.demand_spies()
        started = time.monotonic()
        with patch.object(self.mod, "METER_SAMPLE_TIMEOUT", 0.2), acquire as acquired, \
                release as released, self.metadata(return_value={"ExposureTime": 1000}):
            self.assertIsNone(self.mod.meter_tick())
        self.assertLess(time.monotonic() - started, 1.5)
        acquired.assert_called_once_with("snapshot")
        released.assert_called_once_with("snapshot")
        self.assertEqual(self.mod.snapshot_demands, 0)
        self.assertEqual(self.link.published, [])

    def test_metadata_errors_release_the_demand(self):
        self.mod.on_meter_set(b"ON")
        acquire, release = self.demand_spies()
        with patch.object(self.mod, "METER_SAMPLE_TIMEOUT", 0.2), acquire, release as released, \
                self.metadata(side_effect=RuntimeError("Camera frontend has timed out")):
            self.assertIsNone(self.mod.meter_tick())
        released.assert_called_once_with("snapshot")
        self.assertEqual(self.mod.snapshot_demands, 0)
        self.assertEqual(self.link.published, [])

    def test_an_exception_while_sampling_still_releases_the_demand(self):
        self.mod.on_meter_set(b"ON")
        acquire, release = self.demand_spies()
        with acquire, release as released, \
                patch.object(self.mod, "_wait_for_metadata_lux", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.mod.meter_tick())
        released.assert_called_once_with("snapshot")
        self.assertEqual(self.mod.snapshot_demands, 0)
        self.assertEqual(self.link.published, [])

    def test_a_hung_metadata_read_is_bounded_and_released(self):
        self.mod.on_meter_set(b"ON")
        gate = threading.Event()

        def hang():
            gate.wait(5)
            return {"Lux": 1.0}

        acquire, release = self.demand_spies()
        started = time.monotonic()
        try:
            with patch.object(self.mod, "METER_SAMPLE_TIMEOUT", 0.2), acquire, release as released, \
                    self.metadata(side_effect=hang):
                self.assertIsNone(self.mod.meter_tick())
                elapsed = time.monotonic() - started
        finally:
            gate.set()
            reader = getattr(self.mod, "_lux_reader", None)
            if reader is not None:
                reader.join(2)
        self.assertLess(elapsed, 1.5, "the 5 s wait bound must hold even if capture_metadata hangs")
        released.assert_called_once_with("snapshot")
        self.assertEqual(self.mod.snapshot_demands, 0)

    def test_running_camera_with_fresh_lux_publishes_stream_without_extra_demand(self):
        m = self.mod
        lease = m.acquire_stream_demand("viewer")
        self.addCleanup(lease.release)
        with self.metadata(return_value={"Lux": 42.04}):
            self.assertEqual(m.read_lux(), 42.04)  # the day/night monitor's metadata path
        m.on_meter_set(b"ON")
        acquire, release = self.demand_spies()
        with acquire as acquired, release:
            payload = m.meter_tick()
        acquired.assert_not_called()
        self.assertEqual(m.snapshot_demands, 0)
        self.assertEqual(self.hw["starts"], 1, "only the viewer started the camera")
        self.assertEqual(payload["source"], "stream")
        self.assertEqual(payload["lux"], 42.0)
        self.assertEqual(self.link.lux_messages(), [payload])

    def test_running_camera_with_stale_lux_takes_a_sample(self):
        m = self.mod
        lease = m.acquire_stream_demand("viewer")
        self.addCleanup(lease.release)
        m.last_lux = 42.0
        m.last_lux_at = time.monotonic() - 16
        m.on_meter_set(b"ON")
        acquire, release = self.demand_spies()
        with acquire as acquired, release as released, self.metadata(return_value={"Lux": 7.25}):
            payload = m.meter_tick()
        acquired.assert_called_once_with("snapshot")
        released.assert_called_once_with("snapshot")
        self.assertEqual(payload["source"], "sample")
        self.assertEqual(payload["lux"], 7.2)
        self.assertEqual(self.hw["starts"], 1, "the camera was already running")
        self.assertEqual(m.active_viewers, 1)

    def test_a_recent_reading_from_a_stopped_camera_is_not_reused(self):
        m = self.mod
        m.last_lux = 42.0
        m.last_lux_at = time.monotonic()
        m.on_meter_set(b"ON")
        acquire, release = self.demand_spies()
        with acquire as acquired, release, self.metadata(return_value={"Lux": 3.0}):
            payload = m.meter_tick()
        acquired.assert_called_once_with("snapshot")
        self.assertEqual((payload["lux"], payload["source"]), (3.0, "sample"))

    def test_unavailable_camera_publishes_nothing(self):
        self.mod.on_meter_set(b"ON")
        with patch.object(self.mod, "camera_available", False):
            self.assertIsNone(self.mod.meter_tick())
        self.assertEqual(self.mod.snapshot_demands, 0)
        self.assertEqual(self.link.published, [])

    # -- the metering thread ----------------------------------------------------
    def run_meter_loop(self):
        m = self.mod
        m._meter_stop.clear()
        thread = threading.Thread(target=m.monitor_meter, daemon=True)
        thread.start()

        def stop():
            m._meter_stop.set()
            m._meter_wake.set()
            thread.join(2)
            m._meter_stop.clear()
            m._meter_wake.clear()

        self.addCleanup(stop)
        return thread

    def test_meter_loop_samples_at_least_every_interval_only_while_on(self):
        m = self.mod
        with patch.object(m, "METER_INTERVAL", 0.05):
            self.run_meter_loop()
            time.sleep(0.25)
            self.assertEqual(self.link.published, [], "metering OFF: no sampling")
            self.assertEqual(self.hw["starts"], 0)
            m.on_meter_set(b"ON")
            self.assertTrue(wait_until(lambda: len(self.link.lux_messages()) >= 3))
            m.on_meter_set(b"OFF")
            time.sleep(0.1)
            settled = len(self.link.published)
            time.sleep(0.25)
            self.assertEqual(len(self.link.published), settled, "metering OFF stops sampling")
        self.assertEqual(m.snapshot_demands, 0)

    def test_meter_on_wakes_the_loop_immediately(self):
        m = self.mod
        with patch.object(m, "METER_INTERVAL", 60):
            self.run_meter_loop()
            time.sleep(0.05)
            m.on_meter_set(b"ON")
            self.assertTrue(wait_until(lambda: self.link.lux_messages(), timeout=2))


class StreamMqttStartupTest(unittest.TestCase):
    """Importing dogcam_stream with MQTT on (fake paho) and off (no paho)."""

    def setUp(self):
        environ = patch.dict(os.environ, STREAM_ENV)
        environ.start()
        self.addCleanup(environ.stop)

    def test_mqtt_off_needs_no_paho_and_starts_nothing(self):
        before = thread_names().count("lux-meter")
        with patch.dict(os.environ, {"DOGCAM_MQTT": "0"}), blocked_paho():
            mod, _hw = import_stream()
        self.addCleanup(mod.cleanup)
        self.assertIsNone(mod.mqtt_link)
        self.assertIsNone(mod.meter_thread)
        self.assertEqual(thread_names().count("lux-meter"), before)

    def test_unset_dogcam_mqtt_import_builds_no_client_and_starts_no_thread(self):
        before = thread_names().count("lux-meter")
        with patch.dict(os.environ, {}), fake_paho(reachable=True) as paho:
            os.environ.pop("DOGCAM_MQTT", None)
            mod, _hw = import_stream()
            self.addCleanup(mod.cleanup)
        self.assertEqual(paho.instances, [], "MQTT is opt-in: no client without DOGCAM_MQTT=1")
        self.assertIsNone(mod.mqtt_link)
        self.assertIsNone(mod.meter_thread)
        self.assertEqual(thread_names().count("lux-meter"), before)

    def test_mqtt_on_builds_one_stream_client_and_the_meter_thread(self):
        env = {"DOGCAM_MQTT": "1", "DOGCAM_METER_INTERVAL": "7"}
        with patch.dict(os.environ, env), fake_paho(reachable=False) as paho:
            for name in ("DOGCAM_MQTT_HOST", "DOGCAM_MQTT_PORT", "DOGCAM_MQTT_STREAM_CLIENT_ID"):
                os.environ.pop(name, None)
            mod, hw = import_stream()
            self.addCleanup(mod.cleanup)
            self.assertIs(mod.start_mqtt(), mod.mqtt_link, "one MQTT client per process")
        self.assertEqual(mod.METER_INTERVAL, 7)
        self.assertEqual(len(paho.instances), 1)
        client = paho.instances[0]
        self.assertEqual(client.client_id, "dogo-cam-stream")
        self.assertEqual(client.connect_args, ("192.168.50.211", 1883, 60))
        self.assertIsNone(client.will, "status/LWT belongs to the ky004-control client")
        self.assertTrue(mod.meter_thread.is_alive())
        self.assertTrue(mod.meter_thread.daemon)

        client.fire_connect()
        self.assertEqual(client.subscribed, [(METER_SET, 1)])
        self.assertFalse([p for p in client.published if p[0] == STATUS])

        client.deliver(METER_SET, b"ON")
        self.assertTrue(mod.meter_enabled.is_set())
        self.assertTrue(wait_until(lambda: any(p[0] == LUX for p in client.published)))
        topic, payload, qos, retain = next(p for p in client.published if p[0] == LUX)
        self.assertEqual((qos, retain), (1, True))
        message = json.loads(payload)
        self.assertEqual(set(message), {"lux", "t", "source"})
        self.assertEqual(message["source"], "sample")
        self.assertTrue(wait_until(lambda: mod.snapshot_demands == 0))

        mod.cleanup()
        mod.meter_thread.join(2)
        self.assertFalse(mod.meter_thread.is_alive(), "cleanup stops the meter thread")
        self.assertTrue(client.disconnected)

    def test_stream_client_id_is_env_overridable(self):
        env = {"DOGCAM_MQTT": "1", "DOGCAM_MQTT_STREAM_CLIENT_ID": "bench-stream"}
        with patch.dict(os.environ, env), fake_paho() as paho:
            mod, _hw = import_stream()
            self.addCleanup(mod.cleanup)
        self.assertEqual([c.client_id for c in paho.instances], ["bench-stream"])


class DocsAndRequirementsTest(unittest.TestCase):
    def test_requirements_pin_paho_mqtt(self):
        lines = [line.strip() for line in (ROOT / "requirements.txt").read_text().splitlines()]
        self.assertIn("paho-mqtt==2.1.0", lines)

    def test_pyproject_depends_on_paho_mqtt(self):
        import tomllib

        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertIn("paho-mqtt==2.1.0", project["dependencies"])

    def test_readme_says_ky004_runs_from_the_project_venv(self):
        # The deployed button-control.service runs <checkout>/.venv/bin/python
        # ky004-control.py, so paho comes from the venv, not the system python3.
        readme = (ROOT / "README.md").read_text()
        self.assertNotIn("runs under the system `python3`", readme)
        paragraphs = [p for p in readme.split("\n\n") if "paho-mqtt" in p and "ky004-control.py" in p]
        self.assertTrue(paragraphs, "README has no paragraph on installing paho-mqtt for ky004-control.py")
        self.assertTrue(any(".venv/bin/python" in p for p in paragraphs), paragraphs)

    def test_readme_documents_topics_env_and_the_remote_power_file(self):
        readme = (ROOT / "README.md").read_text()
        for needle in (
            POWER_SET, POWER_STATE, STATUS, METER_SET, LUX,
            "DOGCAM_MQTT=1", "DOGCAM_MQTT_HOST", "DOGCAM_MQTT_PORT",
            "DOGCAM_MQTT_POWER_CLIENT_ID", "DOGCAM_MQTT_STREAM_CLIENT_ID",
            "DOGCAM_REMOTE_POWER_FILE", "/var/lib/dogcam/remote-power", "DOGCAM_METER_INTERVAL",
            "dogo-cam-power", "dogo-cam-stream", '"source"',
        ):
            self.assertTrue(needle in readme, f"README does not mention {needle}")

    def test_readme_documents_mqtt_as_opt_in(self):
        readme = (ROOT / "README.md").read_text()
        rows = [line for line in readme.splitlines() if line.startswith("| `DOGCAM_MQTT` |")]
        self.assertEqual(len(rows), 1, rows)
        default = rows[0].strip("|").split("|")[1].strip()
        self.assertNotIn(default, ("`1`", "1"), "DOGCAM_MQTT is not on by default")
        for value in ("`1`", "`true`", "`yes`", "`on`"):
            self.assertIn(value, rows[0])
        self.assertIn("/etc/dogo-cam.env", readme, "README must say where production turns MQTT on")

    def test_env_example_leaves_mqtt_off(self):
        # A checkout's .env is loaded by load_dotenv() on import, so a copied
        # example must not switch MQTT on for tests or a bench run.
        values = re.findall(r"^DOGCAM_MQTT=(.*)$", (ROOT / ".env.example").read_text(), re.MULTILINE)
        self.assertEqual(len(values), 1, values)
        with patch.dict(os.environ, {"DOGCAM_MQTT": values[0].split("#")[0].strip()}):
            self.assertFalse(dogcam_mqtt.enabled())


if __name__ == "__main__":
    unittest.main(verbosity=2)
