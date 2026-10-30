"""The telemetry folder both sides share: mqtt_reader.py writes it, TelemetryProvider reads it.

    <TELEMETRY_DIR>/telemetry_YYYYMMDDTHHMMSSZ.jsonl      one file per TELEMETRY_FILE_SECONDS

The name is the start of the file's period (UTC), so names sort in time order. One line per
MQTT message, the MQTT logger's format (software/mqtt_logger):

    {"t": "2026-10-29T15:42:07.123+00:00", "unix": 1792..., "topic": "audrey/boat1/telemetry", "data": {...}}

("raw": "<text>" instead of "data" if the payload wasn't JSON.)
"""

import os
from datetime import datetime, timezone

DEFAULT_DIR = "/tmp/audrey-telemetry"
DEFAULT_FILE_SECONDS = 30
DEFAULT_KEEP_FILES = 20
PREFIX, SUFFIX = "telemetry_", ".jsonl"


def telemetry_dir():
    return os.environ.get("TELEMETRY_DIR") or DEFAULT_DIR


def file_name(period_start):
    """The file for the period starting at `period_start` (unix seconds)."""
    return PREFIX + datetime.fromtimestamp(period_start, timezone.utc).strftime("%Y%m%dT%H%M%SZ") + SUFFIX


def telemetry_files(folder):
    """The telemetry files in the folder, oldest first."""
    try:
        names = os.listdir(folder)
    except FileNotFoundError:
        return []
    return sorted(n for n in names if n.startswith(PREFIX) and n.endswith(SUFFIX))
