"""python -m pytest -q camera_observation      (from software/mission_control)"""

import json
import time
from datetime import datetime, timezone

import pytest

from boat_mcp.camera import Picture
from camera_observation.notes import Notes, describe
from camera_observation.observer import Lookout
from captain.captain import camera_notes_text
from telemetry_service import TelemetryProvider
from telemetry_service.mqtt_reader import TelemetryFiles, record_line

LAT, LON = 30.2443012, -95.4902590
TEL = "audrey/boat1/telemetry"


@pytest.fixture
def notes(tmp_path):
    return Notes(str(tmp_path / "notes.jsonl"), keep=5)


@pytest.fixture
def telemetry(tmp_path):
    files = TelemetryFiles(str(tmp_path / "telemetry"))

    def write(heading, at):
        data = {"gps": {"fix_valid": True, "lat_e7": round(LAT * 1e7), "lon_e7": round(LON * 1e7), "sog_kn": 1.5},
                "compass": {"virtual": {"heading": heading}}, "nav": {"state": "navigating"}}
        files.write(record_line(TEL, json.dumps(data).encode(), now=at), now=at)

    write.provider = TelemetryProvider(files.folder)
    return write


class FakeCamera:
    def __init__(self, taken_ago=1.0, error=None):
        self.taken_ago, self.error = taken_ago, error

    def latest_since(self, unix):
        if self.error:
            raise self.error
        taken = time.time() - self.taken_ago
        return Picture(b"\xff\xd8\xffjpeg", "w/p.jpg", "2026-10-30T00:00:00.000Z", taken) if taken >= unix else None


def test_notes_file(notes):
    assert notes.all() == [] and notes.latest(3) == []
    for i in range(7):
        notes.append({"status": "ok", "summary": "look %d" % i}, now=1000 + i)
    assert [n["summary"] for n in notes.all()] == ["look 2", "look 3", "look 4", "look 5", "look 6"]  # newest 5 kept
    assert [n["summary"] for n in notes.latest(2)] == ["look 5", "look 6"]
    assert [n["summary"] for n in notes.since(1004.5)] == ["look 5", "look 6"]


def test_describe():
    shot = datetime(2026, 10, 30, 15, 42, 7, tzinfo=timezone.utc).timestamp()
    note = {"unix": shot + 6, "status": "ok", "picture_taken_at": "2026-10-30T15:42:07.000+00:00",
            "boat": {"heading_deg": 90}, "summary": "a buoy ahead, nothing new",
            "hazards": [{"what": "red buoy", "relative_deg": 10, "azimuth_deg": 100, "distance_m": 30}],
            "clear_ahead_m": 25, "confidence": 0.8}
    line = describe(note, shot + 12)
    assert line.startswith("- taken 15:42:07 UTC (12 s ago), camera on course 90°: a buoy ahead, nothing new")
    assert "red buoy at +10° from the bow (azimuth 100°), ~30 m" in line and "clear ahead ~25 m" in line
    gap = describe({"unix": shot, "status": "no fresh picture: x"}, shot + 1)
    assert gap == "- 15:42:07 UTC (1 s ago): no observation (no fresh picture: x)"


def test_look_writes_a_note_with_the_heading_when_the_picture_was_taken(notes, telemetry):
    now = time.time()
    telemetry(heading=45, at=now - 5)
    telemetry(heading=80, at=now - 1.1)  # the picture is taken 1 s ago: this one
    telemetry(heading=120, at=now)  # turned since
    contexts = []

    def ask(jpeg, context, timeout):
        contexts.append(context)
        return {"summary": "open water, a buoy right of the bow", "clear_ahead_m": 40, "confidence": 0.9,
                "hazards": [{"what": "red buoy", "relative_deg": 15, "distance_m": 30}]}

    lookout = Lookout(FakeCamera(taken_ago=1.0), telemetry.provider, notes, ask=ask)
    note = lookout.look()
    assert note["status"] == "ok" and note["boat"]["heading_deg"] == 80 and note["picture"] == "w/p.jpg"
    assert note["hazards"] == [{"what": "red buoy", "relative_deg": 15, "distance_m": 30, "azimuth_deg": 95.0}]
    assert note["clear_ahead_m"] == 40 and note["confidence"] == 0.9 and 0.5 < note["picture_age_s"] < 3
    assert '"heading_deg": 80' in contexts[0] and "(none yet)" in contexts[0]
    lookout.look()
    assert "open water, a buoy right of the bow" in contexts[1]  # its own previous note, for consistency
    assert len(notes.all()) == 2


def test_look_without_a_picture_or_a_model(notes, telemetry):
    telemetry(heading=0, at=time.time())
    must_not_ask = lambda *a: pytest.fail("the model shouldn't be asked")  # noqa: E731
    assert "none taken in the last 5 s" in Lookout(FakeCamera(taken_ago=60), telemetry.provider, notes, ask=must_not_ask).look()["status"]
    assert "camera service error" in Lookout(FakeCamera(error=OSError("down")), telemetry.provider, notes, ask=must_not_ask).look()["status"]

    def broken(*a):
        raise TimeoutError("slow")

    assert Lookout(FakeCamera(), telemetry.provider, notes, ask=broken).look()["status"] == "model error: slow"
    assert len(notes.all()) == 3


def test_captain_gets_the_notes_since_its_previous_wake_up(notes):
    assert camera_notes_text(None, None, 0) == "(no lookout running)"
    assert camera_notes_text(notes, None, 0) == "(none yet)"
    notes.append({"status": "ok", "summary": "first"}, now=100)
    notes.append({"status": "ok", "summary": "second"}, now=110)
    notes.append({"status": "ok", "summary": "third"}, now=120)
    text = camera_notes_text(notes, since=105, now=125)
    assert "second" in text and "third" in text and "first" not in text
    nothing_new = camera_notes_text(notes, since=121, now=125)
    assert nothing_new.startswith("(nothing new since the previous wake-up") and "third" in nothing_new
