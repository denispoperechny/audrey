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
# The conversation's size: once it passes HISTORY_TRIGGER_TOKENS, deepagents summarizes the
# older part (one extra model call) and keeps about the last HISTORY_KEEP_TOKENS as they were
# (the evicted messages go to conversation_history/ under the agent's home). Its own default
# for this model would be 85% / 10% of the 1M-token context: never reached in a mission, while
# every wake-up re-sends the whole conversation.
HISTORY_TRIGGER_TOKENS = 60_000
HISTORY_KEEP_TOKENS = 22_500
# What a summary has to keep for the captain, added to deepagents' own summary prompt. The
# mission itself is in every wake-up message, and the captain's progress notes too
# (progress.py), so neither depends on the summary; this is the second line of defence.
MISSION_SUMMARY_SECTION = """## MISSION PROGRESS

The mission (each wake-up message repeats it) and how far it has got: every waypoint or leg
already reached, with the time; the current target and the leg the boat is on; anything learned on
the way that still matters (hazards with their positions or azimuths, shores, a detour chosen and
why, failed looks, battery concerns, anything the operator said). Be exact with coordinates and
directions. Never mark a waypoint as reached unless the conversation shows it was.

"""

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
send navigation commands: say what you see and wait. When you act, say in one line why.

When the whole mission is done (the boat at its final point), stop() the boat and call
mission_complete with a short summary: that ends the run, so you aren't woken up for nothing.
Don't use it to give up: if the mission can't be finished safely, stop() and say why.

Older parts of this conversation are summarized from time to time. The mission and your mission
progress notes are on every wake-up, so keep the notes current with update_mission_progress
whenever something changes: a waypoint reached, a hazard located, a plan changed."""


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


def summary_prompt():
    from deepagents.middleware.summarization import DEEPAGENTS_DEFAULT_SUMMARY_PROMPT

    marker = "## NEXT STEPS"
    if marker not in DEEPAGENTS_DEFAULT_SUMMARY_PROMPT:  # a deepagents update changed it: put ours first
        return MISSION_SUMMARY_SECTION + DEEPAGENTS_DEFAULT_SUMMARY_PROMPT
    return DEEPAGENTS_DEFAULT_SUMMARY_PROMPT.replace(marker, MISSION_SUMMARY_SECTION + marker, 1)


def build_agent(chat_model, tools, sleep_s, home=HOME, history_trigger_tokens=HISTORY_TRIGGER_TOKENS,
                history_keep_tokens=HISTORY_KEEP_TOKENS):
    from deepagents import create_deep_agent
    from deepagents.backends import FilesystemBackend
    from deepagents.middleware.summarization import SummarizationMiddleware
    from langgraph.checkpoint.memory import InMemorySaver

    from captain.geo_tools import TOOLS as GEO_TOOLS

    (Path(home) / "skills").mkdir(parents=True, exist_ok=True)
    backend = FilesystemBackend(root_dir=str(home), virtual_mode=True)
    # Same name as deepagents' own summarization middleware, so it replaces it in place.
    history = SummarizationMiddleware(chat_model, backend=backend, trigger=("tokens", history_trigger_tokens),
                                      keep=("tokens", history_keep_tokens), summary_prompt=summary_prompt())
    return create_deep_agent(
        model=chat_model,
        tools=list(tools) + GEO_TOOLS,
        system_prompt=SYSTEM_PROMPT.format(sleep=sleep_s),
        skills=["/skills/"],
        backend=backend,
        checkpointer=InMemorySaver(),
        middleware=[history],
        name="captain",
    )
