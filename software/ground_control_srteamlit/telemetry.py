"""MQTT link to the boat: a background subscriber for its telemetry, and commands to it.

The boat (electronics/main_controller_esp32_mp/mqtt.py) publishes one JSON message per
second to TELEMETRY_TOPIC, built by Controller.telemetry() in controller.py. This keeps the
latest message plus a few things derived from the stream: the last known battery voltage
(the "bcu" block is null whenever a status read from the Pico fails) and a track of
recent valid GPS fixes.

Commands go to COMMAND_TOPIC as JSON objects; the ones the boat acts on are listed at the
top of its controller.py.

Broker settings come from environment variables (see load_mqtt_config), read from the
.env file next to this one if there is one: copy .env.example to .env and fill it in. .env
is gitignored. Variables already set in the environment win over the file, so a container
can also be given them directly.
"""

import json
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import paho.mqtt.client as mqtt
import streamlit as st
from dotenv import load_dotenv

TELEMETRY_TOPIC = "audrey/boat1/telemetry"  # MQTT_TELEMETRY_TOPIC in the boat's mqtt.py
COMMAND_TOPIC = "audrey/boat1/cmd"  # MQTT_COMMAND_TOPIC in the boat's mqtt.py
TRACK_LEN = 3600  # GPS fixes kept for the track (1 per second -> an hour)

APP_DIR = Path(__file__).resolve().parent
ENV_FILE = APP_DIR / ".env"
MQTT_SETTINGS = ("MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PASSWORD", "MQTT_CA_FILE")


def load_mqtt_config():
    """{"host", "port", "user", "password", "ca_file"} from MQTT_HOST, MQTT_PORT, MQTT_USER,
    MQTT_PASSWORD and MQTT_CA_FILE (the broker's certificate; a relative path is relative to
    this folder)."""
    load_dotenv(ENV_FILE)  # doesn't override variables that are already set
    missing = [name for name in MQTT_SETTINGS if not os.environ.get(name)]
    if missing:
        raise RuntimeError("MQTT settings missing: %s. Copy .env.example to .env and fill it in." % ", ".join(missing))
    ca_file = Path(os.environ["MQTT_CA_FILE"])
    return {
        "host": os.environ["MQTT_HOST"],
        "port": int(os.environ["MQTT_PORT"]),
        "user": os.environ["MQTT_USER"],
        "password": os.environ["MQTT_PASSWORD"],
        "ca_file": str(ca_file if ca_file.is_absolute() else APP_DIR / ca_file),
    }


class TelemetryFeed:
    """Subscribes in paho's own thread; snapshot() is safe to call from Streamlit reruns."""

    def __init__(self, config):
        self._lock = threading.Lock()
        self.connected = False
        self.error = None
        self.latest = None  # last telemetry dict
        self.received_at = None  # time.time() of the last message
        self.messages = 0
        self.bad_messages = 0
        self.battery = None  # (battery_mv, low_battery, time.time()) of the last status that had it
        self.track = deque(maxlen=TRACK_LEN)  # (lat, lon) of valid fixes, oldest first

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id="audrey-ground-" + uuid.uuid4().hex[:8])
        client.username_pw_set(config["user"], config["password"])
        client.tls_set(ca_certs=config["ca_file"])
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.connect_async(config["host"], config["port"], keepalive=30)
        client.loop_start()  # connects, reconnects and delivers messages in its own thread
        self._client = client

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        with self._lock:
            if reason_code.is_failure:
                self.connected = False
                self.error = "connect refused: %s" % reason_code
                return
            self.connected = True
            self.error = None
        client.subscribe(TELEMETRY_TOPIC)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        with self._lock:
            self.connected = False
            if reason_code.is_failure:
                self.error = "disconnected: %s" % reason_code

    def _on_message(self, client, userdata, message):
        now = time.time()
        try:
            data = json.loads(message.payload)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except ValueError:
            with self._lock:
                self.bad_messages += 1
            return
        with self._lock:
            self.latest = data
            self.received_at = now
            self.messages += 1
            bcu = data.get("bcu")
            if bcu and bcu.get("battery_mv") is not None:
                self.battery = (bcu["battery_mv"], bool(bcu.get("low_battery")), now)
            gps = data.get("gps") or {}
            if gps.get("fix_valid") and gps.get("lat_e7") is not None:
                point = (gps["lat_e7"] / 1e7, gps["lon_e7"] / 1e7)
                if not self.track or self.track[-1] != point:
                    self.track.append(point)

    def send_command(self, command):
        """Publishes a command (dict) to the boat. Raises RuntimeError if it can't go out now.
        Not retained: a boat that connects later doesn't get it."""
        if not self._client.is_connected():
            raise RuntimeError("not connected to the MQTT broker")
        info = self._client.publish(COMMAND_TOPIC, json.dumps(command), qos=1)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError("publish failed: %s" % mqtt.error_string(info.rc))

    def send_destination(self, lat, lon):
        """Degrees. Sent as integers of degrees * 1e7, the way the boat reports positions."""
        self.send_command({"cmd": "destination", "lat_e7": round(lat * 1e7), "lon_e7": round(lon * 1e7)})

    def clear_destination(self):
        self.send_command({"cmd": "clear_destination"})

    def snapshot(self):
        """A consistent copy of the current state."""
        with self._lock:
            return {
                "connected": self.connected,
                "error": self.error,
                "latest": self.latest,
                "received_at": self.received_at,
                "messages": self.messages,
                "bad_messages": self.bad_messages,
                "battery": self.battery,
                "track": list(self.track),
            }


@st.cache_resource
def get_feed():
    """One MQTT connection per server process, shared by all pages and browser sessions."""
    return TelemetryFeed(load_mqtt_config())
