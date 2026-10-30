"""python -m pytest -q mission_ui      (from software/mission_control)"""

import time

import pytest
from fastapi.testclient import TestClient

from camera_observation.notes import Notes
from captain.control import CaptainLog, Control
from mission_ui.app import create_app
from telemetry_service import TelemetryProvider


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPTAIN_MISSION_FILE", str(tmp_path / "mission" / "mission.md"))
    client = TestClient(create_app(Control(str(tmp_path)), CaptainLog(str(tmp_path)),
                                   Notes(str(tmp_path / "notes.jsonl")), TelemetryProvider(str(tmp_path / "telemetry"))))
    client.log, client.notes = CaptainLog(str(tmp_path)), Notes(str(tmp_path / "notes.jsonl"))
    return client


def test_page_is_served(ui):
    r = ui.get("/")
    assert r.status_code == 200 and "Audrey mission control" in r.text


def test_mission_is_saved_to_the_file(ui, tmp_path):
    assert ui.get("/api/mission").json()["text"] == ""
    saved = ui.put("/api/mission", json={"text": "Go to the dock.\n\n"}).json()
    assert saved["text"] == "Go to the dock.\n" and saved["saved_at"]
    assert (tmp_path / "mission" / "mission.md").read_text() == "Go to the dock.\n"
    assert ui.put("/api/mission", json={"text": "x" * 20001}).status_code == 422


def test_commands(ui):
    assert ui.post("/api/control/pause").status_code == 409
    assert ui.post("/api/control/start").json()["state"] == "running"
    assert ui.get("/api/status").json()["control"]["state"] == "running"
    assert ui.post("/api/control/start").json()["detail"] == "can't start: the captain is running"
    assert ui.post("/api/control/cancel").json()["state"] == "idle"


def test_status_shows_the_log_and_notes_newest_first(ui):
    ui.log.append({"kind": "command", "command": "start"})
    ui.log.append({"kind": "wake-up", "n": 1, "said": ["Heading east."], "tool_calls": []})
    ui.notes.append({"status": "ok", "summary": "open water"}, now=time.time() - 10)
    ui.notes.append({"status": "ok", "summary": "a buoy ahead"}, now=time.time())
    status = ui.get("/api/status").json()
    assert [e["kind"] for e in status["log"]] == ["wake-up", "command"]
    assert "a buoy ahead" in status["notes"][0]["line"] and status["notes"][1]["summary"] == "open water"
    assert "none" in status["boat"]["telemetry"] and status["control"]["state"] == "idle"
