"""python -m pytest -q test_tracing.py      (from software/mission_control)"""

import json
import os
import time

import tracing


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_events_go_to_the_window_file(tmp_path):
    tracer = tracing.Tracer(tmp_path, proc="captain", run_id=lambda: "abc12345")
    tracer.event("wake-up", n=1, message="x" * (tracing.MAX_TEXT + 10), nested={"items": ["y" * (tracing.MAX_TEXT + 1)]})
    tracer.event("command", command="pause", run_id="override")
    tracer.flush()
    [path] = list(tmp_path.rglob("*.jsonl"))
    assert path.name.startswith("captain_") and path.name.endswith("Z.jsonl")
    first, second = lines(path)
    assert list(first)[:5] == ["unix", "t", "proc", "run_id", "kind"] and first["run_id"] == "abc12345"
    assert first["message"].endswith("...[10 more]") and first["nested"]["items"][0].endswith("...[1 more]")
    assert second["kind"] == "command" and second["run_id"] == "override"


def test_file_names_follow_the_mqtt_logs():
    assert tracing.file_name("lookout", 1793329044.5) == "2026/10/30/lookout_20261030T0250Z.jsonl"


def test_a_full_queue_drops_and_counts_instead_of_waiting(tmp_path):
    tracer = tracing.Tracer(tmp_path, proc="p", run_id=lambda: None, max_queue=2)
    started = time.perf_counter()
    for i in range(1000):
        tracer.event("e", i=i)
    assert time.perf_counter() - started < 0.5 and tracer.dropped == 998
    tracer.flush()
    events = lines(next(tmp_path.rglob("*.jsonl")))
    assert [e["kind"] for e in events] == ["e", "e", "trace-dropped"] and events[-1]["count"] == 998


def test_unserialisable_values_dont_break_it(tmp_path):
    tracer = tracing.Tracer(tmp_path, proc="p", run_id=lambda: None)
    tracer.event("e", thing=object(), more={1, 2})
    tracer.flush()
    assert lines(next(tmp_path.rglob("*.jsonl")))[0]["kind"] == "e"


def test_event_is_cheap(tmp_path):
    tracer = tracing.Tracer(tmp_path, proc="p", run_id=lambda: None)
    started = time.perf_counter()
    for i in range(10000):
        tracer.event("e", i=i, text="some text")
    per_event_us = (time.perf_counter() - started) / 10000 * 1e6
    assert per_event_us < 50, per_event_us


class FakeStore:
    def __init__(self, fail=False):
        self.puts, self.fail = [], fail

    def put(self, name, data):
        if self.fail:
            raise OSError("no network")
        self.puts.append((name, data))


def test_uploader_uploads_changed_files_and_cleans_up(tmp_path):
    tracer = tracing.Tracer(tmp_path, proc="captain", run_id=lambda: None)
    tracer.event("e")
    tracer.flush()
    store = FakeStore()
    uploader = tracing.Uploader(tmp_path, store, keep_s=3600)
    assert uploader.pass_once() == 1 and uploader.pass_once() == 0  # unchanged: not again
    name = store.puts[0][0]
    assert name.startswith("mission-control/2") and name.endswith(".jsonl")
    tracer.event("e")
    tracer.flush()
    assert uploader.pass_once() == 1  # appended to: uploaded whole again
    path = next(tmp_path.rglob("*.jsonl"))
    assert uploader.pass_once(now=time.time() + 7200) == 0 and not path.exists()  # old and uploaded: deleted


def test_uploader_keeps_files_it_couldnt_upload(tmp_path):
    tracer = tracing.Tracer(tmp_path, proc="captain", run_id=lambda: None)
    tracer.event("e")
    tracer.flush()
    uploader = tracing.Uploader(tmp_path, FakeStore(fail=True), keep_s=0)
    assert uploader.pass_once(now=time.time() + 7200) == 0 and next(tmp_path.rglob("*.jsonl")).exists()


def test_off():
    assert tracing.trace_dir() == (os.environ.get("TRACE_TEST_DIR") or "off")  # conftest.py
    tracing.event("anything", x=1)
