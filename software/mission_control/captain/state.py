"""The boat's state for the captain, worked out from the latest telemetry so the LLM doesn't have
to: position, heading, speed, what the autopilot is doing, and where the destination is."""

import time

from boat_mcp.boat import TELEMETRY_TOPIC
from geo import course_between, relative_from_azimuth

KNOT_M_S = 0.514444


def boat_state(telemetry, now=None):
    """A compact dict of the boat's state, or {"telemetry": "none"} if there is none."""
    now = time.time() if now is None else now
    newest = telemetry.latest(TELEMETRY_TOPIC, n=1)
    if not newest:
        return {"telemetry": "none: the boat isn't reporting (off, or not connected)"}
    return state_from_message(newest[0], now)


def state_from_message(message, now):
    """The same, from one telemetry message (as telemetry_service gives them)."""
    data = message.get("data") or {}
    gps, compass, nav, bcu = (data.get(k) or {} for k in ("gps", "compass", "nav", "bcu"))
    state = {"telemetry_age_s": round(now - message.get("unix", now), 1)}

    if gps.get("fix_valid") and gps.get("lat_e7") is not None:
        lat, lon = gps["lat_e7"] / 1e7, gps["lon_e7"] / 1e7
        state["position"] = {"lat": round(lat, 7), "lon": round(lon, 7)}
        if gps.get("sog_kn") is not None:
            state["speed_m_s"] = round(gps["sog_kn"] * KNOT_M_S, 2)
    else:
        lat = lon = None
        state["position"] = "no GPS fix"

    heading = (compass.get("virtual") or {}).get("heading")
    if heading is None:
        heading = compass.get("heading")
    state["heading_deg"] = None if heading is None else round(heading % 360, 1)  # degrees from north, clockwise

    if bcu.get("esp32_mode") is not None:
        state["control"] = "autopilot" if bcu.get("esp32_mode") else "remote control (RC): the autopilot can't drive"
    state["autopilot"] = nav.get("state") or (data.get("output") or {}).get("reason") or "unknown"

    destination = data.get("destination")
    if destination and lat is not None:
        d_lat, d_lon = destination["lat_e7"] / 1e7, destination["lon_e7"] / 1e7
        azimuth, distance = course_between(lat, lon, d_lat, d_lon)
        state["destination"] = {"lat": round(d_lat, 7), "lon": round(d_lon, 7), "distance_m": round(distance, 1),
                                "azimuth_deg": round(azimuth, 1)}
        if heading is not None:  # where it is relative to the bow: + right, - left
            state["destination"]["relative_bearing_deg"] = round(relative_from_azimuth(heading, azimuth), 1)
    else:
        state["destination"] = None
    if bcu.get("battery_mv"):
        state["battery_v"] = round(bcu["battery_mv"] / 1000, 2)
    return state
