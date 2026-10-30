"""The camera notes file: what the lookout (observer.py) saw, for the captain to read.

    CAMERA_NOTES_FILE (default /tmp/audrey-camera-notes.jsonl), one JSON object per line, oldest
    first, the newest KEEP_NOTES kept:

    {"t": "2026-10-30T15:42:07.123+00:00", "unix": 1793..., "status": "ok",
     "picture": "<camera service name>", "picture_taken_at": "...", "picture_age_s": 1.2,
     "boat": {"position": {...}, "heading_deg": 92.0, "speed_m_s": 0.8},
     "summary": "...", "clear_ahead_m": 40, "confidence": 0.8,
     "hazards": [{"what": "red buoy", "relative_deg": 10, "azimuth_deg": 102.0, "distance_m": 30}]}

    "status" is "ok", or what went wrong ("no fresh picture: ...", "model error: ..."), and then
    the note has no summary or hazards.

Written whole and replaced (a temporary file, then a rename), so a reader never sees half a note.
"""

import json
import os
import threading
from datetime import datetime, timezone

DEFAULT_FILE = "/tmp/audrey-camera-notes.jsonl"
KEEP_NOTES = 100


def notes_file():
    return os.environ.get("CAMERA_NOTES_FILE") or DEFAULT_FILE


class Notes:
    def __init__(self, path=None, keep=KEEP_NOTES):
        self.path = path or notes_file()
        self.keep = keep
        self._lock = threading.Lock()

    def all(self):
        """Every note in the file, oldest first."""
        try:
            with open(self.path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            return []
        notes = []
        for line in lines:
            try:
                notes.append(json.loads(line))
            except ValueError:
                pass
        return notes

    def latest(self, n=1):
        """The newest n notes, oldest first."""
        return self.all()[-n:] if n > 0 else []

    def since(self, unix):
        """Notes written after `unix`, oldest first."""
        return [note for note in self.all() if note.get("unix", 0) > unix]

    def append(self, note, now):
        note = {"t": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds"), "unix": round(now, 3), **note}
        with self._lock:
            notes = (self.all() + [note])[-self.keep:]
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("".join(json.dumps(n, separators=(",", ":"), ensure_ascii=False) + "\n" for n in notes))
            os.replace(tmp, self.path)
        return note


def describe(note, now):
    """One line for the captain (and the lookout's own memory): when the picture was taken, in UTC
    and how long before `now`, the course the boat (and so the camera) was on, and what was seen."""
    if note.get("status") != "ok":
        when = datetime.fromtimestamp(note.get("unix", now), timezone.utc)
        return "- %s UTC (%.0f s ago): no observation (%s)" % (when.strftime("%H:%M:%S"), now - note.get("unix", now), note.get("status"))
    taken_at = note.get("picture_taken_at")
    taken = datetime.fromisoformat(taken_at).timestamp() if taken_at else note.get("unix", now)
    heading = (note.get("boat") or {}).get("heading_deg")
    parts = ["- taken %s UTC (%.0f s ago), camera on course %s: %s" % (
        datetime.fromtimestamp(taken, timezone.utc).strftime("%H:%M:%S"), now - taken,
        "%.0f°" % heading if heading is not None else "unknown", note.get("summary", ""))]
    hazards = note.get("hazards") or []
    if hazards:
        parts.append("hazards: " + "; ".join(
            "%s at %+.0f° from the bow%s%s" % (
                h.get("what", "?"), h.get("relative_deg", 0),
                " (azimuth %.0f°)" % h["azimuth_deg"] if h.get("azimuth_deg") is not None else "",
                ", ~%.0f m" % h["distance_m"] if h.get("distance_m") is not None else "")
            for h in hazards))
    if note.get("clear_ahead_m") is not None:
        parts.append("clear ahead ~%.0f m" % note["clear_ahead_m"])
    if note.get("confidence") is not None:
        parts.append("confidence %.1f" % note["confidence"])
    return "; ".join(parts)
