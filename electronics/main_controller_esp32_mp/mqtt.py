"""WiFi + MQTT link: commands in, telemetry out, each through its own queue.

WiFi and MQTT each run in their own thread: connecting to WiFi blocks for seconds and the
MQTT TLS handshake for several hundred ms, which would starve the main loop and trip the
Pico's failsafe. The main loop never touches the network -- it only calls publish() and
get_command(), which just move items in and out of the queues under a lock.

  link = MqttLink(config)
  link.start()
  ...
  link.publish({"heading": 251.1})   # queued; sent by the MQTT thread (dict -> JSON)
  cmd = link.get_command()           # (payload bytes, ticks_ms received) or None

Both queues are bounded and drop their oldest item when full: while the broker is
unreachable, telemetry keeps only the latest messages, and a backlog of commands can't
grow without limit. Commands are handed over as raw bytes; what they mean (and whether
one is too old to act on, see the timestamp) is up to the caller.
"""

import _thread
import binascii
import machine
import network
import ntptime
import ssl
import time

try:
    import ujson as json
except ImportError:
    import json

from umqtt.simple import MQTTClient

MQTT_CLIENT_ID = b"audrey-esp32-" + binascii.hexlify(machine.unique_id())
MQTT_TELEMETRY_TOPIC = b"audrey/boat1/telemetry"
MQTT_COMMAND_TOPIC = b"audrey/boat1/cmd"
MQTT_KEEPALIVE_S = 30
MQTT_POLL_INTERVAL_MS = 200  # how often the MQTT thread checks for commands and sends telemetry
MQTT_RECONNECT_DELAY_MS = 2000

WIFI_CHECK_INTERVAL_MS = 1000
WIFI_CONNECT_TIMEOUT_MS = 15000

COMMAND_QUEUE_LEN = 10
TELEMETRY_QUEUE_LEN = 5


class BoundedQueue:
    """Thread-safe FIFO that drops its oldest item when full."""

    def __init__(self, maxlen):
        self.maxlen = maxlen
        self.items = []
        self.lock = _thread.allocate_lock()
        self.dropped = 0

    def put(self, item):
        with self.lock:
            if len(self.items) >= self.maxlen:
                self.items.pop(0)
                self.dropped += 1
            self.items.append(item)

    def get(self):
        """Oldest item, or None if empty."""
        with self.lock:
            return self.items.pop(0) if self.items else None

    def __len__(self):
        return len(self.items)


class MqttLink:
    """Keeps WiFi and the MQTT session up in background threads."""

    def __init__(self, config, client_id=MQTT_CLIENT_ID,
                 command_topic=MQTT_COMMAND_TOPIC, telemetry_topic=MQTT_TELEMETRY_TOPIC):
        self.config = config
        self.client_id = client_id
        self.command_topic = command_topic
        self.telemetry_topic = telemetry_topic

        self.commands = BoundedQueue(COMMAND_QUEUE_LEN)
        self.telemetry = BoundedQueue(TELEMETRY_QUEUE_LEN)

        self.wlan = network.WLAN(network.STA_IF)
        self.wifi_connected = False
        self.mqtt_connected = False
        self.rx_count = 0
        self.tx_count = 0
        self.errors = 0

    def start(self):
        # Bring the WiFi driver up here, not in the thread, so it gets its memory before
        # the caller goes on to allocate more.
        self.wlan.active(True)
        _thread.start_new_thread(self._wifi_loop, ())
        _thread.start_new_thread(self._mqtt_loop, ())

    # --- main loop side ---------------------------------------------------------------

    def publish(self, payload):
        """Queues telemetry: a dict (sent as JSON), str or bytes."""
        self.telemetry.put(payload)

    def get_command(self):
        """Oldest received command as (payload bytes, ticks_ms received), or None."""
        return self.commands.get()

    def describe(self):
        """One-line status for the console."""
        return "mqtt: wifi=%s mqtt=%s rx=%d tx=%d errors=%d dropped cmd/tlm=%d/%d" % (
            "up" if self.wifi_connected else "down",
            "up" if self.mqtt_connected else "down",
            self.rx_count, self.tx_count, self.errors,
            self.commands.dropped, self.telemetry.dropped)

    # --- WiFi thread ------------------------------------------------------------------

    def _wifi_connect(self):
        """Returns True once connected, False if it didn't connect in time."""
        wlan = self.wlan
        if wlan.isconnected():
            return True
        wlan.connect(self.config["wifi_ssid"], self.config["wifi_password"])
        start = time.ticks_ms()
        while not wlan.isconnected():
            if time.ticks_diff(time.ticks_ms(), start) > WIFI_CONNECT_TIMEOUT_MS:
                return False
            time.sleep_ms(100)
        return True

    def _wifi_loop(self):
        """Keeps the station connected, reconnecting if the link drops."""
        self.wlan.active(True)
        while True:
            try:
                connected = self._wifi_connect()
            except Exception as e:  # keep the thread alive across any WiFi error
                connected = False
                print("wifi error: %s: %s" % (type(e).__name__, e))
            if connected != self.wifi_connected:
                self.wifi_connected = connected
                print("wifi %s" % ("connected " + self.wlan.ifconfig()[0] if connected else "disconnected"))
                if connected:
                    # The board's clock starts at year 2000 on every boot, which is before
                    # our TLS cert's validity start; sync it or certificate checks fail.
                    try:
                        ntptime.settime()
                        print("time synced:", time.gmtime())
                    except Exception as e:
                        print("ntp sync failed: %s: %s" % (type(e).__name__, e))
            time.sleep_ms(WIFI_CHECK_INTERVAL_MS)

    # --- MQTT thread ------------------------------------------------------------------

    def _ssl_params(self):
        """Best-effort TLS setup: verify against our CA file if the firmware supports it,
        otherwise fall back to an encrypted-but-unverified connection rather than failing."""
        ca_file = self.config["mqtt_ca_file"]
        try:
            with open(ca_file, "rb") as f:
                ca = f.read()
        except OSError:
            print("mqtt: CA file %s not found on device; connecting without cert verification" % ca_file)
            return {}
        try:
            return {"cert_reqs": ssl.CERT_REQUIRED, "cadata": ca, "server_hostname": self.config["mqtt_host"]}
        except AttributeError:
            print("mqtt: this build's ssl module can't verify a CA; connecting without cert verification")
            return {}

    def _on_message(self, topic, msg):
        """Runs in the MQTT thread, from inside check_msg()."""
        self.rx_count += 1
        self.commands.put((msg, time.ticks_ms()))

    def _connect(self):
        config = self.config
        client = MQTTClient(
            self.client_id,
            config["mqtt_host"],
            port=config["mqtt_port"],
            user=config["mqtt_user"],
            password=config["mqtt_password"],
            keepalive=MQTT_KEEPALIVE_S,
            ssl=True,
            ssl_params=self._ssl_params(),
        )
        client.set_callback(self._on_message)
        client.connect()
        client.subscribe(self.command_topic)
        return client

    def _send_telemetry(self, client):
        while True:
            payload = self.telemetry.get()
            if payload is None:
                return
            if isinstance(payload, dict):
                payload = json.dumps(payload)
            client.publish(self.telemetry_topic, payload)
            self.tx_count += 1

    def _mqtt_loop(self):
        """Connects, subscribes, then polls for commands and flushes queued telemetry.
        Any failure drops the client and reconnects after a delay, so a broker restart or
        a WiFi blip doesn't kill the thread."""
        client = None
        while True:
            try:
                if client is None:
                    if not self.wlan.isconnected():
                        time.sleep_ms(500)
                        continue
                    client = self._connect()
                    self.mqtt_connected = True
                    print("mqtt connected to %s:%d as %s" % (
                        self.config["mqtt_host"], self.config["mqtt_port"], self.client_id))

                client.check_msg()  # non-blocking; runs _on_message() if a command arrived
                self._send_telemetry(client)
                time.sleep_ms(MQTT_POLL_INTERVAL_MS)
            except Exception as e:
                self.errors += 1
                self.mqtt_connected = False
                print("mqtt error: %s: %s" % (type(e).__name__, e))
                if client is not None:
                    try:
                        client.disconnect()
                    except Exception:
                        pass
                    client = None
                time.sleep_ms(MQTT_RECONNECT_DELAY_MS)
