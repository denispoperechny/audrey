"""visual_observe: turn the boat to a direction, take the camera's picture of it, bring the boat
back, and have a vision model answer a request about the picture.

    1. Sends the boat towards `azimuth`: to a point OBSERVE_DISTANCE_M along it from where it is,
       only to give it a direction; it turns back long before getting there.
    2. Waits for its telemetry to show the heading within ALIGN_TOLERANCE_DEG of the azimuth: the
       moment it is "aligned". Not within ALIGN_TIMEOUT_S: back to `original`, error.
    3. The run: the boat carries on along the azimuth for at most RUN_S, while the camera service
       is polled (every poll_s, faster than the camera takes pictures) for a picture taken during
       the run: from the aligned moment to RUN_S after.
    4. As soon as there is one, or when the run is over, the boat is sent back to `original`. A
       picture taken during the run can still arrive after that (it was being uploaded), so the
       polling goes on while the boat returns; a picture taken after the run doesn't count.
    5. With the picture, the vision model is asked about it, while the boat goes back (no answer
       within MODEL_TIMEOUT_S: error).
    6. Returns only once the boat is back: its telemetry shows `original` as its destination and
       "arrived" (within its 4 m arrival radius). Not back within RETURN_TIMEOUT_S: error, with
       the model's answer in the message if there was one. No picture from the run by then: error.

So the boat never goes further than the turn plus RUN_S along the azimuth. Every way out (the
answer, or any of the errors) waits for the boat to be back first. Its earlier destination is
replaced: after this it holds still at `original` until it is sent on (start_navigation).
"""

import time
from concurrent.futures import ThreadPoolExecutor

import tracing
from boat_mcp.boat import TELEMETRY_TOPIC, BoatError
from geo import destination_point, wrap180

OBSERVE_DISTANCE_M = 100.0  # how far along the azimuth the boat is aimed (a direction to steer by)
ALIGN_TOLERANCE_DEG = 5.0  # the heading within this of the azimuth counts as "on course"
ALIGN_TIMEOUT_S = 10.0  # to get on course; then it is brought back and the tool reports an error
RUN_S = 4.0  # on course for at most this long, for the camera to take the picture
MODEL_TIMEOUT_S = 30.0
RETURN_TIMEOUT_S = 60.0  # to get back to `original`


def heading_of(data):
    """The heading the boat steers by (its virtual compass), or the magnetic one, or None."""
    compass = data.get("compass") or {}
    heading = (compass.get("virtual") or {}).get("heading")
    return heading if heading is not None else compass.get("heading")


class VisualObserver:
    def __init__(self, boat, camera, vision, clock=time.time, sleep=time.sleep, poll_s=0.25,
                 align_timeout_s=ALIGN_TIMEOUT_S, run_s=RUN_S, model_timeout_s=MODEL_TIMEOUT_S,
                 return_timeout_s=RETURN_TIMEOUT_S):
        self.boat = boat  # boat.Boat
        self.camera = camera  # camera.CameraClient: picture_between(start, end), picture_url(name)
        self.vision = vision  # vision.ask(jpeg, request_note, azimuth_deg, timeout_s) -> text
        self.clock, self.sleep, self.poll_s = clock, sleep, poll_s
        self.align_timeout_s, self.run_s = align_timeout_s, run_s
        self.model_timeout_s, self.return_timeout_s = model_timeout_s, return_timeout_s

    def observe(self, original_lat, original_lon, azimuth_deg, request_note):
        if not (-90 <= original_lat <= 90 and -180 <= original_lon <= 180):
            raise BoatError("original_gps isn't a position: %r, %r" % (original_lat, original_lon))
        original = (original_lat, original_lon)
        azimuth_deg %= 360
        lat, lon, _ = self.boat.position()
        started = self.clock()
        self.boat.send_destination(*destination_point(lat, lon, azimuth_deg, OBSERVE_DISTANCE_M))
        tracing.event("observe", phase="turning", azimuth_deg=azimuth_deg, request_note=request_note,
                      position={"lat": lat, "lon": lon}, original={"lat": original_lat, "lon": original_lon})

        aligned_at = self._wait_aligned(started, azimuth_deg)
        tracing.event("observe", phase="aligned" if aligned_at else "not aligned",
                      after_s=round(aligned_at - started, 2) if aligned_at else None)
        if aligned_at is None:
            raise BoatError("the boat didn't turn to %.0f° within %g s; %s"
                            % (azimuth_deg, self.align_timeout_s, self._bring_back(original)))

        # The run: on course until a picture taken during it is in, or the run is over.
        run_end = aligned_at + self.run_s
        problem = []
        picture = self._poll_picture(aligned_at, run_end, problem)
        while picture is None and self.clock() < run_end:
            self.sleep(self.poll_s)
            picture = self._poll_picture(aligned_at, run_end, problem)

        # Back to `original`; meanwhile the model looks at the picture, and if there isn't one
        # yet, the polling for one taken during the run goes on.
        back_sent = self.clock()
        self.boat.send_destination(*original)
        tracing.event("observe", phase="going back", picture=picture.name if picture else None,
                      run_s=round(back_sent - aligned_at, 2))
        with ThreadPoolExecutor(max_workers=1) as pool:
            asked = None if picture is None else self._ask(pool, picture, request_note, azimuth_deg)
            back_at = None
            while True:
                if picture is None:
                    picture = self._poll_picture(aligned_at, run_end, problem)
                    if picture is not None:
                        tracing.event("observe", phase="late picture", picture=picture.name)
                        asked = self._ask(pool, picture, request_note, azimuth_deg)
                back_at = self._arrived_since(back_sent, original)
                if back_at is not None or self.clock() - back_sent > self.return_timeout_s:
                    break
                self.sleep(self.poll_s)
            answer, model_error = self._answer(asked)
        tracing.event("observe", phase="back" if back_at is not None else "not back",
                      back_in_s=round(back_at - back_sent, 2) if back_at is not None else None,
                      picture=picture.name if picture else None, picture_taken_at=picture.taken_at if picture else None,
                      problem=problem[-1] if problem else None, model_error=model_error, answer=answer)

        where = "the boat is back at original_gps" if back_at is not None else \
            "the boat was sent back to original_gps but hasn't arrived within %g s" % self.return_timeout_s
        if picture is None:
            raise BoatError("no camera picture taken during the %g s on course at %.0f°%s; %s"
                            % (self.run_s, azimuth_deg, problem[-1] if problem else "", where))
        if back_at is None:
            raise BoatError("%s%s" % (where, "; the vision model's answer: %s" % answer if answer is not None
                                      else "; the vision model %s too" % model_error))
        if model_error:
            raise BoatError("the vision model %s; %s" % (model_error, where))
        return {
            "answer": answer,
            "azimuth_deg": round(azimuth_deg, 1),
            "picture": picture.name,
            "picture_url": self.camera.picture_url(picture.name),
            "picture_taken_at": picture.taken_at,
            "turned_in_s": round(aligned_at - started, 1),
            "picture_after_aligned_s": round(picture.taken_unix - aligned_at, 2),
            "back_in_s": round(back_at - back_sent, 1),
            "back_at": {"lat": original_lat, "lon": original_lon},
            "note": "the boat is back at original_gps and holds still there; send it on with start_navigation",
        }

    def _ask(self, pool, picture, request_note, azimuth_deg):
        return pool.submit(self.vision, picture.jpeg, request_note, azimuth_deg, self.model_timeout_s)

    def _answer(self, asked):
        """(answer, None) or (None, what went wrong); (None, None) if the model wasn't asked."""
        if asked is None:
            return None, None
        try:
            return asked.result(), None
        except Exception as e:  # openai.APITimeoutError on a timeout
            timed_out = isinstance(e, TimeoutError) or "Timeout" in type(e).__name__
            return None, "didn't answer within %g s" % self.model_timeout_s if timed_out else "failed (%s)" % e

    def _poll_picture(self, start, end, problem):
        """A picture taken between start and end, or None; a camera service error is noted in
        `problem` (for the message) and counts as none yet."""
        try:
            return self.camera.picture_between(start, end)
        except Exception as e:
            problem.append(" (camera service: %s)" % e)
            return None

    def _bring_back(self, original):
        """Sends the boat to `original` and waits for it to arrive. Returns how it went, for a message."""
        sent = self.clock()
        self.boat.send_destination(*original)
        while self._arrived_since(sent, original) is None:
            if self.clock() - sent > self.return_timeout_s:
                return "it was sent back to original_gps but hasn't arrived within %g s" % self.return_timeout_s
            self.sleep(self.poll_s)
        return "it is back at original_gps"

    def _arrived_since(self, since, original):
        """When telemetry since `since` first showed the boat arrived at `original`, or None."""
        wanted = {"lat_e7": round(original[0] * 1e7), "lon_e7": round(original[1] * 1e7)}
        for message in self.boat.telemetry.since(since, TELEMETRY_TOPIC):
            data = message.get("data") or {}
            if data.get("destination") == wanted and (data.get("nav") or {}).get("state") == "arrived":
                return message["unix"]
        return None

    def _wait_aligned(self, since, azimuth_deg):
        """When telemetry first showed the heading within tolerance (unix seconds), or None."""
        while True:
            for message in self.boat.telemetry.since(since, TELEMETRY_TOPIC):
                heading = heading_of(message.get("data") or {})
                if heading is not None and abs(wrap180(heading - azimuth_deg)) <= ALIGN_TOLERANCE_DEG:
                    return message["unix"]
            if self.clock() - since > self.align_timeout_s:
                return None
            self.sleep(self.poll_s)
