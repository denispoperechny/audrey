"""python -m pytest -q captain      (from software/mission_control)"""

import asyncio
import json
import time

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from captain import agent as captain_agent
from captain.captain import Captain, read_mission, wake_up_message
from captain.state import boat_state
from geo import course_between
from telemetry_service import TelemetryProvider
from telemetry_service.mqtt_reader import TelemetryFiles, record_line

LAT, LON = 30.2443012, -95.4902590
TEL = "audrey/boat1/telemetry"


@pytest.fixture
def telemetry(tmp_path):
    files = TelemetryFiles(str(tmp_path / "telemetry"))

    def write(**data):
        now = time.time()
        files.write(record_line(TEL, json.dumps(data).encode(), now=now), now=now)

    write.provider = TelemetryProvider(files.folder)
    return write


def test_boat_state(telemetry):
    assert "none" in boat_state(telemetry.provider)["telemetry"]
    north_50 = (LAT + 50 / 111195, LON)
    telemetry(gps={"fix_valid": True, "lat_e7": round(LAT * 1e7), "lon_e7": round(LON * 1e7), "sog_kn": 1.5},
              compass={"heading": 10, "virtual": {"heading": 350}}, nav={"state": "navigating"},
              bcu={"esp32_mode": True, "battery_mv": 7950},
              destination={"lat_e7": round(north_50[0] * 1e7), "lon_e7": round(north_50[1] * 1e7)})
    state = boat_state(telemetry.provider)
    assert state["position"] == {"lat": LAT, "lon": LON} and state["speed_m_s"] == 0.77
    assert state["heading_deg"] == 350 and state["autopilot"] == "navigating" and state["control"] == "autopilot"
    dest = state["destination"]
    assert abs(dest["distance_m"] - 50) < 0.5 and abs(dest["azimuth_deg"]) < 0.5 and dest["relative_bearing_deg"] == 10
    assert state["battery_v"] == 7.95 and state["telemetry_age_s"] < 5


def test_boat_state_without_a_fix(telemetry):
    telemetry(gps={"fix_valid": False}, bcu={"esp32_mode": False}, destination=None)
    state = boat_state(telemetry.provider)
    assert state["position"] == "no GPS fix" and state["destination"] is None and "remote control" in state["control"]


def test_bearing_and_distance():
    az, d = course_between(LAT, LON, LAT, LON + 100 / (111195 * 0.8641))
    assert abs(az - 90) < 0.5 and abs(d - 100) < 0.5


def test_wake_up_message_and_mission(tmp_path):
    assert "No mission" in read_mission(tmp_path / "missing.md")
    (tmp_path / "m.md").write_text("Go to the dock.\n")
    msg = wake_up_message(3, 15.2, read_mission(tmp_path / "m.md"), {"autopilot": "idle"}, now=0)
    assert msg.startswith("Wake-up 3 at 1970-01-01T00:00:00Z (the previous one started 15.2 s ago)")
    assert "Mission:\nGo to the dock." in msg and '"autopilot": "idle"' in msg


class StubAgent:
    """Records the wake-up messages; each wake-up takes `seconds`."""

    def __init__(self, seconds=0.0):
        self.seconds, self.calls, self.running, self.overlapped = seconds, [], False, False

    async def ainvoke(self, payload, config):
        self.overlapped |= self.running
        self.running = True
        self.calls.append((time.monotonic(), payload["messages"][0]["content"], config))
        await asyncio.sleep(self.seconds)
        self.running = False
        return {"messages": [AIMessage(content="holding still")]}


def run_for(captain, seconds):
    async def go():
        stopping = asyncio.Event()
        asyncio.get_running_loop().call_later(seconds, stopping.set)
        await captain.run(stopping)
    asyncio.run(go())


def test_captain_sleeps_between_wake_ups(telemetry, tmp_path):
    (tmp_path / "m.md").write_text("Hold still.")
    agent = StubAgent()
    run_for(Captain(agent, telemetry.provider, tmp_path / "m.md", sleep_s=0.1, thread_id="t"), 0.45)
    times = [c[0] for c in agent.calls]
    assert len(times) in (4, 5) and all(0.08 < b - a < 0.15 for a, b in zip(times, times[1:]))
    assert "Mission:\nHold still." in agent.calls[0][1] and "the first one" in agent.calls[0][1]
    assert agent.calls[1][2] == {"configurable": {"thread_id": "t"}}  # one conversation across wake-ups


def test_captain_never_overlaps_a_long_wake_up(telemetry, tmp_path):
    agent = StubAgent(seconds=0.25)
    run_for(Captain(agent, telemetry.provider, tmp_path / "m.md", sleep_s=0.1), 0.8)
    times = [c[0] for c in agent.calls]  # each one 0.25 s, then the 0.1 s sleep
    assert not agent.overlapped and len(times) >= 2 and all(0.34 < b - a < 0.45 for a, b in zip(times, times[1:]))


SEEN_PROMPTS = []


class ToolCallingFake(GenericFakeChatModel):
    """A fake chat model that the agent can bind tools to; records what it is sent."""

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, *args, **kwargs):
        SEEN_PROMPTS.append(messages)
        return super()._generate(messages, *args, **kwargs)


def test_build_agent_runs_a_wake_up_with_tools_and_skills(tmp_path):
    calls = []

    @tool
    def stop() -> str:
        """Hold still."""
        calls.append("stop")
        return "stopped"

    skill = tmp_path / "skills" / "harbour"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: harbour\ndescription: Entering a harbour safely.\n---\nGo slow.\n")
    model = ToolCallingFake(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "stop", "args": {}, "id": "1"}]),
        AIMessage(content="Stopped: no mission yet."),
    ]))
    agent = captain_agent.build_agent(model, [stop], 15, home=tmp_path)
    result = asyncio.run(agent.ainvoke({"messages": [{"role": "user", "content": "Wake-up 1"}]},
                                       {"configurable": {"thread_id": "x"}}))
    assert calls == ["stop"] and result["messages"][-1].content == "Stopped: no mission yet."
    system = SEEN_PROMPTS[0][0]
    assert system.type == "system" and "woken up again 15 seconds after each wake-up ends" in system.text
    assert "harbour" in system.text and "Entering a harbour safely." in system.text  # the skill is offered
