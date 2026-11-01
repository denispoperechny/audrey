"""python -m pytest -q captain      (from software/mission_control)"""

import asyncio
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from captain import captain as captain_module
from captain.captain import Captain, what_happened
from captain.control import CaptainLog, Control
from captain.test_captain import StubAgent, telemetry  # noqa: F401  (the fixture)


def test_control_commands(tmp_path):
    control = Control(str(tmp_path))
    assert control.read()["state"] == "idle"
    with pytest.raises(ValueError, match="can't pause: the captain is idle"):
        control.command("pause")
    with pytest.raises(ValueError, match="unknown command"):
        control.command("dance")
    started = control.command("start", now=100)
    assert started["state"] == "running" and len(started["run_id"]) == 8 and started["changed_at"].startswith("1970")
    assert control.command("pause")["state"] == "paused"
    with pytest.raises(ValueError):
        control.command("pause")
    assert control.command("resume")["run_id"] == started["run_id"]  # the same run
    assert Control(str(tmp_path)).read()["state"] == "running"  # from the file, as another process sees it
    assert control.command("cancel")["state"] == "idle"
    assert control.command("start")["run_id"] != started["run_id"]
    assert control.command("end")["state"] == "idle"


def test_captain_log_keeps_the_newest(tmp_path):
    log = CaptainLog(str(tmp_path), keep=3)
    assert log.entries() == []
    for i in range(5):
        log.append({"kind": "wake-up", "n": i}, now=1000 + i)
    assert [e["n"] for e in log.entries()] == [2, 3, 4] and [e["n"] for e in log.entries(1)] == [4]
    assert log.entries(1)[0]["unix"] == 1004 and log.entries(1)[0]["t"].startswith("1970-01-01T00:16:44")


def test_what_happened_pairs_calls_with_results():
    messages = [AIMessage(content="", tool_calls=[{"name": "stop", "args": {}, "id": "c1"}]),
                ToolMessage(content="stopped", tool_call_id="c1", name="stop"),
                AIMessage(content="Holding here: a kayak ahead.")]
    calls, said = what_happened(messages)
    assert calls == [{"name": "stop", "args": {}, "result": "stopped"}] and said == ["Holding here: a kayak ahead."]


class ToolAgent(StubAgent):
    async def ainvoke(self, payload, config):
        await super().ainvoke(payload, config)
        return {"messages": [HumanMessage(content="earlier"), AIMessage(content="old"), HumanMessage(content=payload["messages"][0]["content"]),
                             AIMessage(content="", tool_calls=[{"name": "start_navigation", "args": {"azimuth": 90, "distance": 50}, "id": "a"}]),
                             ToolMessage(content="navigating", tool_call_id="a", name="start_navigation"),
                             AIMessage(content="Heading east.")]}


def test_captain_follows_the_controls(telemetry, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(captain_module, "CONTROL_POLL_S", 0.02)
    (tmp_path / "m.md").write_text("Go east.")
    control, log, agent, stops = Control(str(tmp_path)), CaptainLog(str(tmp_path)), ToolAgent(), []

    async def stop_boat():
        stops.append(time.monotonic())
        return "stopped"

    captain = Captain(agent, telemetry.provider, tmp_path / "m.md", sleep_s=0.1, control=control,
                      captain_log=log, stop_boat=stop_boat)

    async def operator():
        await asyncio.sleep(0.3)
        assert agent.calls == []  # idle: no model calls
        control.command("start")
        await asyncio.sleep(0.75)
        control.command("pause")
        await asyncio.sleep(0.8)
        control.command("resume")
        await asyncio.sleep(0.35)
        control.command("end")
        await asyncio.sleep(0.6)

    async def go():
        stopping = asyncio.Event()
        task = asyncio.create_task(captain.run(stopping))
        await operator()
        stopping.set()
        await task
    asyncio.run(go())

    times = [c[0] for c in agent.calls]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert 9 <= len(times) <= 13 and sum(g > 0.6 for g in gaps) == 1  # the pause: one long gap, no wake-ups in it
    run_id = control.read()["run_id"]
    assert {c[2]["configurable"]["thread_id"] for c in agent.calls} == {"run-" + run_id}
    resumed = [c[1] for c in agent.calls if "paused by the operator for" in c[1]]
    assert len(resumed) == 1  # told once, at the first wake-up after resuming
    assert len(stops) == 2  # pause and end stopped the boat

    entries = log.entries()
    commands = [e["command"] for e in entries if e["kind"] == "command"]
    assert commands == ["start", "pause", "resume", "end"] and entries[0]["mission"] == "Go east."
    assert all(e["run_id"] == run_id for e in entries)
    wake = next(e for e in entries if e["kind"] == "wake-up")
    assert wake["n"] == 1 and wake["said"] == ["Heading east."]  # only this wake-up's part of the conversation
    assert wake["tool_calls"] == [{"name": "start_navigation", "args": {"azimuth": 90, "distance": 50}, "result": "navigating"}]
    assert next(e for e in entries if e.get("command") == "pause")["boat_stop"] == "stopped"


def test_a_failed_wake_up_is_logged(telemetry, tmp_path):  # noqa: F811
    class Broken:
        async def ainvoke(self, payload, config):
            raise RuntimeError("model unavailable")

    log = CaptainLog(str(tmp_path))
    with pytest.raises(RuntimeError):
        asyncio.run(Captain(Broken(), telemetry.provider, tmp_path / "m.md", captain_log=log).wake_up())
    assert log.entries()[0]["error"] == "RuntimeError: model unavailable"


def test_mission_complete_tool(tmp_path):
    from captain.control import mission_complete_tool

    control = Control(str(tmp_path))
    done = mission_complete_tool(control)
    assert done.invoke({"summary": "back at L1"}).startswith("not ended: can't end: the captain is idle")
    control.command("start")
    assert done.invoke({"summary": "L2, L3 reached; back at L1 (2.6 m)."}).startswith("the run is ended")
    state = control.read()
    assert state["state"] == "idle" and state["command"] == "end" and state["by"] == "captain"
    assert state["note"] == "L2, L3 reached; back at L1 (2.6 m)."
    control.command("start")
    assert control.command("end")["by"] == "operator" and "note" not in control.read()


class FinishingAgent(StubAgent):
    """Calls mission_complete on its second wake-up, as the real captain would at the final point."""

    def __init__(self, done):
        super().__init__()
        self.done = done

    async def ainvoke(self, payload, config):
        result = await super().ainvoke(payload, config)
        if len(self.calls) == 2:
            self.done.invoke({"summary": "Back at L1. Mission done."})
        return result


def test_the_captain_ends_the_run_itself(telemetry, tmp_path, monkeypatch):  # noqa: F811
    from captain.control import mission_complete_tool

    monkeypatch.setattr(captain_module, "CONTROL_POLL_S", 0.02)
    control, log, stops = Control(str(tmp_path)), CaptainLog(str(tmp_path)), []
    agent = FinishingAgent(mission_complete_tool(control))

    async def stop_boat():
        stops.append(1)
        return "stopped"

    captain = Captain(agent, telemetry.provider, tmp_path / "m.md", sleep_s=0.05, control=control, captain_log=log,
                      stop_boat=stop_boat)
    control.command("start")

    async def go():
        stopping = asyncio.Event()
        task = asyncio.create_task(captain.run(stopping))
        await asyncio.sleep(0.6)
        stopping.set()
        await task
    asyncio.run(go())

    assert len(agent.calls) == 2  # not woken up again after it ended the run
    assert stops == [1]  # the boat was stopped, as on the operator's End
    ended = [e for e in log.entries() if e.get("command") == "end"]
    assert len(ended) == 1 and ended[0]["by"] == "captain" and ended[0]["note"] == "Back at L1. Mission done."


def test_control_keeps_each_command_numbered(tmp_path):
    control = Control(str(tmp_path))
    control.command("start")
    control.command("end")
    control.command("start")
    recent = control.read()["recent"]
    assert [(c["seq"], c["command"]) for c in recent] == [(1, "start"), (2, "end"), (3, "start")]
    assert recent[0]["run_id"] != recent[2]["run_id"]


def test_pause_interrupts_a_long_wake_up_at_once(telemetry, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(captain_module, "CONTROL_POLL_S", 0.02)
    control, log, stops = Control(str(tmp_path)), CaptainLog(str(tmp_path)), []
    agent = StubAgent(seconds=5.0)  # a long wake-up, like one with a visual_observe

    async def stop_boat():
        stops.append(time.monotonic())
        return "stopped"

    captain = Captain(agent, telemetry.provider, tmp_path / "m.md", sleep_s=0.05, control=control, captain_log=log,
                      stop_boat=stop_boat)
    control.command("start")

    async def go():
        stopping = asyncio.Event()
        task = asyncio.create_task(captain.run(stopping))
        await asyncio.sleep(0.3)  # the first wake-up is under way
        paused = time.monotonic()
        control.command("pause")
        await asyncio.sleep(0.5)
        assert stops and stops[0] - paused < 0.2  # stopped at once, not after the wake-up's 5 s
        assert len(agent.calls) == 1  # paused: no new wake-up
        control.command("resume")
        await asyncio.sleep(0.3)
        assert len(agent.calls) == 2  # resumed: woken up again
        stopping.set()
        await task
    asyncio.run(go())

    entries = log.entries()
    assert entries[1]["error"] == "interrupted by an operator command"
    assert [e["command"] for e in entries if e["kind"] == "command"][:3] == ["start", "pause", "resume"]


def test_end_and_start_during_a_wake_up_are_both_acted_on(telemetry, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(captain_module, "CONTROL_POLL_S", 0.05)
    control, log, stops = Control(str(tmp_path)), CaptainLog(str(tmp_path)), []
    agent = StubAgent(seconds=5.0)

    async def stop_boat():
        stops.append(1)
        return "stopped"

    captain = Captain(agent, telemetry.provider, tmp_path / "m.md", sleep_s=0.05, control=control, captain_log=log,
                      stop_boat=stop_boat)
    first = control.command("start")["run_id"]

    async def go():
        stopping = asyncio.Event()
        task = asyncio.create_task(captain.run(stopping))
        await asyncio.sleep(0.3)
        control.command("end")  # both between two checks
        second = control.command("start")["run_id"]
        await asyncio.sleep(0.4)
        stopping.set()
        await task
        return second
    second = asyncio.run(go())

    commands = [(e["command"], e["run_id"]) for e in log.entries() if e["kind"] == "command"]
    assert commands == [("start", first), ("end", first), ("start", second)]
    assert stops == [1]  # the end stopped the boat
    assert agent.calls[-1][2] == {"configurable": {"thread_id": "run-" + second}}  # the new run's conversation


def test_the_captains_own_end_doesnt_cut_its_wake_up_short(telemetry, tmp_path, monkeypatch):  # noqa: F811
    from captain.control import mission_complete_tool

    monkeypatch.setattr(captain_module, "CONTROL_POLL_S", 0.02)
    control, log = Control(str(tmp_path)), CaptainLog(str(tmp_path))

    class SlowFinisher(StubAgent):
        async def ainvoke(self, payload, config):
            mission_complete_tool(control).invoke({"summary": "done"})
            await asyncio.sleep(0.3)  # still saying its last words
            return {"messages": [AIMessage(content="Mission complete.")]}

    captain = Captain(SlowFinisher(), telemetry.provider, tmp_path / "m.md", sleep_s=0.05, control=control,
                      captain_log=log, stop_boat=None)
    control.command("start")

    async def go():
        stopping = asyncio.Event()
        task = asyncio.create_task(captain.run(stopping))
        await asyncio.sleep(0.6)
        stopping.set()
        await task
    asyncio.run(go())
    wake = next(e for e in log.entries() if e["kind"] == "wake-up")
    assert "error" not in wake and wake["said"] == ["Mission complete."]
    assert [e["command"] for e in log.entries() if e["kind"] == "command"] == ["start", "end"]
