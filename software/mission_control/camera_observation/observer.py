"""The lookout: every CAMERA_OBS_PERIOD_S (10 s) it looks at the camera's latest picture and writes a
note on what is around the boat, for the captain (notes.py has the file).

    python -m camera_observation.observer        (from software/mission_control)

Runs as its own process (supervisord.conf). Each look:

    1. takes the newest streamed picture from the camera service, if it was taken in the last
       MAX_PICTURE_AGE_S (otherwise writes a "no fresh picture" note: the captain knows it is blind)
    2. notes what the boat was doing when it was taken (heading, position, speed: the telemetry
       closest to that moment)
    3. shows the picture (with its course marks), that, and its own previous notes (the last
       HISTORY_NOTES, about a minute) to the vision model, which answers in a fixed form: a
       summary, the hazards with their direction from the bow and rough distance, how far ahead
       looks clear, and a confidence. With its previous notes it keeps one consistent picture of
       the surroundings: what is new, what has gone, what is still there.
    4. works out each hazard's azimuth from the heading (geo.py, not the model) and appends the note

Looks never overlap: one that runs long is followed by the next straight away. The model is in
Azure AI Foundry (FOUNDRY_OPENAI_URL, FOUNDRY_MODEL; boat_mcp.vision signs in). Settings:
CAMERA_OBS_PERIOD_S, CAMERA_NOTES_FILE, CAMERA_SERVICE_URL, TELEMETRY_DIR.
"""

import base64
import json
import logging
import os
import signal
import threading
import time


import geo
import tracing
from boat_mcp.boat import TELEMETRY_TOPIC
from camera_observation.notes import Notes, describe
from captain.state import boat_state, state_from_message
from telemetry_service import TelemetryProvider

log = logging.getLogger("camera_observation")
DEFAULT_PERIOD_S = 10.0
MAX_PICTURE_AGE_S = 5.0  # an older picture isn't a look at now (the camera may be off)
HISTORY_NOTES = 6  # previous notes the model sees: about a minute
MODEL_TIMEOUT_S = 25.0
TELEMETRY_MATCH_S = 2.0  # telemetry this close to the picture describes the boat when it was taken

INSTRUCTIONS = """You are the lookout on Audrey, a small autonomous boat, looking through her fixed
forward camera every few seconds. The captain, another AI, steers by your notes, so be accurate,
consistent and brief.

The camera looks straight ahead over the bow and covers about 43 degrees either side. Red boxes
on the picture mark -25° (left), 0° (straight ahead) and +25° (right) from the bow. You get the
boat's heading, speed and position when the picture was taken, and your own previous notes from
the last minute or so.

Report what matters for steering the boat safely: shores and banks, piers, bridges, moored or
moving boats, buoys, anything floating, people in the water, shallows or weeds, and the open
water ahead. For each hazard give its direction in degrees from the bow (+ right, - left) and a
rough distance in metres (a judgement from the picture; say if very unsure). Say how far ahead
the water looks clear along the bow, or null if you can't tell.

Use your previous notes to keep a consistent picture: say when something is new, has moved, or
is no longer in view, and don't re-describe the unchanged scene at length. The boat turns and
moves between pictures; the headings tell you how. Don't invent things the picture doesn't show:
if it is dark, blurred or unclear, say so and lower the confidence."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "one or two sentences: what is ahead and what changed"},
        "hazards": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "what": {"type": "string"},
                "relative_deg": {"type": "number", "description": "degrees from the bow, + right, - left"},
                "distance_m": {"type": ["number", "null"], "description": "rough distance in metres, null if unknown"},
            },
            "required": ["what", "relative_deg", "distance_m"], "additionalProperties": False}},
        "clear_ahead_m": {"type": ["number", "null"], "description": "how far ahead along the bow looks clear"},
        "confidence": {"type": "number", "description": "0 to 1"},
    },
    "required": ["summary", "hazards", "clear_ahead_m", "confidence"],
    "additionalProperties": False,
}


def ask_model(jpeg, context, timeout_s):
    """The model's look at the picture: a dict in RESPONSE_SCHEMA's form."""
    from boat_mcp.vision import DEFAULT_MODEL, client

    picture = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    started = time.monotonic()
    response = client().with_options(timeout=timeout_s, max_retries=0).responses.create(
        model=os.environ.get("FOUNDRY_MODEL", DEFAULT_MODEL),
        instructions=INSTRUCTIONS,
        input=[{"role": "user", "content": [{"type": "input_text", "text": context},
                                            {"type": "input_image", "image_url": picture, "detail": "high"}]}],
        text={"format": {"type": "json_schema", "name": "lookout_note", "schema": RESPONSE_SCHEMA, "strict": True}},
    )
    tracing.event("model-call", purpose="lookout", model=response.model, tokens=tracing.usage_of(response),
                  model_s=round(time.monotonic() - started, 2))
    return json.loads(response.output_text)


class Lookout:
    def __init__(self, camera, telemetry, notes, ask=ask_model, clock=time.time, period_s=DEFAULT_PERIOD_S,
                 max_picture_age_s=MAX_PICTURE_AGE_S, history=HISTORY_NOTES):
        self.camera, self.telemetry, self.notes, self.ask = camera, telemetry, notes, ask
        self.clock, self.period_s = clock, period_s
        self.max_picture_age_s, self.history = max_picture_age_s, history

    def boat_when(self, unix):
        """The boat's position, heading and speed at about `unix` (the telemetry closest to it),
        or as now if there is none that close."""
        messages = self.telemetry.messages(TELEMETRY_TOPIC)
        closest = min(messages, key=lambda m: abs(m.get("unix", 0) - unix), default=None)
        if closest is not None and abs(closest.get("unix", 0) - unix) <= TELEMETRY_MATCH_S:
            state = state_from_message(closest, closest["unix"])
        else:
            state = boat_state(self.telemetry, now=self.clock())
        return {k: state.get(k) for k in ("position", "heading_deg", "speed_m_s", "autopilot")}

    def look(self):
        """One look: writes a note and returns it."""
        now = self.clock()
        try:
            picture = self.camera.latest_since(now - self.max_picture_age_s)
        except Exception as e:
            note = self.notes.append({"status": "no fresh picture: camera service error (%s)" % e}, now)
            tracing.event("look", status=note["status"])
            return note
        if picture is None:
            return self.notes.append({"status": "no fresh picture: none taken in the last %g s" % self.max_picture_age_s}, now)

        boat = self.boat_when(picture.taken_unix)
        previous = self.notes.latest(self.history)
        context = "The boat when the picture was taken: %s\n\nYour previous notes, oldest first:\n%s" % (
            json.dumps(boat), "\n".join(describe(n, now) for n in previous) or "(none yet)")
        asked = time.monotonic()
        try:
            seen = self.ask(picture.jpeg, context, MODEL_TIMEOUT_S)
        except Exception as e:
            note = self.notes.append({"status": "model error: %s" % e, "picture": picture.name}, self.clock())
            tracing.event("look", status=note["status"], picture=picture.name, picture_taken_at=picture.taken_at, boat=boat,
                          context=context, model_s=round(time.monotonic() - asked, 2))
            return note
        model_s = round(time.monotonic() - asked, 2)

        heading = boat.get("heading_deg")
        hazards = []
        for h in seen.get("hazards") or []:
            hazard = {"what": h.get("what"), "relative_deg": h.get("relative_deg"), "distance_m": h.get("distance_m")}
            if heading is not None and h.get("relative_deg") is not None:
                hazard["azimuth_deg"] = round(geo.azimuth_from_relative(heading, h["relative_deg"]), 1)
            hazards.append(hazard)
        written = self.clock()
        tracing.event("look", status="ok", picture=picture.name, picture_taken_at=picture.taken_at, boat=boat, context=context,
                      answer=seen, model_s=model_s)
        return self.notes.append({
            "status": "ok", "picture": picture.name, "picture_taken_at": picture.taken_at,
            "picture_age_s": round(written - picture.taken_unix, 1), "boat": boat,
            "summary": seen.get("summary", ""), "hazards": hazards, "clear_ahead_m": seen.get("clear_ahead_m"),
            "confidence": min(1.0, max(0.0, float(seen.get("confidence", 0)))),
        }, written)

    def run(self, stopping):
        next_at = time.monotonic()
        while not stopping.wait(max(0.0, next_at - time.monotonic())):
            next_at = time.monotonic() + self.period_s
            try:
                note = self.look()
                log.info("%s", describe(note, self.clock()))
            except Exception:
                log.exception("a look failed")
            next_at = max(next_at, time.monotonic())  # ran long: the next one now


def main():
    from boat_mcp.camera import CameraClient

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "azure"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    period_s = float(os.environ.get("CAMERA_OBS_PERIOD_S") or DEFAULT_PERIOD_S)
    notes = Notes()
    lookout = Lookout(CameraClient(), TelemetryProvider(), notes, period_s=period_s)
    log.info("looking every %g s; notes in %s", period_s, notes.path)
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda signum, frame: stopping.set())
    signal.signal(signal.SIGINT, lambda signum, frame: stopping.set())
    lookout.run(stopping)
    log.info("stopped")


if __name__ == "__main__":
    main()
