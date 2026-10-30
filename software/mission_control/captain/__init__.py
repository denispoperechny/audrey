"""The captain: an LLM agent (agent.py) woken 15 s after its previous wake-up ends with the mission and the boat's state
(state.py), acting through the boat's MCP tools (captain.py runs it)."""

import settings  # noqa: F401  (reads .env before anything looks at the environment)
