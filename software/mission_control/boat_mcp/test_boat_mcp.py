"""python -m pytest -q boat_mcp      (from software/mission_control)"""

import json
import math
import time

import pytest
from starlette.testclient import TestClient

from boat_mcp import server
from boat_mcp.boat import Boat, BoatError, destination_point
from telemetry_service import TelemetryProvider
from telemetry_service.mqtt_reader import TelemetryFiles, record_line

LAT, LON = 30.2443012, -95.4902590
TEL = "audrey/boat1/telemetry"


def distance_m(lat1, lon1, lat2, lon2):
    x = math.radians(lon2 - lon1) * math.cos(math.radians(lat1)) * 6371000
    y = math.radians(lat2 - lat1) * 6371000
    return math.hypot(x, y)


class FakeBoat:
    """Telemetry in a folder, as the reader writes it, and a boat that echoes its commands."""

    def __init__(self, folder, echo=True):
        self.files = TelemetryFiles(folder)
        self.echo = echo
        self.sent = []
        self.destination = None

    def telemetry(self, fix=True, age_s=0.0, heading=None, nav_state=None, **extra):
        data = {"gps": {"fix_valid": fix, "lat_e7": round(LAT * 1e7), "lon_e7": round(LON * 1e7), "age_ms": 0},
                "destination": self.destination, "nav": {"state": nav_state}, **extra}
        if heading is not None:
            data["compass"] = {"heading": heading, "virtual": {"heading": heading}}
        now = time.time() - age_s
        self.files.write(record_line(TEL, json.dumps(data).encode(), now=now), now=now)

    def publish(self, command):
        self.sent.append(command)
        if not self.echo:
            return
        if command["cmd"] == "destination":
            self.destination = {"lat_e7": command["lat_e7"], "lon_e7": command["lon_e7"]}
        elif command["cmd"] == "clear_destination":
            self.destination = None
        time.sleep(0.01)
        self.telemetry()


@pytest.fixture
def fake(tmp_path):
    return FakeBoat(str(tmp_path / "telemetry"))


def make_boat(fake):
    return Boat(fake.publish, TelemetryProvider(fake.files.folder), confirm_s=0.5, poll_s=0.02)


def test_destination_point():
    north = destination_point(LAT, LON, 0, 100)
    assert abs(distance_m(LAT, LON, *north) - 100) < 0.1 and north[0] > LAT and abs(north[1] - LON) < 1e-9
    east = destination_point(LAT, LON, 90, 100)
    assert abs(distance_m(LAT, LON, *east) - 100) < 0.1 and east[1] > LON
    sw = destination_point(LAT, LON, 225, 50)
    assert sw[0] < LAT and sw[1] < LON and abs(distance_m(LAT, LON, *sw) - 50) < 0.1


def test_start_navigation_sends_the_destination_and_confirms(fake):
    fake.telemetry()
    result = make_boat(fake).start_navigation(450, 60)  # 450 = 90: east
    assert result["accepted"] is True and result["azimuth_deg"] == 90 and result["distance_m"] == 60
    command = fake.sent[-1]
    assert command["cmd"] == "destination" and type(command["lat_e7"]) is int and type(command["lon_e7"]) is int
    assert abs(distance_m(LAT, LON, command["lat_e7"] / 1e7, command["lon_e7"] / 1e7) - 60) < 0.1
    assert result["start"]["lat"] == pytest.approx(LAT) and result["destination"]["lon"] > LON


def test_start_navigation_not_confirmed(tmp_path):
    fake = FakeBoat(str(tmp_path / "telemetry"), echo=False)
    fake.telemetry()
    result = make_boat(fake).start_navigation(0, 20)
    assert result["accepted"] is False and "hasn't shown it" in result["note"] and len(fake.sent) == 1


def test_start_navigation_refuses(fake):
    boat = make_boat(fake)
    with pytest.raises(BoatError, match="no telemetry"):
        boat.start_navigation(0, 20)
    fake.telemetry(fix=False)
    with pytest.raises(BoatError, match="no GPS fix"):
        boat.start_navigation(0, 20)
    fake.telemetry()
    for bad in (1, 10000):
        with pytest.raises(BoatError, match="distance"):
            boat.start_navigation(0, bad)
    assert fake.sent == []  # nothing was sent for any of them


def test_stale_fix_is_refused(fake):
    fake.telemetry(age_s=30)
    with pytest.raises(BoatError, match="old"):
        make_boat(fake).start_navigation(0, 20)


def test_navigate_to_a_position(fake):
    fake.telemetry()
    target = destination_point(LAT, LON, 300, 45)
    result = make_boat(fake).navigate_to(*target)
    assert result["accepted"] is True and abs(result["distance_m"] - 45) < 0.1 and abs(result["azimuth_deg"] - 300) < 0.1
    assert fake.sent[-1] == {"cmd": "destination", "lat_e7": round(target[0] * 1e7), "lon_e7": round(target[1] * 1e7)}
    for too_far_or_near in (destination_point(LAT, LON, 0, 2), destination_point(LAT, LON, 0, 400)):
        with pytest.raises(BoatError, match="m away"):
            make_boat(fake).navigate_to(*too_far_or_near)
    with pytest.raises(BoatError, match="not a position"):
        make_boat(fake).navigate_to(100, 0)


def test_stop_clears_the_destination(fake):
    fake.destination = {"lat_e7": 1, "lon_e7": 2}
    fake.telemetry()
    result = make_boat(fake).stop()
    assert result["accepted"] is True and fake.sent == [{"cmd": "clear_destination"}]


# ---- MCP ---------------------------------------------------------------------------------------

@pytest.fixture
def mcp_client(fake, monkeypatch):
    monkeypatch.setattr(server, "_boat", make_boat(fake))
    with TestClient(server.app()) as c:
        yield c


def mcp_call(c, method, params):
    r = c.post("/mcp", headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
               json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_mcp_tools(mcp_client, fake):
    tools = {t["name"]: t for t in mcp_call(mcp_client, "tools/list", {})["tools"]}
    assert set(tools) >= {"start_navigation", "navigate_to", "stop", "visual_observe"}
    assert "clockwise from true north" in tools["start_navigation"]["description"]
    assert set(tools["start_navigation"]["inputSchema"]["required"]) == {"azimuth", "distance"}
    fake.telemetry()
    out = mcp_call(mcp_client, "tools/call", {"name": "start_navigation", "arguments": {"azimuth": 180, "distance": 40}})
    assert not out.get("isError"), out
    assert out["structuredContent"]["accepted"] is True and out["structuredContent"]["destination"]["lat"] < LAT
    out = mcp_call(mcp_client, "tools/call", {"name": "stop", "arguments": {}})
    assert out["structuredContent"]["accepted"] is True


def test_mcp_tool_error(mcp_client):
    out = mcp_call(mcp_client, "tools/call", {"name": "start_navigation", "arguments": {"azimuth": 0, "distance": 20}})
    assert out["isError"] and "no telemetry" in out["content"][0]["text"]
