"""TelemetryProvider: the latest telemetry for the mission control processes, read from the
folder mqtt_reader.py writes (files.py has the layout).

    from telemetry_service import TelemetryProvider

    telemetry = TelemetryProvider()                  # TELEMETRY_DIR, or /tmp/audrey-telemetry
    state = telemetry.latest_data("audrey/boat1/telemetry")     # the newest message's data, or None
    recent = telemetry.since(time.time() - 30)       # everything from the last 30 s, oldest first

Every request first catches up with the folder: it reads only what was added to each file since
the last request (a line still being written, without its newline yet, is left for next time),
and forgets the messages of files the reader has deleted. So it holds what is on disk, nothing
older, and reading is cheap however often it is asked.

Topic filters work as in MQTT: "audrey/boat1/telemetry", "audrey/boat1/#", "audrey/+/cmd".
"""

import json
import os
import threading

from telemetry_service.files import telemetry_dir, telemetry_files


def topic_matches(pattern, topic):
    """MQTT topic filter matching: + is one level, # (last) is any number of levels."""
    if pattern is None:
        return True
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(p) == len(t)


class TelemetryProvider:
    def __init__(self, folder=None):
        self.folder = folder or telemetry_dir()
        self._files = {}  # file name -> {"offset": bytes read, "messages": [...] in file order}
        self._lock = threading.Lock()

    def refresh(self):
        """Catches up with the folder. Called by every query; call it yourself only to preload."""
        with self._lock:
            names = telemetry_files(self.folder)
            for gone in set(self._files) - set(names):
                del self._files[gone]  # deleted by the reader: forget its messages
            for name in names:
                self._read_new(name)

    def _read_new(self, name):
        state = self._files.setdefault(name, {"offset": 0, "messages": []})
        path = os.path.join(self.folder, name)
        try:
            size = os.path.getsize(path)
            if size < state["offset"]:  # the file was replaced: start it again
                state["offset"], state["messages"] = 0, []
            if size == state["offset"]:
                return
            with open(path, "rb") as f:
                f.seek(state["offset"])
                chunk = f.read(size - state["offset"])
        except FileNotFoundError:
            self._files.pop(name, None)  # deleted between listing and reading
            return
        complete = chunk.rfind(b"\n") + 1  # only whole lines; the rest is still being written
        for line in chunk[:complete].splitlines():
            try:
                state["messages"].append(json.loads(line))
            except ValueError:
                pass  # not a line of ours: skip it
        state["offset"] += complete

    def messages(self, topic=None):
        """Everything on disk, oldest first, optionally only one topic filter's."""
        self.refresh()
        with self._lock:
            return [m for name in sorted(self._files) for m in self._files[name]["messages"]
                    if topic_matches(topic, m.get("topic", ""))]

    def since(self, unix, topic=None):
        """Messages received after `unix` (seconds), oldest first."""
        return [m for m in self.messages(topic) if m.get("unix", 0) > unix]

    def latest(self, topic=None, n=1):
        """The newest n messages, newest first."""
        return self.messages(topic)[::-1][:n]

    def latest_data(self, topic=None):
        """The newest message's data (the parsed JSON payload), or None if there is none."""
        newest = self.latest(topic, 1)
        return newest[0].get("data") if newest else None
