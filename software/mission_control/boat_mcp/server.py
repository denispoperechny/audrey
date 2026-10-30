"""The boat as an MCP server: tools the mission control's LLM loops (or any MCP client) use to
command it.

    python -m boat_mcp.server          (from software/mission_control)
    -> http://localhost:8100/mcp       (streamable HTTP, no auth; BOAT_MCP_PORT to change the port)

Tools:
    start_navigation(azimuth, distance)   go `distance` metres from where the boat is now, towards
                                          `azimuth` (degrees clockwise from true north)
    navigate_to(lat, lon)                 go straight to that point (5-300 m from where the boat is)
    stop()                                forget the destination: the boat holds neutral
    visual_observe(original_gps, azimuth, request_note)
                                          turn to `azimuth`, take the camera's picture, go back to
                                          original_gps (and hold there), and have the vision model
                                          answer request_note about the picture (observe.py)

The tools run in worker threads, so a long one (visual_observe) doesn't hold up the others.

Each tool sends the command over MQTT and waits a few seconds for the boat's telemetry to show
it, reporting "accepted" (boat.py). The position comes from the telemetry folder the
telemetry_reader process writes (telemetry_service). Runs as its own process (supervisord.conf).
"""

import contextlib
import os
import time

import anyio

# mcp 1.x (FastMCP): langchain-mcp-adapters, which the captain uses in this same container,
# requires mcp < 2. (The camera service, in its own container, is on mcp 2.x: MCPServer.)
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

from boat_mcp.boat import MAX_DISTANCE_M, MIN_DISTANCE_M, Boat, BoatError
import tracing
from telemetry_service import TelemetryProvider

DEFAULT_PORT = 8100

mcp = FastMCP("audrey-boat", instructions="Commands for Audrey, a small autonomous boat: start_navigation sends it "
              "a given distance in a given direction from where it is now; stop makes it hold still.",
              streamable_http_path="/mcp", stateless_http=True, json_response=True,
              # The Host check is for browsers on localhost; other containers or machines call this.
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
_boat = None  # created on first use: the MQTT connection, and so the settings, only when needed
_observer = None


def boat():
    global _boat
    if _boat is None:
        from boat_mcp.boat import MqttPublisher

        _boat = Boat(MqttPublisher(), TelemetryProvider())
    return _boat


def observer():
    global _observer
    if _observer is None:
        from boat_mcp import vision
        from boat_mcp.camera import CameraClient
        from boat_mcp.observe import VisualObserver

        _observer = VisualObserver(boat(), CameraClient(), vision.ask)
    return _observer


async def in_thread(tool, args, function, *call_args):
    """A tool's work in a worker thread; a BoatError becomes the tool's error. Traced: the
    arguments, the result or error, how long it took."""
    started = time.monotonic()
    try:
        result = await anyio.to_thread.run_sync(lambda: function(*call_args))
    except BoatError as e:
        tracing.event("tool", tool=tool, args=args, error=str(e), took_s=round(time.monotonic() - started, 2))
        raise ToolError(str(e))
    except Exception as e:
        tracing.event("tool", tool=tool, args=args, error="%s: %s" % (type(e).__name__, e), took_s=round(time.monotonic() - started, 2))
        raise
    tracing.event("tool", tool=tool, args=args, result=result, took_s=round(time.monotonic() - started, 2))
    return result


class Position(BaseModel):
    lat: float
    lon: float


class Start(Position):
    fix_age_s: float = Field(description="how old the GPS fix was, in seconds")


class NavigationStarted(BaseModel):
    accepted: bool = Field(description="the boat's telemetry showed the new destination")
    azimuth_deg: float = Field(description="the direction used, degrees clockwise from true north")
    distance_m: float
    start: Start = Field(description="where the boat was")
    destination: Position
    note: str


class Stopped(BaseModel):
    accepted: bool = Field(description="the boat's telemetry showed it has no destination")
    note: str


class Observation(BaseModel):
    answer: str = Field(description="the vision model's answer to request_note")
    azimuth_deg: float = Field(description="where the boat (and the camera) pointed, degrees from true north")
    picture: str = Field(description="the camera service's name for the picture")
    picture_url: str
    picture_taken_at: str = Field(description="when the picture was taken (UTC)")
    turned_in_s: float = Field(description="how long the boat took to turn to the azimuth")
    picture_after_aligned_s: float = Field(description="how long after it was on the azimuth the picture was taken")
    back_in_s: float = Field(description="how long the boat took to get back to original_gps")
    back_at: Position = Field(description="original_gps: the boat is there and holds still")
    note: str


START_NAVIGATION = """Sends the boat `distance` metres from where it is now, in the direction `azimuth`, and
waits a few seconds for its telemetry to confirm. The boat steers there by itself and stops
(holds neutral) when it arrives within 4 m.

azimuth: degrees clockwise from true north (0 = north, 90 = east, 180 = south, 270 = west)
distance: metres, %g to %g

Needs a GPS fix from the last few seconds. Returns whether the boat accepted it, where it
was, and the destination it was given.""" % (MIN_DISTANCE_M, MAX_DISTANCE_M)


@mcp.tool(name="start_navigation", description=START_NAVIGATION, structured_output=True)
async def start_navigation(azimuth: float, distance: float) -> NavigationStarted:
    return NavigationStarted(**await in_thread("start_navigation", {"azimuth": azimuth, "distance": distance},
                                                 boat().start_navigation, azimuth, distance))


@mcp.tool(name="navigate_to", structured_output=True)
async def navigate_to(lat: float, lon: float) -> NavigationStarted:
    """Sends the boat straight to the point (lat, lon), and waits a few seconds for its telemetry to
    confirm. Use it for a known position (a waypoint, the final destination); start_navigation is
    for "this far in that direction". The boat steers there by itself and stops (holds neutral)
    within 4 m of it. The point has to be 5 to 300 m from where the boat is now.

    Needs a GPS fix from the last few seconds. Returns whether the boat accepted it, where it was,
    and the azimuth and distance to the point from there.
    """
    return NavigationStarted(**await in_thread("navigate_to", {"lat": lat, "lon": lon}, boat().navigate_to, lat, lon))


@mcp.tool(name="stop", structured_output=True)
async def stop() -> Stopped:
    """Makes the boat forget its destination, so it stops driving and holds neutral; waits a few
    seconds for its telemetry to confirm."""
    return Stopped(**await in_thread("stop", {}, boat().stop))


@mcp.tool(name="visual_observe", structured_output=True)
async def visual_observe(original_gps: Position, azimuth: float, request_note: str) -> Observation:
    """Looks in a direction with the boat's forward camera and answers a question about what it sees.

    THIS STOPS THE BOAT'S NAVIGATION: the boat turns to `azimuth` and runs on that course for at
    most 4 seconds while the camera takes the picture, then goes back to `original_gps`, where it
    holds still. The tool returns only once the boat is back there. Its earlier destination is
    gone; send it on with start_navigation afterwards.

    original_gps: where to return to, {lat, lon} (e.g. the boat's position before calling this)
    azimuth: the direction to look, degrees clockwise from true north (0 = north, 90 = east)
    request_note: what to look for or answer about the picture (e.g. "is the channel ahead clear of
        boats and buoys?", "how far is the shore, roughly?")

    Errors (each returns only after the boat is back at original_gps): the boat didn't turn to the
    azimuth within 10 s; no camera picture taken during the 4 s on course arrived by the time the
    boat was back; the vision model didn't answer within 30 s; the boat didn't get back within 60 s
    (the model's answer is in the message). Usually takes 10-40 s, up to about 75 s: allow for that
    in the client's timeout.
    """
    return Observation(**await in_thread(
        "visual_observe", {"original_gps": original_gps.model_dump(), "azimuth": azimuth, "request_note": request_note},
        observer().observe, original_gps.lat, original_gps.lon, azimuth, request_note))


def app():
    """The ASGI app: MCP at /mcp."""
    # A fresh session manager for each app built (one can only run once; the tests build several).
    mcp._session_manager = None
    starlette_app = mcp.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with mcp.session_manager.run():
            yield

    starlette_app.router.lifespan_context = lifespan
    return starlette_app


def main():
    import uvicorn

    uvicorn.run(app(), host="0.0.0.0", port=int(os.environ.get("BOAT_MCP_PORT") or DEFAULT_PORT))


if __name__ == "__main__":
    main()
