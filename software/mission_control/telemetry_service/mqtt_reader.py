"""MQTT reader: writes the boat's MQTT messages into the local telemetry folder, for the
other mission control processes to read (telemetry_provider.TelemetryProvider).

    python -m telemetry_service.mqtt_reader        (from software/mission_control)

Runs as its own process (supervisord.conf). Every message is appended to the file of the
current TELEMETRY_FILE_SECONDS period (files.py has the layout); when a new period starts, a new
file is begun and only the newest TELEMETRY_KEEP_FILES are kept, older ones deleted. Each line
is written with a single write, so a reader sees either the whole line or none of it yet.

Settings from environment variables (or .env: settings.py, .env.example):

    MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASSWORD, MQTT_CA_FILE   the broker, as for the MQTT logger
    TELEMETRY_TOPICS          topic filters to read, comma-separated (default audrey/boat1/#)
    TELEMETRY_DIR             the folder (default /tmp/audrey-telemetry)
    TELEMETRY_FILE_SECONDS    the period of one file (default 30)
    TELEMETRY_KEEP_FILES      how many files to keep (default 20)

Broker connection drops are reconnected by paho (back-off up to 30 s) and the subscriptions
renewed. A message that can't be written is logged and skipped. supervisord restarts the
process if it stops.
"""

import json
import logging
import os
import signal
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


from telemetry_service.files import (DEFAULT_FILE_SECONDS, DEFAULT_KEEP_FILES, file_name, telemetry_dir,
                                     telemetry_files)

log = logging.getLogger("mqtt_reader")
APP_DIR = Path(__file__).resolve().parent.parent  # software/mission_control
DEFAULT_TOPICS = "audrey/boat1/#"
PRUNE_EVERY_S = 5  # also start new files and delete old ones when no messages come


class TelemetryFiles:
    """Appends lines to the current period's file and keeps only the newest few files."""

    def __init__(self, folder, file_seconds=DEFAULT_FILE_SECONDS, keep=DEFAULT_KEEP_FILES):
        self.folder = folder
        self.file_seconds = file_seconds
        self.keep = keep
        self.lock = threading.Lock()
        os.makedirs(folder, exist_ok=True)

    def write(self, line, now=None):
        now = time.time() if now is None else now
        name = file_name(int(now // self.file_seconds) * self.file_seconds)
        with self.lock:
            with open(os.path.join(self.folder, name), "a", encoding="utf-8") as f:
                f.write(line + "\n")  # one write: a reader never sees half a line as complete
            self._prune(name)

    def prune(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            self._prune(file_name(int(now // self.file_seconds) * self.file_seconds))

    def _prune(self, current):
        # Keep the newest `keep` periods up to the current one (an empty period has no file and
        # doesn't count), and delete the rest.
        names = [n for n in telemetry_files(self.folder) if n <= current]
        for name in names[:-self.keep]:
            try:
                os.remove(os.path.join(self.folder, name))
            except FileNotFoundError:
                pass


def record_line(topic, payload, retained=False, now=None):
    """A message as one line of the shared format."""
    now = time.time() if now is None else now
    record = {"t": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds"),
              "unix": round(now, 3), "topic": topic}
    if retained:
        record["retained"] = True
    try:
        record["data"] = json.loads(payload)
    except ValueError:
        record["raw"] = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)
    return json.dumps(record, separators=(",", ":"))


def settings():
    env = os.environ
    missing = [n for n in ("MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PASSWORD", "MQTT_CA_FILE") if not env.get(n)]
    if missing:
        raise SystemExit("settings missing: %s" % ", ".join(missing))
    ca_file = Path(env["MQTT_CA_FILE"])
    return {
        "host": env["MQTT_HOST"], "port": int(env["MQTT_PORT"]),
        "user": env["MQTT_USER"], "password": env["MQTT_PASSWORD"],
        "ca_file": str(ca_file if ca_file.is_absolute() else APP_DIR / ca_file),
        "topics": [t.strip() for t in (env.get("TELEMETRY_TOPICS") or DEFAULT_TOPICS).split(",") if t.strip()],
        "folder": telemetry_dir(),
        "file_seconds": int(env.get("TELEMETRY_FILE_SECONDS") or DEFAULT_FILE_SECONDS),
        "keep": int(env.get("TELEMETRY_KEEP_FILES") or DEFAULT_KEEP_FILES),
    }


def main():
    import paho.mqtt.client as mqtt

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = settings()
    files = TelemetryFiles(config["folder"], config["file_seconds"], config["keep"])
    log.info("reading %s into %s: %d s files, the newest %d kept", ", ".join(config["topics"]), config["folder"],
             config["file_seconds"], config["keep"])

    def on_connect(client, userdata, flags, reason_code, properties):
        log.info("connected to the broker: %s", reason_code)
        for topic in config["topics"]:
            client.subscribe(topic, qos=1)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        log.warning("disconnected from the broker: %s", reason_code)

    def on_connect_fail(client, userdata):
        log.warning("could not reach the broker at %s:%d; retrying", config["host"], config["port"])

    def on_message(client, userdata, message):
        # paho's network thread: an exception escaping from here would end it.
        try:
            files.write(record_line(message.topic, message.payload, message.retain))
        except Exception:
            log.exception("could not write a message on %s; skipped", getattr(message, "topic", "?"))

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="audrey-telemetry-" + uuid.uuid4().hex[:8])
    client.username_pw_set(config["user"], config["password"])
    client.tls_set(ca_certs=config["ca_file"])
    client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
    client.on_connect_fail = on_connect_fail
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(config["host"], config["port"], keepalive=30)
    client.loop_start()

    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda signum, frame: stopping.set())
    signal.signal(signal.SIGINT, lambda signum, frame: stopping.set())
    try:
        while not stopping.wait(PRUNE_EVERY_S):
            files.prune()
    finally:
        client.loop_stop()
        client.disconnect()
        log.info("stopped")


if __name__ == "__main__":
    main()
