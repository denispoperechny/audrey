"""The captain: wakes the agent (agent.py) with the mission and the boat's state, CAPTAIN_SLEEP_S
seconds after its previous wake-up ended, and lets it act through the boat's MCP tools.

    python -m captain.captain        (from software/mission_control)

Runs as its own process (supervisord.conf), next to the telemetry reader and the boat_mcp
server. Each wake-up sends one message:

    Wake-up 12 at 2026-10-29T20:15:30Z (the previous one started 19.4 s ago)
    Mission: <captain/mission.md, read afresh every time, so it can be edited while this runs>
    Boat state: {...}            (state.py, from the telemetry folder)
    Camera notes: <the lookout's notes since the previous wake-up, or its newest one if there
                   are none since: camera_observation>

Wake-ups never overlap: the next one is CAPTAIN_SLEEP_S after the previous one ends, however long
it took (a visual_observe can take over a minute), so with a model answering in a few seconds
the captain wakes up about every 20 s.

The mission UI (mission_ui) runs it through control.py's files: the captain starts idle (no model
calls) and wakes up only while a mission is running. Start begins a fresh conversation; pause,
end and cancel first stop the boat (the boat's stop tool, directly, not through the model); resume
goes on with the same conversation and tells the captain it was paused. Every wake-up and command
goes to the captain's log for the UI (what it saw, its tool calls and their results, what it said),
and to this process's output; the full wake-up messages, replies and token counts go to the
traces (tracing.py). Settings (environment, or .env: settings.py, .env.example):
CAPTAIN_SLEEP_S (15), CAPTAIN_MISSION_FILE, CAPTAIN_STATE_DIR, and those in agent.py.
"""

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

import tracing
from camera_observation.notes import Notes
from camera_observation.notes import describe as describe_note
from captain import agent as captain_agent
from captain.control import PAUSED, RUNNING, CaptainLog, Control
from captain.state import boat_state
from telemetry_service import TelemetryProvider

log = logging.getLogger("captain")
DEFAULT_SLEEP_S = 15.0  # between the end of one wake-up and the start of the next
CYCLE_TIMEOUT_S = 180.0  # one wake-up, including its tool calls
DEFAULT_MISSION_FILE = Path(__file__).resolve().parent / "mission.md"
CONTROL_POLL_S = 0.5  # how often the control file is checked


def read_mission(path):
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        text = ""
    return text or "No mission given: keep the boat still (stop) and wait."


def camera_notes_text(notes, since, now):
    """The lookout's notes since `since` (the previous wake-up), or its newest one; newest last."""
    if notes is None:
        return "(no lookout running)"
    fresh = notes.since(since) if since is not None else notes.latest(1)
    shown = fresh or notes.latest(1)
    if not shown:
        return "(none yet)"
    head = "" if fresh else "(nothing new since the previous wake-up; the newest:)\n"
    return head + "\n".join(describe_note(note, now) for note in shown)


def wake_up_message(n, since_last_s, mission, state, now=None, camera_notes="(no lookout running)", note=None):
    now = time.time() if now is None else now
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    previous = "the first one" if since_last_s is None else "the previous one started %.1f s ago" % since_last_s
    return "Wake-up %d at %s (%s)%s\n\nMission:\n%s\n\nBoat state:\n%s\n\nCamera notes (the lookout, oldest first):\n%s" % (
        n, stamp, previous, "\n" + note if note else "", mission, json.dumps(state, indent=1), camera_notes)


def what_happened(messages):
    """From the agent's messages in one wake-up: (its tool calls with their results, what it said).
    Also logged to this process's output."""
    calls, said = {}, []
    for m in messages:
        kind = getattr(m, "type", "")
        if kind == "ai":
            for call in getattr(m, "tool_calls", None) or []:
                calls[call.get("id")] = {"name": call["name"], "args": call.get("args", {}), "result": None}
                log.info("tool call: %s(%s)", call["name"], json.dumps(call.get("args", {}), ensure_ascii=False))
            text = m.text if isinstance(getattr(m, "text", None), str) else ""
            if text.strip():
                said.append(text.strip())
                log.info("captain: %s", text.strip())
        elif kind == "tool":
            result = str(m.content)[:1000]
            log.info("tool result (%s): %s", getattr(m, "name", "?"), result[:500])
            call = calls.get(getattr(m, "tool_call_id", None))
            if call is not None:
                call["result"] = result
            else:
                calls[id(m)] = {"name": getattr(m, "name", "?"), "args": None, "result": result}
    return list(calls.values()), said


def tokens_of(message):
    """An AI message's token counts: {"in", "cached", "out"}, or None if it has none."""
    usage = getattr(message, "usage_metadata", None)
    if not usage:
        return None
    return {"in": usage.get("input_tokens", 0), "cached": (usage.get("input_token_details") or {}).get("cache_read", 0) or 0,
            "out": usage.get("output_tokens", 0)}


def replies_of(messages):
    """The agent's messages in one wake-up, for the trace: what it said and called, the results,
    and the tokens of each model call; and the total tokens."""
    replies, total = [], {"in": 0, "cached": 0, "out": 0, "calls": 0}
    for m in messages:
        kind = getattr(m, "type", "")
        if kind == "ai":
            reply = {"role": "ai", "text": m.text if isinstance(getattr(m, "text", None), str) else str(m.content),
                     "tool_calls": [{"name": c["name"], "args": c.get("args")} for c in getattr(m, "tool_calls", None) or []]}
            tokens = tokens_of(m)
            if tokens:
                reply["tokens"] = tokens
                total = {k: total[k] + tokens.get(k, 0) for k in ("in", "cached", "out")} | {"calls": total["calls"] + 1}
            replies.append(reply)
        elif kind == "tool":
            replies.append({"role": "tool", "name": getattr(m, "name", None), "content": str(m.content)})
    return replies, total


class Captain:
    def __init__(self, agent, telemetry, mission_file, sleep_s=DEFAULT_SLEEP_S, thread_id=None,
                 cycle_timeout_s=CYCLE_TIMEOUT_S, notes=None, control=None, captain_log=None, stop_boat=None):
        self.agent, self.telemetry, self.mission_file = agent, telemetry, mission_file
        self.notes = notes  # camera_observation.notes.Notes, or None
        self.sleep_s, self.cycle_timeout_s = sleep_s, cycle_timeout_s
        self.control = control  # control.Control, or None: always running (tests, scripts)
        self.captain_log = captain_log  # control.CaptainLog, or None
        self.stop_boat = stop_boat  # async () -> result: the boat's stop tool, or None
        self.config = {"configurable": {"thread_id": thread_id or "captain-%d" % time.time()}}
        self.run_id = None
        self.wake_ups = 0
        self.last_wake_up = None
        self.paused_at = None
        self.note = None  # said once, at the next wake-up (e.g. "resumed after a pause")
        self.seen_control = None

    def _log(self, entry, **trace):
        """To the captain's log (the UI) and, with `trace`'s extra fields, to the traces."""
        if self.captain_log is not None:
            self.captain_log.append({"run_id": self.run_id, **entry})
        fields = dict(entry, run_id=self.run_id, **trace)  # its run, also for "end" (the control is idle by then)
        if "replies" in fields:  # they have what it said and called, and the full results
            fields.pop("said", None), fields.pop("tool_calls", None)
        tracing.event(fields.pop("kind"), **fields)

    async def wake_up(self):
        now = time.time()
        self.wake_ups += 1
        state = boat_state(self.telemetry, now)
        message = wake_up_message(self.wake_ups, None if self.last_wake_up is None else now - self.last_wake_up,
                                  read_mission(self.mission_file), state, now,
                                  camera_notes_text(self.notes, self.last_wake_up, now), self.note)
        self.last_wake_up, self.note = now, None
        entry = {"kind": "wake-up", "n": self.wake_ups, "boat": state}
        try:
            result = await asyncio.wait_for(self.agent.ainvoke({"messages": [{"role": "user", "content": message}]}, self.config),
                                            self.cycle_timeout_s)
        except asyncio.TimeoutError:
            self._log({**entry, "error": "took over %g s: abandoned" % self.cycle_timeout_s, "took_s": round(time.time() - now, 1)},
                      message=message)
            raise
        except Exception as e:
            self._log({**entry, "error": "%s: %s" % (type(e).__name__, e), "took_s": round(time.time() - now, 1)}, message=message)
            raise
        messages = result.get("messages", [])
        # What came after this wake-up's message (the last one from us; older history may have
        # been summarised away, so counting messages wouldn't do).
        ours = max((i for i, m in enumerate(messages) if getattr(m, "type", "") == "human"), default=-1)
        calls, said = what_happened(messages[ours + 1:])
        replies, tokens = replies_of(messages[ours + 1:])
        self._log({**entry, "tool_calls": calls, "said": said, "tokens": tokens, "took_s": round(time.time() - now, 1)},
                  message=message, replies=replies, history_messages=len(messages))
        return result

    async def _stop_boat(self, why):
        if self.stop_boat is None:
            return "no stop tool"
        try:
            result = str(await self.stop_boat())[:500]
        except Exception as e:
            result = "the stop failed: %s: %s" % (type(e).__name__, e)
        log.info("%s: boat stopped: %s", why, result)
        return result

    async def follow_control(self):
        """Acts on a new command from the UI. Returns whether the captain should be waking up."""
        if self.control is None:
            return True
        control = self.control.read()
        key = (control.get("run_id"), control.get("command"), control.get("unix"))
        if key != self.seen_control:
            self.seen_control = key
            command = control.get("command")
            if command == "start":
                self.run_id = control.get("run_id")
                self.config = {"configurable": {"thread_id": "run-%s" % self.run_id}}
                self.wake_ups, self.last_wake_up, self.paused_at, self.note = 0, None, None, None
                log.info("mission started (run %s)", self.run_id)
                self._log({"kind": "command", "command": "start", "mission": read_mission(self.mission_file)})
            elif command == "resume":
                paused_for = time.time() - self.paused_at if self.paused_at else None
                self.note = ("The mission was paused by the operator%s and is now resumed; the boat was stopped meanwhile."
                             % (" for %.0f s" % paused_for if paused_for else ""))
                self.paused_at = None
                self._log({"kind": "command", "command": "resume"})
            elif command in ("pause", "end", "cancel"):
                self.paused_at = time.time() if command == "pause" else None
                result = await self._stop_boat(command)
                self._log({"kind": "command", "command": command, "boat_stop": result})
        return control.get("state") == RUNNING

    async def run(self, stopping):
        next_at = time.monotonic()
        while not stopping.is_set():
            running = await self.follow_control()
            if not running or time.monotonic() < next_at:
                try:
                    await asyncio.wait_for(stopping.wait(), CONTROL_POLL_S if not running else
                                           max(0.0, min(CONTROL_POLL_S, next_at - time.monotonic())))
                    break
                except asyncio.TimeoutError:
                    pass
                if not running:
                    next_at = time.monotonic()  # once started or resumed: wake up straight away
                continue
            started = time.monotonic()
            try:
                await self.wake_up()
            except asyncio.TimeoutError:
                log.error("wake-up %d took over %g s: abandoned", self.wake_ups, self.cycle_timeout_s)
            except Exception:
                log.exception("wake-up %d failed", self.wake_ups)
            log.info("wake-up %d took %.1f s", self.wake_ups, time.monotonic() - started)
            next_at = time.monotonic() + self.sleep_s


async def main_async():
    sleep_s = float(os.environ.get("CAPTAIN_SLEEP_S") or DEFAULT_SLEEP_S)
    mission_file = os.environ.get("CAPTAIN_MISSION_FILE") or DEFAULT_MISSION_FILE
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)

    while not stopping.is_set():  # the boat_mcp server may start after this process
        try:
            tools = await captain_agent.boat_tools()
            break
        except Exception as e:
            log.warning("boat MCP server not reachable yet (%s); retrying in 5 s", e)
            try:
                await asyncio.wait_for(stopping.wait(), 5)
            except asyncio.TimeoutError:
                pass
    else:
        return
    log.info("tools: %s; waking up %g s after each wake-up ends while a mission runs; mission from %s",
             ", ".join(t.name for t in tools), sleep_s, mission_file)
    agent = captain_agent.build_agent(captain_agent.model(), tools, sleep_s)
    stop_tool = next((t for t in tools if t.name == "stop"), None)
    await Captain(agent, TelemetryProvider(), mission_file, sleep_s, notes=Notes(), control=Control(),
                  captain_log=CaptainLog(), stop_boat=(lambda: stop_tool.ainvoke({})) if stop_tool else None).run(stopping)
    log.info("stopped")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "azure", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
