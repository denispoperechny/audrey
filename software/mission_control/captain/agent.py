"""The captain's agent: a LangChain deep agent (deepagents) with the boat's MCP tools.

    model    gpt-6-astra in Azure AI Foundry (FOUNDRY_OPENAI_URL, FOUNDRY_MODEL), signed in with
             DefaultAzureCredential (your az login locally, the container's managed identity in
             Azure: "Cognitive Services OpenAI User" on the Foundry resource)
    tools    the boat_mcp server's tools (BOAT_MCP_URL, default http://localhost:8100/mcp):
             start_navigation, navigate_to, stop, visual_observe; and calculation tools
             (geo_tools.py) for converting between positions and directions
    skills   folders with a SKILL.md under captain/home/skills/ (CAPTAIN_HOME to move home);
             the agent's own file tools work inside that home folder only
    memory   the conversation is kept between wake-ups (in memory: a restart starts it afresh)
"""

import os
from datetime import timedelta
from pathlib import Path

DEFAULT_OPENAI_URL = "https://temp-test-01-resource.openai.azure.com/openai/v1/"
DEFAULT_MODEL = "gpt-6-astra"
DEFAULT_BOAT_MCP_URL = "http://localhost:8100/mcp"
HOME = Path(os.environ.get("CAPTAIN_HOME") or Path(__file__).resolve().parent / "home")
MODEL_TIMEOUT_S = 60
TOOL_TIMEOUT_S = 120  # visual_observe can take ~75 s

SYSTEM_PROMPT = """You are the captain of Audrey, a small autonomous boat. You are in charge of
getting her to the mission's destination safely: safety comes before speed.

You are woken up again {sleep:g} seconds after each wake-up ends (each wake-up says when the
previous one started; judge progress by time, not by counting wake-ups). Each wake-up brings the
mission, the boat's current state, worked out from her telemetry (position, heading, speed, what her autopilot
is doing, and the destination's distance and bearing, also relative to the bow: + right, - left),
and the lookout's camera notes since the previous wake-up (another AI looks at the camera's
picture every 10 seconds: hazards with their direction and rough distance, how far ahead looks
clear). The notes only see what is in front of the bow; when you need more, look yourself. Between wake-ups the boat's own autopilot keeps steering to the destination you
gave it and stops by itself on arrival (within 4 m). So most wake-ups need no action: only act
when something should change. Keep each wake-up short: a few words when nothing changes.

Your authority, through your tools:
- start_navigation(azimuth, distance): go that far in that direction (degrees from true north).
- navigate_to(lat, lon): go straight to a known position (a waypoint, the destination).
- stop(): hold still.
- visual_observe(original_gps, azimuth, request_note): look in a direction with the camera and
  ask about it. It stops the navigation, takes the boat there and back, and leaves it holding
  still at original_gps: send it on afterwards. Use it when you need to see, not every time.

Don't do position or direction arithmetic in your head: use the calculation tools
(gps_from_course, course_between, relative_from_azimuth, azimuth_from_relative,
split_into_legs). They are exact and fast.

If the telemetry is missing or stale, the GPS has no fix, or the boat is on remote control, don't
send navigation commands: say what you see and wait. When you act, say in one line why."""


def model():
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from langchain_openai import ChatOpenAI

    token = get_bearer_token_provider(DefaultAzureCredential(exclude_interactive_browser_credential=True),
                                      "https://cognitiveservices.azure.com/.default")
    return ChatOpenAI(model=os.environ.get("FOUNDRY_MODEL") or DEFAULT_MODEL,
                      base_url=os.environ.get("FOUNDRY_OPENAI_URL") or DEFAULT_OPENAI_URL,
                      api_key=token, use_responses_api=True, timeout=MODEL_TIMEOUT_S, max_retries=1)


async def boat_tools():
    """The boat_mcp server's tools as LangChain tools. Raises if the server isn't reachable."""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient({"boat": {
        "transport": "streamable_http", "url": os.environ.get("BOAT_MCP_URL") or DEFAULT_BOAT_MCP_URL,
        "timeout": timedelta(seconds=TOOL_TIMEOUT_S), "sse_read_timeout": timedelta(seconds=TOOL_TIMEOUT_S)}})
    return await client.get_tools()


def build_agent(chat_model, tools, sleep_s, home=HOME):
    from deepagents import create_deep_agent
    from deepagents.backends import FilesystemBackend
    from langgraph.checkpoint.memory import InMemorySaver

    from captain.geo_tools import TOOLS as GEO_TOOLS

    (Path(home) / "skills").mkdir(parents=True, exist_ok=True)
    return create_deep_agent(
        model=chat_model,
        tools=list(tools) + GEO_TOOLS,
        system_prompt=SYSTEM_PROMPT.format(sleep=sleep_s),
        skills=["/skills/"],
        backend=FilesystemBackend(root_dir=str(home), virtual_mode=True),
        checkpointer=InMemorySaver(),
        name="captain",
    )
