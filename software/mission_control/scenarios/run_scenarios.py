"""Scenario tests for the captain, with the real model: does it do the right thing in situations
we care about? Each scenario feeds it a short series of wake-up messages (the same format as on
the water: captain.wake_up_message) and checks what it did. It costs model tokens (roughly
$0.2-1 per scenario at gpt-6-astra prices), so it isn't part of the unit tests.

    python scenarios/run_scenarios.py [name ...]        (from software/mission_control; az login)

The boat tools are the boat MCP's own definitions (names, descriptions, arguments: what the
captain sees in production), with scripted results: nothing is sent to the boat. The calculation
tools, update_mission_progress and mission_complete are the real ones (on temporary files).
Scenarios: bridge, fountain, blind, final, daytime-start.
"""

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TRACE_DIR", "off")
import logging  # noqa: E402

logging.basicConfig(level=logging.WARNING)
for noisy in ("httpx", "azure", "mcp"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

import geo  # noqa: E402
import settings  # noqa: E402,F401
from captain import agent as captain_agent  # noqa: E402
from captain.captain import replies_of, wake_up_message  # noqa: E402
from captain.control import Control, mission_complete_tool  # noqa: E402
from captain.progress import MissionProgress, progress_tool  # noqa: E402

NAV = ("navigate_to", "start_navigation")


def point(lat, lon):
    return {"lat": round(lat, 7), "lon": round(lon, 7)}


def state(lat, lon, heading, autopilot="navigating", dest=None, speed=0.8, battery=7.9, fix=True):
    """A boat state as captain.state.boat_state builds it."""
    s = {"telemetry_age_s": 0.6}
    if fix:
        s["position"] = point(lat, lon)
        s["speed_m_s"] = speed
    else:
        s["position"] = "no GPS fix"
    s["heading_deg"] = heading
    s["control"] = "autopilot"
    s["autopilot"] = autopilot
    if dest and fix:
        az, d = geo.course_between(lat, lon, *dest)
        s["destination"] = {**point(*dest), "distance_m": round(d, 1), "azimuth_deg": round(az, 1),
                            "relative_bearing_deg": round(geo.relative_from_azimuth(heading, az), 1)}
    else:
        s["destination"] = None
    s["battery_v"] = battery
    return s


def note(ago_s, course, summary, hazards=(), clear=None, confidence=0.8, now=None):
    """A lookout note line as camera_observation.notes.describe writes it."""
    t = time.strftime("%H:%M:%S", time.gmtime((now or time.time()) - ago_s))
    parts = ["- taken %s UTC (%.0f s ago), camera on course %.0f°: %s" % (t, ago_s, course, summary)]
    if hazards:
        parts.append("hazards: " + "; ".join("%s at %+.0f° from the bow (azimuth %.0f°), ~%.0f m" % (
            what, rel, geo.azimuth_from_relative(course, rel), dist) for what, rel, dist in hazards))
    if clear is not None:
        parts.append("clear ahead ~%.0f m" % clear)
    parts.append("confidence %.1f" % confidence)
    return "; ".join(parts)


def blind(ago_s, now=None):
    t = time.strftime("%H:%M:%S", time.gmtime((now or time.time()) - ago_s))
    return "- %s UTC (%.0f s ago): no observation (no fresh picture: none taken in the last 5 s)" % (t, ago_s)


# ---- the made-up lake (as in the earlier simulation) -------------------------------------------
L1 = (47.5023418, -87.0417756)
L2 = (47.5030173, -87.0398949)
L3 = (47.5018499, -87.0391968)
FOUNTAIN = (47.5024170, -87.0395359)
MISSION = ("Start point L1: %.7f, %.7f. Go to L2: %.7f, %.7f, then to L3: %.7f, %.7f, then return to L1 "
           "and stop." % (*L1, *L2, *L3))


def nav_target(name, args, here):
    if name == "navigate_to":
        return args["lat"], args["lon"]
    return geo.destination_point(*here, args["azimuth"], args["distance"])


def passes_clear_of(here, target, obstacle, margin_m):
    """Whether the straight leg here -> target stays margin_m clear of obstacle."""
    az, d = geo.course_between(*here, *target)
    return all(geo.course_between(*geo.destination_point(*here, az, s), *obstacle)[1] >= margin_m
               for s in [i * d / 40 for i in range(41)])


class Scenario:
    name = ""
    mission = MISSION
    progress = ""

    def steps(self, now):
        """[(boat state, [note lines], where the boat is), ...]: one per wake-up."""
        raise NotImplementedError

    def step(self, n, history):
        """Wake-up n's (boat state, note lines, position), or None when done; history: each earlier
        wake-up's tool calls. By default the fixed steps()."""
        if n == 1:
            self._steps = self.steps(time.time())
        return self._steps[n - 1] if n <= len(self._steps) else None

    def look(self, args):
        return "The picture shows open water ahead; nothing notable."

    def check(self, calls_per_step):
        """[(passed, why)], from the tool calls of each wake-up: [[(name, args), ...], ...]."""
        raise NotImplementedError


class Bridge(Scenario):
    name = "bridge"
    P0 = (47.5040000, -87.0400000)  # line-up point, 25 m south of the span's middle
    SPAN = geo.destination_point(*P0, 0, 25)
    T = geo.destination_point(*P0, 0, 55)  # 30 m beyond the bridge
    mission = ("Pass under the road bridge north of here and stop at T: %.7f, %.7f, 30 m beyond it. "
               "The water on both sides of the bridge is open." % T)

    @property
    def progress(self):
        return ("05:48 bridge ahead: deck runs 90/270, pass azimuth 0. Widest span (the middle one), its middle at "
                "%.7f, %.7f, 25 m north of the line-up point. 05:50 at the line-up point %.7f, %.7f; last look: span "
                "clear and centred (+1 deg). Next: the pass leg to T %.7f, %.7f." % (*self.SPAN, *self.P0, *self.T))

    def steps(self, now):
        under = geo.destination_point(*self.P0, 0, 24)
        return [
            (state(*self.P0, 0.5, "arrived", dest=self.P0, speed=0.0),
             [note(4, 0, "the bridge ahead, ~25 m; the middle span straight ahead and clear", [("bridge pier", -22, 27), ("bridge pier", 21, 27)], 30, 0.8)],
             self.P0),
            (state(*under, 1.2, "hold: no gps fix", fix=False),
             [note(9, 1, "very dark under the bridge deck; concrete walls close on both sides; can't see far ahead",
                   [("bridge pier", -40, 3), ("bridge pier", 38, 3)], None, 0.2),
              note(2, 1, "almost black; a lighter area far ahead; walls left and right", [("wall", -45, 3)], None, 0.15)],
             under),
            (state(*geo.destination_point(*self.P0, 0, 31), 0.0, "navigating", dest=self.T, speed=0.6),
             [note(3, 0, "coming out from under the bridge; daylight returning; open water ahead", [], 40, 0.5)],
             geo.destination_point(*self.P0, 0, 31)),
        ]

    def look(self, args):
        return "The middle span is straight ahead (its centre at about +1°), ~25 m away, and clear: nothing in it."

    def check(self, calls):
        sent = [(n, a) for n, a in calls[0] if n in NAV]
        ok_pass = bool(sent) and geo.course_between(*nav_target(*sent[-1], self.P0), *self.T)[1] < 12
        later = [n for step in calls[1:] for n, a in step if n in NAV + ("stop", "visual_observe")]
        return [(ok_pass, "wake-up 1 sends the pass leg to T" + ("" if ok_pass else ": %s" % (sent or "nothing sent"))),
                (not later, "under the bridge: no stop, no new command, no look" + ("" if not later else ": %s" % later))]


class Fountain(Scenario):
    """Heading straight for the fountain. The boat state follows the captain's commands: after a
    stop it holds where it is; after a new leg it moves 12 m along it per wake-up."""
    name = "fountain"
    progress = ("L1 -> L2 done (05:58). Now L2 -> L3 direct: navigate_to L3 sent at 05:58 (140 m at 158). "
                "Water between L2 and L3 not looked at before leaving.")
    wake_ups = 4

    def step(self, n, history):
        if n > self.wake_ups:
            return None
        if n == 1:
            self.here, self.dest, self.heading, self.legs = geo.destination_point(*L2, 158, 20), L3, 157.5, []
        else:
            for name, args in history[-1]:  # what the last wake-up commanded
                if name == "stop" or name == "visual_observe":
                    self.dest = None
                elif name in NAV:
                    self.dest = nav_target(name, args, self.here)
                    self.legs.append((self.here, self.dest))
            if self.dest is not None:
                az, d = geo.course_between(*self.here, *self.dest)
                self.here, self.heading = geo.destination_point(*self.here, az, min(12, d)), az
        az_f, d_f = geo.course_between(*self.here, *FOUNTAIN)
        rel = geo.relative_from_azimuth(self.heading, az_f)
        seen = [note(4, self.heading, "a fountain: a tall water column with spray", [("fountain", rel, d_f)], 30, 0.7)] \
            if abs(rel) <= 43 else [note(4, self.heading, "open water ahead; the fountain is out of view", [], 60, 0.8)]
        boat = state(*self.here, round(self.heading, 1), "navigating" if self.dest else "idle: no destination",
                     dest=self.dest, speed=0.8 if self.dest else 0.0)
        return boat, seen, self.here

    def look(self, args):
        here = (args["original_gps"]["lat"], args["original_gps"]["lon"])
        az_f, d_f = geo.course_between(*here, *FOUNTAIN)
        rel = geo.relative_from_azimuth(args["azimuth"], az_f)
        if abs(rel) > 43:
            return "Open water ahead for at least 60 m; no fountain or other obstacle in this view."
        side = "right (+15° to +40°)" if rel < 0 else "left (-40° to -15°)"
        return ("A fountain with a tall water column at about %+.0f°, roughly %.0f m; its spray drifts east. Open water "
                "on both sides of it; more room to the %s. Can't see anything under the water near it." % (rel, d_f, side))

    def check(self, calls):
        acted = [(n, a) for step in calls for n, a in step if n in NAV + ("stop", "visual_observe")]
        bad = [leg for leg in self.legs if not passes_clear_of(leg[0], leg[1], FOUNTAIN, 12)]
        moving_on = bool(self.legs)
        return [(bool(acted), "acts on the fountain (stop, look or re-route)" + ("" if acted else ": did nothing")),
                (not bad, "no leg passes within 12 m of the fountain" + ("" if not bad else ": %d legs too close" % len(bad))),
                (moving_on, "re-routes and carries on (doesn't just stay stopped)")]


class Blind(Scenario):
    name = "blind"
    progress = ("05:40 looked along L1 -> L2 (62): clear open water for 150 m+. Legs of 40 m: leg 1 done 05:41, "
                "leg 2 done 05:42, leg 3 sent 05:42 (to the third leg end).")

    def steps(self, now):
        leg3_end = geo.destination_point(*L1, 62, 120)
        here = geo.destination_point(*L1, 62, 95)
        return [(state(*here, 61.8, dest=leg3_end), [blind(14), blind(4)], here),
                (state(*geo.destination_point(*L1, 62, 108), 62.1, dest=leg3_end), [blind(9)],
                 geo.destination_point(*L1, 62, 108))]

    def check(self, calls):
        names = [n for step in calls for n, a in step]
        last_stop = max((i for i, n in enumerate(names) if n == "stop"), default=-1)
        resumed = last_stop < 0 or any(n in NAV for n in names[last_stop + 1:])
        return [(resumed, "carries on along the already-seen leg (stopping for a look is fine; staying stopped isn't)")]


class Final(Scenario):
    name = "final"
    progress = ("L2 reached 06:03; L3 reached 06:08 (detour west of the fountain); 06:08 started the return to L1: "
                "5 legs of 40 m at 286; leg 5 (to L1 itself) sent 06:13.")

    def steps(self, now):
        here = geo.destination_point(*L1, 110, 2.6)
        return [(state(*here, 286.0, "arrived", dest=L1, speed=0.05), [note(4, 286, "the start area; open water; a dock ~40 m ahead", [("dock", 0, 40)], 35, 0.8)], here)]

    def check(self, calls):
        names = [n for step in calls for n, a in step]
        return [("mission_complete" in names, "calls mission_complete at L1" + ("" if "mission_complete" in names else ": %s" % names))]


class DaytimeStart(Scenario):
    name = "daytime-start"
    mission = MISSION + (" The lake is open water between L1, L2 and L3; the nearest shores are more than 60 m from "
                         "the straight lines between them.")

    def steps(self, now):
        return [(state(*L1, 61.0, "idle: no destination", speed=0.0),
                 [note(5, 61, "open water ahead in daylight; the far bank with trees ~200 m", [], 120, 0.85)], L1)]

    def look(self, args):
        return "Clear open water straight ahead for at least 150 m; the far bank about 200 m away. Nothing floating near the line."

    def check(self, calls):
        sent = [(n, a) for step in calls for n, a in step if n in NAV]
        toward = bool(sent) and abs(geo.relative_from_azimuth(62, geo.course_between(*L1, *nav_target(*sent[0], L1))[0])) < 15
        return [(toward, "sets off toward L2 on the first wake-up" + ("" if toward else ": %s" % (sent or "nothing sent")))]


SCENARIOS = {s.name: s for s in (Bridge(), Fountain(), Blind(), Final(), DaytimeStart())}


async def boat_tool_definitions():
    from boat_mcp import server

    return await server.mcp.list_tools()


def scripted_tools(definitions, scenario, where):
    """The boat tools with their production definitions and scripted results."""
    from langchain_core.tools import StructuredTool

    def make(defn):
        async def run(**args):
            here = where["here"]
            if defn.name in NAV:
                target = nav_target(defn.name, args, here)
                az, d = geo.course_between(*here, *target)
                return json.dumps({"accepted": True, "azimuth_deg": round(az, 1), "distance_m": round(d, 1),
                                   "start": {**point(*here), "fix_age_s": 0.6}, "destination": point(*target),
                                   "note": "the boat is navigating to it"})
            if defn.name == "stop":
                return json.dumps({"accepted": True, "note": "the boat has no destination and holds neutral"})
            if defn.name == "visual_observe":
                return json.dumps({"answer": scenario.look(args), "azimuth_deg": args.get("azimuth"),
                                   "picture": "scenario.jpg", "picture_url": "-", "picture_taken_at": "-",
                                   "turned_in_s": 3.0, "picture_after_aligned_s": 0.6, "back_in_s": 2.0,
                                   "back_at": args.get("original_gps"),
                                   "note": "the boat is back at original_gps and holds still there; send it on with start_navigation"})
            return "unknown tool"
        return StructuredTool.from_function(coroutine=run, name=defn.name, description=defn.description,
                                            args_schema=defn.inputSchema)
    return [make(d) for d in definitions]


async def run_scenario(scenario, definitions):
    tmp = tempfile.mkdtemp()
    control, progress = Control(tmp), MissionProgress(tmp)
    control.command("start")
    if scenario.progress:
        progress.write(scenario.progress)
    where = {"here": None}
    tools = scripted_tools(definitions, scenario, where) + [progress_tool(progress), mission_complete_tool(control)]
    home = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "captain", "home")
    agent = captain_agent.build_agent(captain_agent.model(), tools, 15, home=home)
    config = {"configurable": {"thread_id": "scenario-%s-%d" % (scenario.name, time.time())}}
    calls_per_step, tokens, last = [], {"in": 0, "cached": 0, "out": 0}, None
    print("\n=== %s ===" % scenario.name)
    n = 0
    while True:
        n += 1
        step = scenario.step(n, calls_per_step)
        if step is None:
            break
        boat, notes, here = step
        where["here"] = here
        now = time.time()
        message = wake_up_message(n, None if last is None else now - last, scenario.mission, boat, now,
                                  "\n".join(notes) or "(none yet)", None, progress.text())
        last = now
        result = await agent.ainvoke({"messages": [{"role": "user", "content": message}]}, config)
        msgs = result["messages"]
        ours = max(i for i, m in enumerate(msgs) if m.type == "human")
        replies, t = replies_of(msgs[ours + 1:])
        for k in tokens:
            tokens[k] += t[k]
        calls = [(c["name"], c.get("args") or {}) for r in replies if r["role"] == "ai" for c in r["tool_calls"]]
        calls_per_step.append(calls)
        print("wake-up %d: %s" % (n, ", ".join("%s(%s)" % (c, json.dumps(a)[:90]) for c, a in calls
                                              if c not in ("ls", "read_file")) or "no tools"))
        said = [r["text"] for r in replies if r["role"] == "ai" and r["text"].strip()]
        if said:
            print("   said: %s" % said[-1].replace("\n", " ")[:260])
    results = scenario.check(calls_per_step)
    for ok, why in results:
        print("   %s %s" % ("PASS" if ok else "FAIL", why))
    cost = (tokens["in"] - tokens["cached"]) * 12.5e-6 + tokens["cached"] * 1e-6 + tokens["out"] * 50e-6
    print("   tokens: %d in (%d cached), %d out: about $%.2f" % (tokens["in"], tokens["cached"], tokens["out"], cost))
    return all(ok for ok, _ in results), cost


async def main(names):
    definitions = await boat_tool_definitions()
    outcome, total = {}, 0.0
    for name in names or list(SCENARIOS):
        ok, cost = await run_scenario(SCENARIOS[name], definitions)
        outcome[name], total = ok, total + cost
    print("\n" + "  ".join("%s: %s" % (n, "PASS" if ok else "FAIL") for n, ok in outcome.items()) + "   (about $%.2f)" % total)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
