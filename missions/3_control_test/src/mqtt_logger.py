"""Logs everything on the boat's MQTT topics to a file, for the control test.

    pip install -r requirements.txt
    python mqtt_logger.py            # Ctrl+C (or a plain kill) to stop

Each run writes a new file, ../data/mqtt_<date>_<time>.jsonl: one JSON object per line,
written as it arrives, so a crash or a pulled plug loses nothing that was received.

  A message:   {"t": "...", "unix": ..., "topic": "audrey/boat1/telemetry", "data": {...}}
               ("raw": "<text>" instead of "data" if the payload wasn't JSON;
               "retained": true if the broker replayed it from before the logger connected)
  An event:    {"t": "...", "unix": ..., "event": "connected" | "disconnected" | "started" | "stopped", ...}

"t" is this computer's local time and "unix" the same moment in seconds: when the message
was received here, not when the boat sent it (telemetry carries the boat's own uptime_s).
The connection events tell a gap in the boat's telemetry from a gap in this logger's link.

Both directions are logged: the boat's telemetry and the commands sent to it (TOPICS).
Broker settings come from the boat's config.py, like the ground control app's.
"""

import argparse
import json
import runpy
import signal
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt

TOPICS = "audrey/boat1/#"  # telemetry and commands; see the boat's mqtt.py
STATUS_S = 10  # console status line period

MISSION_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = MISSION_DIR / "data"
CONTROLLER_DIR = MISSION_DIR.parents[1] / "electronics" / "main_controller_esp32_mp"


def load_mqtt_config():
    config = runpy.run_path(str(CONTROLLER_DIR / "config.py"))["config"]
    return {
        "host": config["mqtt_host"],
        "port": int(config["mqtt_port"]),
        "user": config["mqtt_user"],
        "password": config["mqtt_password"],
        "ca_file": str(CONTROLLER_DIR / config["mqtt_ca_file"]),
    }


class Logger:
    def __init__(self, path):
        self.path = path
        self.file = open(path, "a", encoding="utf-8")
        self.lock = threading.Lock()  # paho's thread writes messages, the main thread events
        self.messages = 0
        self.per_topic = {}
        self.last_message = None  # time.time() of the last message

    def write(self, record):
        now = time.time()
        line = {"t": datetime.fromtimestamp(now).astimezone().isoformat(timespec="milliseconds"),
                "unix": round(now, 3)}
        line.update(record)
        with self.lock:
            self.file.write(json.dumps(line, separators=(",", ":")) + "\n")
            self.file.flush()
        return now

    def event(self, name, **details):
        self.write({"event": name, **details})
        print("%s  %s %s" % (datetime.now().strftime("%H:%M:%S"), name, details or ""))

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.event("connect_refused", reason=str(reason_code))
            return
        client.subscribe(TOPICS, qos=1)
        self.event("connected")

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.event("disconnected", reason=str(reason_code))

    def on_message(self, client, userdata, message):
        record = {"topic": message.topic}
        if message.retain:
            record["retained"] = True
        try:
            record["data"] = json.loads(message.payload)
        except ValueError:
            record["raw"] = message.payload.decode("utf-8", "replace")
        self.last_message = self.write(record)
        self.messages += 1
        self.per_topic[message.topic] = self.per_topic.get(message.topic, 0) + 1

    def status(self):
        if self.last_message is None:
            return "no messages yet"
        counts = ", ".join("%s: %d" % (topic.rsplit("/", 1)[-1], n) for topic, n in sorted(self.per_topic.items()))
        return "%d messages (%s), last %.0f s ago" % (self.messages, counts, time.time() - self.last_message)

    def close(self):
        with self.lock:
            self.file.close()


def stop(signum, frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description="Log the boat's MQTT topics to a .jsonl file.")
    parser.add_argument("--out", type=Path, help="file to write (default: a new file in %s)" % DATA_DIR)
    args = parser.parse_args()

    path = args.out or DATA_DIR / ("mqtt_%s.jsonl" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    path.parent.mkdir(parents=True, exist_ok=True)
    config = load_mqtt_config()
    logger = Logger(path)
    print("logging %s from %s:%d to %s (Ctrl+C to stop)" % (TOPICS, config["host"], config["port"], path))
    logger.event("started", topics=TOPICS, host=config["host"])

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="audrey-logger-" + uuid.uuid4().hex[:8])
    client.username_pw_set(config["user"], config["password"])
    client.tls_set(ca_certs=config["ca_file"])
    client.on_connect = logger.on_connect
    client.on_disconnect = logger.on_disconnect
    client.on_message = logger.on_message
    client.reconnect_delay_set(min_delay=1, max_delay=10)
    client.connect_async(config["host"], config["port"], keepalive=30)
    client.loop_start()  # connects, reconnects and delivers messages in its own thread

    # Stop cleanly on kill too, and on Ctrl+C even when started in the background (where
    # the shell leaves SIGINT ignored).
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        while True:
            time.sleep(STATUS_S)
            print("%s  %s" % (datetime.now().strftime("%H:%M:%S"), logger.status()))
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()
        logger.event("stopped", messages=logger.messages)
        logger.close()
        print("wrote %s" % path)


if __name__ == "__main__":
    main()
