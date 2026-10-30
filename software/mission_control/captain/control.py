"""What the mission UI tells the captain, and what the captain tells it back: two small files in
CAPTAIN_STATE_DIR (default /tmp/audrey-captain).

    control.json   the captain's state, written by the UI (commands) and read by the captain on
                   every tick:
                     idle      no mission running: the captain doesn't wake up (no model calls)
                     running   it wakes up every 15-20 s with the mission
                     paused    the boat was stopped and the captain doesn't wake up; resuming goes
                               on with the same conversation
                   with the run's id (a new one on every start), when and why it last changed.
    log.jsonl      the captain's log, newest last (the newest KEEP_LOG entries): one entry per
                   wake-up (time, the boat state it saw, its tool calls and their results, what it
                   said, how long it took) and one per command (start, pause, ...).

Both are replaced whole on every change (a temporary file, then a rename), so a reader never
sees half of one.
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

DEFAULT_DIR = "/tmp/audrey-captain"
KEEP_LOG = 300
IDLE, RUNNING, PAUSED = "idle", "running", "paused"
# command -> (states it is allowed from, the state it leads to)
COMMANDS = {
    "start": ((IDLE,), RUNNING),
    "pause": ((RUNNING,), PAUSED),
    "resume": ((PAUSED,), RUNNING),
    "end": ((RUNNING, PAUSED), IDLE),
    "cancel": ((RUNNING, PAUSED), IDLE),
}


def state_dir():
    return os.environ.get("CAPTAIN_STATE_DIR") or DEFAULT_DIR


def _iso(unix):
    return datetime.fromtimestamp(unix, timezone.utc).isoformat(timespec="milliseconds")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


class Control:
    def __init__(self, folder=None):
        self.folder = folder or state_dir()
        self.path = os.path.join(self.folder, "control.json")
        self._lock = threading.Lock()

    def read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {"state": IDLE, "run_id": None, "changed_at": None, "command": None, "unix": 0}

    def command(self, name, now=None):
        """Applies a command; returns the new control dict. Raises ValueError if it doesn't apply."""
        if name not in COMMANDS:
            raise ValueError("unknown command %r" % name)
        allowed, new_state = COMMANDS[name]
        now = time.time() if now is None else now
        with self._lock:
            current = self.read()
            if current["state"] not in allowed:
                raise ValueError("can't %s: the captain is %s" % (name, current["state"]))
            control = {"state": new_state, "command": name, "unix": round(now, 3), "changed_at": _iso(now),
                       "run_id": uuid.uuid4().hex[:8] if name == "start" else current.get("run_id")}
            _write(self.path, json.dumps(control))
            return control


class CaptainLog:
    def __init__(self, folder=None, keep=KEEP_LOG):
        self.path = os.path.join(folder or state_dir(), "log.jsonl")
        self.keep = keep
        self._lock = threading.Lock()

    def entries(self, n=None):
        """The newest n entries (all if None), oldest first."""
        try:
            with open(self.path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            return []
        entries = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except ValueError:
                pass
        return entries if n is None else entries[-n:]

    def append(self, entry, now=None):
        now = time.time() if now is None else now
        entry = {"t": _iso(now), "unix": round(now, 3), **entry}
        with self._lock:
            entries = (self.entries() + [entry])[-self.keep:]
            _write(self.path, "".join(json.dumps(e, ensure_ascii=False, default=str) + "\n" for e in entries))
        return entry
