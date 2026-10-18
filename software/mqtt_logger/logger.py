"""MQTT logger: records MQTT streams (the boat's topics, the GPS reference) into a storage account.

    cp .env.example .env             # then fill in the broker settings
    pip install -r requirements.txt
    python logger.py

Runs in the background, as a container app in Azure (see Dockerfile). Every message is
filed under the 10-minute window it arrived in (UTC, aligned to the clock: 10:00, 10:10,
...). When a window closes, each stream's messages are uploaded as one JSON Lines file,
in that stream's own folder:

    <container>/<stream>/YYYY/MM/DD/<stream>_YYYYMMDDTHHMMZ.jsonl      (the window's start)

A stream is a name and the MQTT topics it covers (MQTT_STREAMS). With the defaults:

    mqtt            audrey/boat1/#            the boat: telemetry and the commands sent to it
    gps-reference   audrey/gps-reference/#    the GPS reference

A stream with no messages in a window writes nothing for it. One line per message, in the same format as
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
    MQTT_STREAMS           what to record: name=topic filter pairs, comma-separated; a name
                           can be given several filters (default as above). The name is the
                           folder and the start of the file names. A message goes to the
                           first stream whose filter matches.
    STORAGE_ACCOUNT_URL    e.g. https://audreylogs.blob.core.windows.net
    STORAGE_CONTAINER      default logs
    WINDOW_S               window length in seconds, default 600
    LOCAL_OUTPUT_DIR       write the files to this folder instead of the storage account

The storage account is reached with DefaultAzureCredential: the container app's managed
identity in Azure (it needs "Storage Blob Data Contributor"), your az login locally.

Meant to run unattended:
  - Broker connection drops are reconnected by paho, with back-off up to 30 s, and the
    subscriptions are renewed on every reconnect. (Messages sent while it is disconnected
    are not recorded: the boat publishes without delivery guarantees.)
  - A message that can't be processed is logged and skipped; it can't stop the recording.
  - Self-check: every HEARTBEAT_S it publishes a heartbeat to its own topic and listens
    for it. If none has come back for HEARTBEAT_TIMEOUT_S (the broker gone, a stuck
    connection, a dead network thread), it uploads what it has and exits with an error,
    and the container platform starts it again from scratch.
  - Storage errors keep the data in memory and retry (see above); memory is bounded by
    MAX_PENDING_WINDOWS.
"""

import json
import logging
import os
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
MAX_PENDING_WINDOWS = 72  # per stream, kept while uploads fail: 12 hours, ~100 MB of boat telemetry
DEFAULT_STREAMS = "mqtt=audrey/boat1/#,gps-reference=audrey/gps-reference/#"
FLUSH_DELAY_S = 5  # flush this long after a window closes, so late arrivals still make it
HEARTBEAT_S = int(os.environ.get("HEARTBEAT_S", "60"))
HEARTBEAT_TIMEOUT_S = int(os.environ.get("HEARTBEAT_TIMEOUT_S", "600"))  # no heartbeat back for this long: restart
HEARTBEAT_TOPIC = "audrey/logger/heartbeat/"  # + the client id; outside the recorded topics

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
        "streams": parse_streams(env.get("MQTT_STREAMS") or DEFAULT_STREAMS),
        "account_url": env.get("STORAGE_ACCOUNT_URL", ""),
        "container": env.get("STORAGE_CONTAINER", "logs"),
        "window_s": int(env.get("WINDOW_S", "600")),
        "local_dir": env.get("LOCAL_OUTPUT_DIR", ""),
    }


def parse_streams(text):
    """"name=filter,name=filter" -> [(name, filter), ...], in order."""
    streams = []
    for part in text.split(","):
        name, sep, topic = part.partition("=")
        name, topic = name.strip().strip("/"), topic.strip()
        if not sep or not name or not topic or "/" in name:
            raise SystemExit("MQTT_STREAMS: %r is not name=topic-filter" % part.strip())
        streams.append((name, topic))
    return streams


def window_name(stream, window_start):
    start = datetime.fromtimestamp(window_start, timezone.utc)
    return "%s/%s/%s_%s.jsonl" % (stream, start.strftime("%Y/%m/%d"), stream, start.strftime("%Y%m%dT%H%MZ"))


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
    def __init__(self, config, store, client_id):
        self.config = config
        self.store = store
        self.window_s = config["window_s"]
        self.heartbeat_topic = HEARTBEAT_TOPIC + client_id
        self.lock = threading.Lock()
        self.streams = config["streams"]
        self.windows = {}  # (window start (unix s), stream) -> list of JSON lines
        self.received = 0
        self.uploaded_files = 0
        self.last_heartbeat = time.time()  # last heartbeat that came back (start counts as one)

    # ---- MQTT (paho's thread) ------------------------------------------------------------

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("broker refused the connection: %s", reason_code)
            return
        for topic in sorted({t for _, t in self.streams}) + [self.heartbeat_topic]:
            client.subscribe(topic, qos=1)
        log.info("connected to %s:%d, recording %s", self.config["host"], self.config["port"],
                 ", ".join("%s (%s)" % (name, topic) for name, topic in self.streams))

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        log.warning("disconnected from the broker: %s", reason_code)

    def on_message(self, client, userdata, message):
        # Runs in paho's network thread: an exception escaping from here would end it.
        try:
            now = time.time()
            if message.topic == self.heartbeat_topic:
                self.last_heartbeat = now
                return
            stream = next((name for name, topic in self.streams if mqtt.topic_matches_sub(topic, message.topic)), None)
            if stream is None:
                return  # a retained message on an overlapping subscription, or similar
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
                self.windows.setdefault((window, stream), []).append(line)
                self.received += 1
        except Exception:
            log.exception("could not record a message on %s; skipped", getattr(message, "topic", "?"))

    # ---- flushing (main thread) ---------------------------------------------------------

    def flush(self, include_open=False):
        """Uploads every closed window that has messages (and the open one too, on shutdown).
        A failed upload stays pending for the next flush."""
        current = int(time.time() // self.window_s) * self.window_s
        with self.lock:
            ready = sorted(key for key in self.windows if include_open or key[0] < current)
            batches = [(key, self.windows.pop(key)) for key in ready]
        for i, ((window, stream), lines) in enumerate(batches):
            name = window_name(stream, window)
            try:
                stored = self.store.put(name, ("\n".join(lines) + "\n").encode("utf-8"))
            except Exception as e:  # keep it, and everything after it, for the next try
                log.error("upload of %s failed (%s: %s); keeping it for the next flush", name, type(e).__name__, e)
                with self.lock:
                    for w, l in batches[i:]:
                        self.windows[w] = l + self.windows.get(w, [])
                    while len(self.windows) > MAX_PENDING_WINDOWS * len(self.streams):
                        dropped = min(self.windows)
                        log.error("dropping window %s: %d messages could not be uploaded",
                                  window_name(dropped[1], dropped[0]), len(self.windows.pop(dropped)))
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
    client_id = "audrey-logger-" + uuid.uuid4().hex[:8]
    recorder = Recorder(config, store, client_id)
    log.info("recording to %s in %d s windows", store.where, config["window_s"])

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
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
    exit_code = 0
    next_flush = time.time() + recorder.seconds_to_next_flush()
    next_heartbeat = time.time()
    try:
        while not stopping.wait(max(0.0, min(next_flush, next_heartbeat) - time.time())):
            now = time.time()
            if now >= next_heartbeat:
                next_heartbeat = now + HEARTBEAT_S
                client.publish(recorder.heartbeat_topic, "%.0f" % now, qos=1)  # fails quietly while disconnected
                silent = now - recorder.last_heartbeat
                if silent > HEARTBEAT_TIMEOUT_S:
                    log.error("no heartbeat back from the broker for %.0f s: exiting so the platform restarts this", silent)
                    exit_code = 1
                    break
            if now >= next_flush:
                recorder.flush()
                next_flush = time.time() + recorder.seconds_to_next_flush()
        else:
            log.info("stopping: uploading what is left")
    finally:
        client.loop_stop()
        client.disconnect()
        recorder.flush(include_open=True)
        log.info("stopped after %d messages, %d files written", recorder.received, recorder.uploaded_files)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
