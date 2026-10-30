"""python -m pytest -q boat_mcp      (from software/mission_control)"""

import time

import pytest
from starlette.testclient import TestClient

from boat_mcp import server
from boat_mcp.boat import BoatError, destination_point
from boat_mcp.camera import Picture
from boat_mcp.observe import OBSERVE_DISTANCE_M, VisualObserver
from boat_mcp.test_boat_mcp import LAT, LON, FakeBoat, distance_m, make_boat, mcp_call

ORIGINAL = (30.2440000, -95.4900000)
BACK = {"cmd": "destination", "lat_e7": round(ORIGINAL[0] * 1e7), "lon_e7": round(ORIGINAL[1] * 1e7)}
RUN_S = 0.3


class TurningBoat(FakeBoat):
    """Each poll, the boat reports its next heading (one telemetry message), turning as listed.
    Sent to ORIGINAL, it reports "arrived" after `back_after` polls (never, if None)."""

    def __init__(self, folder, headings, back_after=3):
        super().__init__(folder)
        self.headings = list(headings)
        self.back_after, self.heading_back = back_after, 0

    def tick(self, _seconds=None):
        heading = self.headings.pop(0) if len(self.headings) > 1 else self.headings[0]
        going_back = self.destination == {"lat_e7": BACK["lat_e7"], "lon_e7": BACK["lon_e7"]}
        self.heading_back = self.heading_back + 1 if going_back else 0
        arrived = going_back and self.back_after is not None and self.heading_back >= self.back_after
        time.sleep(0.01)
        self.telemetry(heading=heading, nav_state="arrived" if arrived else "navigating")


class FakeCamera:
    """Pictures taken `taken_after_s` after the moment it is first asked about (the boat aligned),
    which the service only has from `available_after_s` after that moment."""

    def __init__(self, *plan):
        self.plan, self.start, self.asked = plan, None, 0

    def picture_between(self, start, end):
        """As the real client: the newest picture taken from start on, if taken by end."""
        self.asked += 1
        self.start = self.start or start
        now = time.time()
        pictures = [Picture(b"\xff\xd8\xffjpeg%d" % i, "w/p%d.jpg" % i, "t%d" % i, self.start + taken)
                    for i, (taken, available) in enumerate(self.plan) if now >= self.start + available]
        after = [p for p in pictures if p.taken_unix >= start]
        newest = max(after, key=lambda p: p.taken_unix) if after else None
        return newest if newest is not None and newest.taken_unix <= end else None

    def picture_url(self, name):
        return "http://camera/" + name


def observer(boat, camera, vision=lambda jpeg, note, az, timeout: "all clear", **timeouts):
    return VisualObserver(make_boat(boat), camera, vision, sleep=boat.tick, poll_s=0.02, **{
        "align_timeout_s": 2, "run_s": RUN_S, "model_timeout_s": 30, "return_timeout_s": 3, **timeouts})


def turning_boat(tmp_path, back_after=3):
    boat = TurningBoat(str(tmp_path / "telemetry"), [200, 160, 120, 95, 91, 90], back_after=back_after)
    boat.telemetry(heading=200)
    return boat


def test_observe_turns_takes_the_picture_and_goes_back(tmp_path):
    boat = turning_boat(tmp_path)
    seen = {}

    def vision(jpeg, note, azimuth, timeout):
        seen.update(jpeg=jpeg, note=note, azimuth=azimuth)
        return "a red buoy at about +10°"

    result = observer(boat, FakeCamera((0.05, 0.1)), vision).observe(*ORIGINAL, 450, "any buoys?")  # 450 = 90
    assert result["answer"] == "a red buoy at about +10°" and result["azimuth_deg"] == 90
    assert seen == {"jpeg": b"\xff\xd8\xffjpeg0", "note": "any buoys?", "azimuth": 90}
    assert result["picture"] == "w/p0.jpg" and result["picture_url"] == "http://camera/w/p0.jpg"
    assert result["back_at"] == {"lat": ORIGINAL[0], "lon": ORIGINAL[1]} and result["back_in_s"] >= 0
    assert boat.heading_back >= boat.back_after  # returned only once the boat was back
    aimed = destination_point(LAT, LON, 90, OBSERVE_DISTANCE_M)  # aimed 100 m east
    assert OBSERVE_DISTANCE_M == 100
    assert distance_m(aimed[0], aimed[1], boat.sent[0]["lat_e7"] / 1e7, boat.sent[0]["lon_e7"] / 1e7) < 0.1
    assert boat.sent[1:] == [BACK]


def test_observe_sends_the_boat_back_when_the_picture_is_in_not_after_the_whole_run(tmp_path):
    boat = turning_boat(tmp_path)
    camera = FakeCamera((0.0, 0.0))  # there at once
    result = observer(boat, camera, run_s=5).observe(*ORIGINAL, 90, "?")
    assert result["picture"] == "w/p0.jpg" and camera.asked <= 3  # didn't sit out the 5 s run


def test_observe_waits_for_a_picture_from_the_run_that_arrives_late(tmp_path):
    boat = turning_boat(tmp_path, back_after=60)  # a return long enough for the late picture
    camera = FakeCamera((0.1, RUN_S + 0.4))  # taken during the run, at the service only after it
    started = time.time()
    result = observer(boat, camera).observe(*ORIGINAL, 90, "?")
    assert result["picture"] == "w/p0.jpg" and result["answer"] == "all clear"
    assert boat.sent[-1] == BACK and time.time() - started > RUN_S  # sent back first, picture later


def test_observe_ignores_a_picture_taken_after_the_run(tmp_path):
    boat = turning_boat(tmp_path)
    camera = FakeCamera((RUN_S + 0.1, RUN_S + 0.1))  # taken on the way back: not of the azimuth
    with pytest.raises(BoatError, match="no camera picture taken during the 0.3 s on course at 90°; the boat is back"):
        observer(boat, camera).observe(*ORIGINAL, 90, "?")
    assert boat.sent[1:] == [BACK]  # sent back, no halt in between


def test_observe_boat_never_turns(tmp_path):
    boat = TurningBoat(str(tmp_path / "telemetry"), [200])
    boat.telemetry(heading=200)
    with pytest.raises(BoatError, match="didn't turn to 90°.*it is back at original_gps"):
        observer(boat, FakeCamera((0, 0)), align_timeout_s=0.3).observe(*ORIGINAL, 90, "?")
    assert boat.sent[-1] == BACK


def test_observe_model_timeout(tmp_path):
    boat = turning_boat(tmp_path)

    def slow(*args):
        raise TimeoutError("took too long")

    with pytest.raises(BoatError, match="didn't answer within 30 s; the boat is back at original_gps"):
        observer(boat, FakeCamera((0.05, 0.05)), slow).observe(*ORIGINAL, 90, "?")


def test_observe_boat_does_not_get_back(tmp_path):
    boat = turning_boat(tmp_path, back_after=None)
    with pytest.raises(BoatError, match="hasn't arrived within 0.5 s; the vision model's answer: all clear"):
        observer(boat, FakeCamera((0.05, 0.05)), return_timeout_s=0.5).observe(*ORIGINAL, 90, "?")


def test_observe_answers_while_the_boat_goes_back(tmp_path):
    boat = turning_boat(tmp_path)
    asked_at = []
    observer(boat, FakeCamera((0.05, 0.05)), lambda *a: asked_at.append(boat.heading_back) or "ok").observe(*ORIGINAL, 90, "?")
    assert asked_at[0] < boat.back_after  # asked before the boat had arrived


def test_observe_refuses_a_bad_original(tmp_path):
    boat = turning_boat(tmp_path)
    with pytest.raises(BoatError, match="isn't a position"):
        observer(boat, FakeCamera()).observe(123, 0, 90, "?")
    assert boat.sent == []


def test_mcp_visual_observe(tmp_path, monkeypatch):
    boat = turning_boat(tmp_path)
    monkeypatch.setattr(server, "_boat", make_boat(boat))
    monkeypatch.setattr(server, "_observer", observer(boat, FakeCamera((0.05, 0.05))))
    with TestClient(server.app()) as c:
        tools = {t["name"]: t for t in mcp_call(c, "tools/list", {})["tools"]}
        assert "STOPS THE BOAT" in tools["visual_observe"]["description"]
        assert set(tools["visual_observe"]["inputSchema"]["required"]) == {"original_gps", "azimuth", "request_note"}
        out = mcp_call(c, "tools/call", {"name": "visual_observe", "arguments": {
            "original_gps": {"lat": ORIGINAL[0], "lon": ORIGINAL[1]}, "azimuth": 90, "request_note": "any buoys?"}})
        assert not out.get("isError"), out
        assert out["structuredContent"]["answer"] == "all clear"
