"""python -m pytest -q telemetry_service      (from software/mission_control)"""

import json
import os
from datetime import datetime, timezone

import pytest

from telemetry_service import TelemetryProvider
from telemetry_service.files import file_name, telemetry_files
from telemetry_service.mqtt_reader import TelemetryFiles, record_line
from telemetry_service.telemetry_provider import topic_matches

T0 = 1793030400  # a period start (unix seconds)
TEL = "audrey/boat1/telemetry"


def telemetry(seq):
    return json.dumps({"seq": seq, "gps": {"lat_e7": 1, "lon_e7": 2}}).encode()


@pytest.fixture
def folder(tmp_path):
    return str(tmp_path / "telemetry")


def test_record_line_is_the_logger_format():
    rec = json.loads(record_line(TEL, telemetry(7), now=T0 + 1.2345))
    t = datetime.fromtimestamp(T0 + 1.234, timezone.utc).isoformat(timespec="milliseconds")
    assert rec == {"t": t, "unix": T0 + 1.234, "topic": TEL, "data": {"seq": 7, "gps": {"lat_e7": 1, "lon_e7": 2}}}
    raw = json.loads(record_line("audrey/boat1/cmd", b"not json", retained=True, now=T0))
    assert raw["raw"] == "not json" and raw["retained"] is True and "data" not in raw


def test_files_rotate_by_period_and_keep_the_newest(folder):
    files = TelemetryFiles(folder, file_seconds=30, keep=20)
    for i in range(25):  # 25 periods, one message each
        files.write(record_line(TEL, telemetry(i), now=T0 + 30 * i + 1), now=T0 + 30 * i + 1)
    names = telemetry_files(folder)
    assert len(names) == 20 and names[0] == file_name(T0 + 30 * 5) and names[-1] == file_name(T0 + 30 * 24)
    files.write(record_line(TEL, telemetry(99)), now=T0 + 30 * 24 + 5)  # same period: same file
    assert len(telemetry_files(folder)) == 20
    files.prune(now=T0 + 30 * 40)  # no messages for a while: still only the newest 20 kept
    assert len(telemetry_files(folder)) == 20


def test_provider_reads_only_whats_new(folder):
    files = TelemetryFiles(folder)
    provider = TelemetryProvider(folder)
    assert provider.messages() == [] and provider.latest_data() is None
    for i in range(3):
        files.write(record_line(TEL, telemetry(i), now=T0 + i), now=T0 + i)
    assert [m["data"]["seq"] for m in provider.messages()] == [0, 1, 2]
    files.write(record_line(TEL, telemetry(3), now=T0 + 3), now=T0 + 3)
    assert [m["data"]["seq"] for m in provider.messages()] == [0, 1, 2, 3]  # appended, nothing read twice
    assert provider.latest_data(TEL) == {"seq": 3, "gps": {"lat_e7": 1, "lon_e7": 2}}


def test_provider_waits_for_a_line_still_being_written(folder):
    os.makedirs(folder)
    path = os.path.join(folder, file_name(T0))
    provider = TelemetryProvider(folder)
    whole = record_line(TEL, telemetry(1), now=T0)
    with open(path, "w") as f:
        f.write(whole + "\n" + whole[:20])  # the second line half written
    assert len(provider.messages()) == 1
    with open(path, "a") as f:
        f.write(whole[20:] + "\n")
    assert len(provider.messages()) == 2


def test_provider_forgets_files_the_reader_deleted(folder):
    files = TelemetryFiles(folder, file_seconds=30, keep=3)
    provider = TelemetryProvider(folder)
    for i in range(3):
        files.write(record_line(TEL, telemetry(i), now=T0 + 30 * i), now=T0 + 30 * i)
    assert [m["data"]["seq"] for m in provider.messages()] == [0, 1, 2]
    files.write(record_line(TEL, telemetry(3), now=T0 + 90), now=T0 + 90)  # a 4th period: the oldest file goes
    assert [m["data"]["seq"] for m in provider.messages()] == [1, 2, 3]


def test_provider_queries(folder):
    files = TelemetryFiles(folder)
    files.write(record_line(TEL, telemetry(1), now=T0 + 1), now=T0 + 1)
    files.write(record_line("audrey/boat1/cmd", b'{"cmd": "destination"}', now=T0 + 2), now=T0 + 2)
    files.write(record_line(TEL, telemetry(2), now=T0 + 3), now=T0 + 3)
    provider = TelemetryProvider(folder)
    assert [m["data"]["seq"] for m in provider.latest(TEL, n=5)] == [2, 1]  # newest first
    assert provider.latest("audrey/boat1/cmd")[0]["data"] == {"cmd": "destination"}
    assert [m["topic"] for m in provider.since(T0 + 1.5)] == ["audrey/boat1/cmd", TEL]
    assert len(provider.messages("audrey/boat1/#")) == 3 and provider.messages("audrey/boat2/#") == []


def test_topic_matches():
    assert topic_matches("audrey/boat1/telemetry", TEL) and not topic_matches("audrey/boat1/cmd", TEL)
    assert topic_matches("audrey/boat1/#", TEL) and topic_matches("audrey/#", TEL) and topic_matches("#", TEL)
    assert topic_matches("audrey/+/telemetry", TEL) and not topic_matches("audrey/+", TEL)
    assert not topic_matches("audrey/boat1/telemetry/x", TEL) and topic_matches(None, TEL)
