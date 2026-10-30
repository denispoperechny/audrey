"""The mission UI: write the mission, start / pause / resume / end / cancel the captain, and watch
what is going on (the boat's state, the captain's decisions, the lookout's notes).

    python -m mission_ui.app          (from software/mission_control)
    -> http://localhost:8200          (MISSION_UI_PORT to change it; no auth)

Runs as its own process (supervisord.conf). It works only through files the other processes
share: the mission file (CAPTAIN_MISSION_FILE), the captain's control and log files
(captain/control.py, CAPTAIN_STATE_DIR), the lookout's notes (CAMERA_NOTES_FILE) and the
telemetry folder (TELEMETRY_DIR).

    GET  /api/status                 control state, boat state, the captain's log and the
                                     lookout's notes (newest first)
    GET  /api/mission                the mission text
    PUT  /api/mission  {"text": ..}  saves it (the captain reads it afresh on every wake-up)
    POST /api/control/<command>      start | pause | resume | end | cancel (409 if it doesn't apply)
"""

import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from camera_observation.notes import Notes, describe
from captain.captain import DEFAULT_MISSION_FILE
from captain.control import CaptainLog, Control
from captain.state import boat_state
from telemetry_service import TelemetryProvider

DEFAULT_PORT = 8200
LOG_ENTRIES = 60
NOTES_SHOWN = 30
MAX_MISSION_CHARS = 20000


def mission_path():
    return Path(os.environ.get("CAPTAIN_MISSION_FILE") or DEFAULT_MISSION_FILE)


class MissionText(BaseModel):
    text: str = Field(max_length=MAX_MISSION_CHARS)


def note_line(note, now):
    try:
        return describe(note, now)
    except Exception as e:  # one odd note mustn't break the page
        return "- (unreadable note: %s) %s" % (e, note.get("summary") or note.get("status") or "")


def create_app(control=None, captain_log=None, notes=None, telemetry=None):
    control = control or Control()
    captain_log = captain_log or CaptainLog()
    notes = notes or Notes()
    telemetry = telemetry or TelemetryProvider()
    app = FastAPI(title="Audrey mission control", docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.get("/api/status")
    def status():
        now = time.time()
        shown = notes.latest(NOTES_SHOWN)[::-1]
        return {
            "now": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
            "control": control.read(),
            "boat": boat_state(telemetry, now),
            "log": captain_log.entries(LOG_ENTRIES)[::-1],
            "notes": [{"line": note_line(n, now), **n} for n in shown],
        }

    @app.get("/api/mission")
    def get_mission():
        path = mission_path()
        try:
            text = path.read_text(encoding="utf-8")
            saved = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        except FileNotFoundError:
            text, saved = "", None
        return {"text": text, "saved_at": saved, "path": str(path)}

    @app.put("/api/mission")
    def put_mission(mission: MissionText):
        path = mission_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(mission.text.rstrip() + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return get_mission()

    @app.post("/api/control/{command}")
    def command(command: str):
        try:
            return control.command(command)
        except ValueError as e:
            raise HTTPException(409, str(e))

    app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent / "static", html=True), name="page")
    return app


def main():
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("MISSION_UI_PORT") or DEFAULT_PORT), log_level="warning")


if __name__ == "__main__":
    main()
