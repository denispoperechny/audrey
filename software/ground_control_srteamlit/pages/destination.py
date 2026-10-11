"""Destination page: type in where the boat should go and send it over MQTT.

The boat keeps the destination it was last sent and reports it back in telemetry, which is
what "The boat's destination" below shows: it changes only once the boat has the command.
"""

import math
import time

import streamlit as st

from telemetry import get_feed

REFRESH_S = 1
STALE_S = 5  # no telemetry for this long = what the boat reports is the last known state
EARTH_RADIUS_M = 6_371_000


def distance_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in meters."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def format_distance(meters):
    return "%.0f m" % meters if meters < 1000 else "%.1f km" % (meters / 1000)


def boat_position(latest):
    """(lat, lon) of the boat's current fix, or None."""
    gps = (latest or {}).get("gps") or {}
    if not gps.get("fix_valid") or gps.get("lat_e7") is None:
        return None
    return gps["lat_e7"] / 1e7, gps["lon_e7"] / 1e7


st.set_page_config(page_title="Audrey destination", layout="wide")
st.title("Destination")

feed = get_feed()

if "dest_lat" not in st.session_state:
    # Start from where the boat is, if it's known: nearby coordinates are quicker to edit.
    start = boat_position(feed.snapshot()["latest"]) or (0.0, 0.0)
    st.session_state["dest_lat"], st.session_state["dest_lon"] = start

left, right = st.columns(2)
lat = left.number_input("Latitude (degrees, north positive)", min_value=-90.0, max_value=90.0,
                        step=0.0001, format="%.7f", key="dest_lat")
lon = right.number_input("Longitude (degrees, east positive)", min_value=-180.0, max_value=180.0,
                         step=0.0001, format="%.7f", key="dest_lon")

send, clear, _ = st.columns([1, 1, 3])
if send.button("Send destination to the boat", type="primary"):
    try:
        feed.send_destination(lat, lon)
        st.success("Sent %.7f, %.7f. It shows below once the boat has it." % (lat, lon))
    except RuntimeError as e:
        st.error("Not sent: %s." % e)
if clear.button("Clear the boat's destination"):
    try:
        feed.clear_destination()
        st.success("Sent. It shows below once the boat has it.")
    except RuntimeError as e:
        st.error("Not sent: %s." % e)


@st.fragment(run_every=REFRESH_S)
def boat_state():
    snap = feed.snapshot()
    latest = snap["latest"]
    age = None if snap["received_at"] is None else time.time() - snap["received_at"]

    st.subheader("The boat's destination")
    status = "MQTT connected" if snap["connected"] else "MQTT disconnected"
    if snap["error"]:
        status += " (%s)" % snap["error"]
    st.caption(status)
    if latest is None:
        st.info("Waiting for telemetry from the boat...")
        return
    if age > STALE_S:
        st.error("No telemetry for %d s: this is the last known state." % age)

    if "destination" not in latest:
        st.warning("The boat's software doesn't report a destination (it predates this page).")
        return
    destination = latest["destination"]
    if destination is None:
        st.info("None set.")
        return
    dest_lat, dest_lon = destination["lat_e7"] / 1e7, destination["lon_e7"] / 1e7
    position = boat_position(latest)
    st.metric("Destination", "%.7f, %.7f" % (dest_lat, dest_lon))
    if position is None:
        st.caption("Distance unknown: the boat has no GPS fix.")
    else:
        st.caption("%s from the boat." % format_distance(distance_m(position[0], position[1], dest_lat, dest_lon)))
    nav = latest.get("nav")
    if nav:
        st.caption("The boat's navigation: %s, %s to go, course to steer %.0f°, course error %+.0f°." % (
            nav["state"], format_distance(nav["distance_m"]), nav["target_course_deg"], nav["course_error_deg"]))
    output = latest.get("output") or {}
    if output.get("reason"):
        st.caption("The boat's control: throttle %s, rudder %s (%s)." % (
            output.get("throttle"), output.get("rudder"), output["reason"]))


boat_state()
