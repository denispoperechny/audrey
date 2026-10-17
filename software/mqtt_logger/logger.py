"""MQTT logger: records everything on the boat's MQTT topics into a storage account.

    cp .env.example .env             # then fill in the broker settings
    pip install -r requirements.txt
    python logger.py

Runs in the background, as a container app in Azure (see Dockerfile). Every message is
filed under the 10-minute window it arrived in (UTC, aligned to the clock: 10:00, 10:10,
...). When a window closes, its messages are uploaded as one JSON Lines file:

    <container>/<prefix>/YYYY/MM/DD/mqtt_YYYYMMDDTHHMMZ.jsonl      (the window's start)

A window with no messages writes nothing. One line per message, in the same format as
missions/3_control_test/src/mqtt_logger.py:

    {"t": "2026-10-17T15:42:07.123+00:00", "unix": 1792..., "topic": "audrey/boat1/telemetry", "data": {...}}

("raw": "<text>" instead of "data" if the payload wasn't JSON; "retained": true if the
broker replayed it). Connection events go to the container's log only, not to the files.

Nothing is lost on purpose: a window whose upload fails is kept and retried at the next
flush (up to MAX_PENDING_WINDOWS of them), and on shutdown (SIGTERM, e.g. a new revision)
everything, including the window still open, is uploaded first. A file that already exists
is never overwritten; the next one gets a -2, -3... suffix (a window split by a restart).

Settings come from environment variables, read from the .env file next to this one if
there is one (variables already set win):

    MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASSWORD, MQTT_CA_FILE   the broker, as in ground control
    MQTT_TOPICS            what to record, comma-separated (default audrey/boat1/#)
    STORAGE_ACCOUNT_URL    e.g. https://audreylogs.blob.core.windows.net
    STORAGE_CONTAINER      default logs
    STORAGE_PREFIX         folder inside the container, default mqtt
    WINDOW_S               window length in seconds, default 600
    LOCAL_OUTPUT_DIR       write the files to this folder instead of the storage account

The storage account is reached with DefaultAzureCredential: the container app's managed
identity in Azure (it needs "Storage Blob Data Contributor"), your az login locally.
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

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
MAX_PENDING_WINDOWS = 144  # windows kept while uploads fail: a day's worth
FLUSH_DELAY_S = 5  # flush this long after a window closes, so late arrivals still make it

log = logging.getLogger("mqtt_logger")


def settings():
    load_dotenv(APP_DIR / ".env")
    env = os.environ
    missing = [n for n in ("MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PASSWORD", "MQTT_CA_FILE") if not env.get(n)]
    if not env.get("STORAGE_ACCOUNT_URL") and not env.get("LOCAL_OUTPUT_DIR"):
        missing.append("STORAGE_ACCOUNT_URL (or LOCAL_OUTPUT_DIR)")
    if missing:
        raise SystemExit("settings missing: %s. Copy .env.example to .env and fill it in." % ", ".join(missing))
    ca_file = Path(env["MQTT_CA_FILE"])
    return {
        "host": env["MQTT_HOST"],
        "port": int(env["MQTT_PORT"]),
        "user": env["MQTT_USER"],
        "password": env["MQTT_PASSWORD"],
        "ca_file": str(ca_file if ca_file.is_absolute() else APP_DIR / ca_file),
        "topics": [t.strip() for t in env.get("MQTT_TOPICS", "audrey/boat1/#").split(",") if t.strip()],
        "account_url": env.get("STORAGE_ACCOUNT_URL", ""),
        "container": env.get("STORAGE_CONTAINER", "logs"),
        "prefix": env.get("STORAGE_PREFIX", "mqtt").strip("/"),
        "window_s": int(env.get("WINDOW_S", "600")),
        "local_dir": env.get("LOCAL_OUTPUT_DIR", ""),
    }


def window_name(prefix, window_start):
    start = datetime.fromtimestamp(window_start, timezone.utc)
    return "%s/%s/mqtt_%s.jsonl" % (prefix, start.strftime("%Y/%m/%d"), start.strftime("%Y%m%dT%H%MZ"))


def with_suffix(name, n):
    return name if n == 1 else name.replace(".jsonl", "-%d.jsonl" % n)


class BlobStore:
    """Uploads to the storage account; never overwrites."""

    def __init__(self, account_url, container):
        from azure.core.exceptions import ResourceExistsError
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient, ContentSettings

        self._exists = ResourceExistsError
        self._content = ContentSettings(content_type="application/x-ndjson")
        service = BlobServiceClient(account_url, credential=DefaultAzureCredential(exclude_interactive_browser_credential=True))
        self._container = service.get_container_client(container)
        self.where = "%s/%s" % (account_url.rstrip("/"), container)

    def put(self, name, data):
        """Returns the name it was stored under."""
        for n in range(1, 100):
            candidate = with_suffix(name, n)
            try:
                self._container.upload_blob(candidate, data, overwrite=False, content_settings=self._content)
                return candidate
            except self._exists:
                continue
        raise RuntimeError("no free name for %s" % name)


class LocalStore:
    """Writes to a local folder instead, for trying it out."""

    def __init__(self, folder):
        self.root = Path(folder)
        self.where = str(self.root)

    def put(self, name, data):
        for n in range(1, 100):
            path = self.root / with_suffix(name, n)
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                return with_suffix(name, n)
        raise RuntimeError("no free name for %s" % name)


class Recorder:
    def __init__(self, config, store):
        self.config = config
        self.store = store
        self.window_s = config["window_s"]
        self.lock = threading.Lock()
        self.windows = {}  # window start (unix s) -> list of JSON lines
        self.received = 0
        self.uploaded_files = 0

    # ---- MQTT (paho's thread) ------------------------------------------------------------

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("broker refused the connection: %s", reason_code)
            return
        for topic in self.config["topics"]:
            client.subscribe(topic, qos=1)
        log.info("connected to %s:%d, recording %s", self.config["host"], self.config["port"], ", ".join(self.config["topics"]))

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        log.warning("disconnected from the broker: %s", reason_code)

    def on_message(self, client, userdata, message):
        now = time.time()
        record = {"t": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds"),
                  "unix": round(now, 3), "topic": message.topic}
        if message.retain:
            record["retained"] = True
        try:
            record["data"] = json.loads(message.payload)
        except ValueError:
            record["raw"] = message.payload.decode("utf-8", "replace")
        line = json.dumps(record, separators=(",", ":"))
        window = int(now // self.window_s) * self.window_s
        with self.lock:
            self.windows.setdefault(window, []).append(line)
            self.received += 1

    # ---- flushing (main thread) ---------------------------------------------------------

    def flush(self, include_open=False):
        """Uploads every closed window that has messages (and the open one too, on shutdown).
        A failed upload stays pending for the next flush."""
        current = int(time.time() // self.window_s) * self.window_s
        with self.lock:
            ready = sorted(w for w in self.windows if include_open or w < current)
            batches = [(w, self.windows.pop(w)) for w in ready]
        for i, (window, lines) in enumerate(batches):
            name = window_name(self.config["prefix"], window)
            try:
                stored = self.store.put(name, ("\n".join(lines) + "\n").encode("utf-8"))
            except Exception as e:  # keep it, and everything after it, for the next try
                log.error("upload of %s failed (%s: %s); keeping it for the next flush", name, type(e).__name__, e)
                with self.lock:
                    for w, l in batches[i:]:
                        self.windows[w] = l + self.windows.get(w, [])
                    while len(self.windows) > MAX_PENDING_WINDOWS:
                        dropped = min(self.windows)
                        log.error("dropping window %s: %d messages could not be uploaded",
                                  window_name(self.config["prefix"], dropped), len(self.windows.pop(dropped)))
                return
            self.uploaded_files += 1
            log.info("wrote %s (%d messages)", stored, len(lines))

    def seconds_to_next_flush(self):
        now = time.time()
        return (int(now // self.window_s) + 1) * self.window_s + FLUSH_DELAY_S - now


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    config = settings()
    store = LocalStore(config["local_dir"]) if config["local_dir"] else BlobStore(config["account_url"], config["container"])
    recorder = Recorder(config, store)
    log.info("recording to %s/%s in %d s windows", store.where, config["prefix"], config["window_s"])

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="audrey-logger-" + uuid.uuid4().hex[:8])
    client.username_pw_set(config["user"], config["password"])
    client.tls_set(ca_certs=config["ca_file"])
    client.on_connect = recorder.on_connect
    client.on_disconnect = recorder.on_disconnect
    client.on_message = recorder.on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(config["host"], config["port"], keepalive=30)
    client.loop_start()

    # A signal only sets the flag, so it can't interrupt an upload half-way.
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda signum, frame: stopping.set())
    signal.signal(signal.SIGINT, lambda signum, frame: stopping.set())
    try:
        while not stopping.wait(max(0.0, recorder.seconds_to_next_flush())):
            recorder.flush()
        log.info("stopping: uploading what is left")
    finally:
        client.loop_stop()
        client.disconnect()
        recorder.flush(include_open=True)
        log.info("stopped after %d messages, %d files written", recorder.received, recorder.uploaded_files)


if __name__ == "__main__":
    main()
