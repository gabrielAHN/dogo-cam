"""Shared MQTT plumbing for dogo-cam house modes.

One small helper used by both long-running processes:

* ``ky004-control.py`` (client id ``dogo-cam-power``) owns remote power:
  ``jg-casa/dogo-cam/power/set`` in, ``power/state`` + ``status`` (LWT) out.
* ``dogcam_stream.py`` (client id ``dogo-cam-stream``) owns lux metering:
  ``jg-casa/dogo-cam/meter/set`` in, ``jg-casa/dogo-cam/lux`` out.

``paho`` is imported lazily inside :meth:`MqttLink.start`, so ``DOGCAM_MQTT=0``
(tests, bench) and machines without paho-mqtt never need it, and importing this
module never touches the network.
"""

import logging
import os
import threading

log = logging.getLogger("dogcam_mqtt")

DEFAULT_HOST = "192.168.50.211"
DEFAULT_PORT = 1883
KEEPALIVE = 60
QOS = 1

POWER_SET_TOPIC = "jg-casa/dogo-cam/power/set"
POWER_STATE_TOPIC = "jg-casa/dogo-cam/power/state"
STATUS_TOPIC = "jg-casa/dogo-cam/status"
METER_SET_TOPIC = "jg-casa/dogo-cam/meter/set"
LUX_TOPIC = "jg-casa/dogo-cam/lux"

# Value used when a set topic is empty (cleared retained message) or never set.
POWER_DEFAULT = "ON"
METER_DEFAULT = "OFF"

POWER_CLIENT_ID = "dogo-cam-power"
STREAM_CLIENT_ID = "dogo-cam-stream"
POWER_CLIENT_ID_ENV = "DOGCAM_MQTT_POWER_CLIENT_ID"
STREAM_CLIENT_ID_ENV = "DOGCAM_MQTT_STREAM_CLIENT_ID"

_DISABLED_VALUES = {"0", "false", "no", "off"}


def enabled():
    """MQTT is on unless DOGCAM_MQTT=0 (or false/no/off)."""
    return os.getenv("DOGCAM_MQTT", "1").strip().lower() not in _DISABLED_VALUES


def broker():
    """(host, port) from DOGCAM_MQTT_HOST / DOGCAM_MQTT_PORT."""
    host = os.getenv("DOGCAM_MQTT_HOST", "").strip() or DEFAULT_HOST
    raw_port = os.getenv("DOGCAM_MQTT_PORT", "").strip()
    try:
        port = int(raw_port) if raw_port else DEFAULT_PORT
    except ValueError:
        log.warning("Invalid DOGCAM_MQTT_PORT %r; using %d", raw_port, DEFAULT_PORT)
        port = DEFAULT_PORT
    return host, port


def client_id(env_name, default):
    return os.getenv(env_name, "").strip() or default


_unknown_lock = threading.Lock()
_unknown_seen = set()


def parse_on_off(payload, default, topic=""):
    """Parse an ON/OFF set payload per the contract.

    Stripped and case-insensitive; empty means ``default``. Anything else
    returns None (caller keeps its current mode) and is logged once.
    """
    if isinstance(payload, (bytes, bytearray)):
        payload = bytes(payload).decode("utf-8", errors="replace")
    text = (payload or "").strip()
    value = text.upper()
    if not value:
        return default
    if value in ("ON", "OFF"):
        return value
    key = (topic, value)
    with _unknown_lock:
        first = key not in _unknown_seen
        _unknown_seen.add(key)
    if first:
        log.warning("Ignoring unknown payload %r on %s; keeping the current mode", text, topic or "MQTT")
    return None


def _load_paho():
    import paho.mqtt.client as mqtt

    return mqtt


def _is_failure(reason_code):
    failure = getattr(reason_code, "is_failure", None)
    if failure is None:  # paho 1.x passes a plain int rc
        return reason_code != 0
    return bool(failure)


class MqttLink:
    """One paho client: optional LWT, retained-friendly subscriptions, auto-reconnect.

    subscriptions: {topic: handler(payload_bytes)}; handlers run on paho's
    network thread and must not block.
    will / online: (topic, payload) published retained at QoS 1 by the broker
    when we vanish / by us on every (re)connect.
    """

    def __init__(self, client_id, subscriptions=None, will=None, online=None, on_connect=None):
        self.client_id = client_id
        self.subscriptions = dict(subscriptions or {})
        self.will = will
        self.online = online
        self.on_connect = on_connect
        self.client = None
        self.connected = threading.Event()
        self._lock = threading.Lock()

    def start(self):
        """Create the client and connect in the background. False if MQTT is unavailable."""
        with self._lock:
            if self.client is not None:
                return True
            try:
                mqtt = _load_paho()
            except ImportError as error:
                log.warning("paho-mqtt is not installed (%s); MQTT disabled", error)
                return False
            host, port = broker()
            try:
                if hasattr(mqtt, "CallbackAPIVersion"):
                    client = mqtt.Client(
                        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                        client_id=self.client_id,
                    )
                else:
                    client = mqtt.Client(client_id=self.client_id)
                if self.will:
                    client.will_set(self.will[0], self.will[1], qos=QOS, retain=True)
                client.on_connect = self._handle_connect
                client.on_disconnect = self._handle_disconnect
                client.on_message = self._handle_message
                client.reconnect_delay_set(min_delay=1, max_delay=60)
                client.connect_async(host, port, keepalive=KEEPALIVE)
                client.loop_start()
            except Exception as error:
                log.error("MQTT client %s could not start: %s", self.client_id, error)
                return False
            self.client = client
            log.info("MQTT client %s connecting to %s:%d", self.client_id, host, port)
            return True

    def publish(self, topic, payload, retain=True):
        client = self.client
        if client is None:
            return False
        try:
            client.publish(topic, payload, qos=QOS, retain=retain)
        except Exception as error:
            log.warning("MQTT publish to %s failed: %s", topic, error)
            return False
        return True

    def stop(self):
        with self._lock:
            client, self.client = self.client, None
        self.connected.clear()
        if client is None:
            return
        try:
            client.disconnect()
            client.loop_stop()
        except Exception as error:
            log.debug("MQTT stop failed: %s", error)

    def _handle_connect(self, client, _userdata, _flags, reason_code=0, _properties=None):
        if _is_failure(reason_code):
            log.warning("MQTT client %s: connection refused (%s)", self.client_id, reason_code)
            return
        self.connected.set()
        log.info("MQTT client %s connected", self.client_id)
        if self.online:
            client.publish(self.online[0], self.online[1], qos=QOS, retain=True)
        for topic in self.subscriptions:
            client.subscribe(topic, qos=QOS)
        if self.on_connect is not None:
            try:
                self.on_connect()
            except Exception:
                log.exception("MQTT on_connect hook failed")

    def _handle_disconnect(self, _client, _userdata, *args):
        # paho 2.x: (disconnect_flags, reason_code, properties); paho 1.x: (rc,)
        reason = args[1] if len(args) >= 2 else (args[0] if args else None)
        self.connected.clear()
        log.warning("MQTT client %s disconnected (%s); paho reconnects", self.client_id, reason)

    def _handle_message(self, _client, _userdata, message):
        handler = self.subscriptions.get(message.topic)
        if handler is None:
            return
        try:
            handler(message.payload)
        except Exception:
            log.exception("MQTT handler for %s failed", message.topic)
