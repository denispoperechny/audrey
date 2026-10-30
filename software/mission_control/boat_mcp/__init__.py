"""The boat as MCP tools (server.py): start_navigation, stop. boat.py does the work: it turns a
direction and distance into a destination, sends the commands over MQTT and confirms them from
the telemetry."""

import settings  # noqa: F401  (reads .env before anything looks at the environment)
