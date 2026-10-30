"""The boat's telemetry for mission control: mqtt_reader.py (its own process) writes the latest
MQTT messages to a local folder, TelemetryProvider reads them back for the other processes."""

import settings  # noqa: F401  (reads .env before anything looks at the environment)

from telemetry_service.telemetry_provider import TelemetryProvider

__all__ = ["TelemetryProvider"]
