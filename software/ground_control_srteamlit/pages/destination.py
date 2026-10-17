"""Destination page: type in where the boat should go and send it over MQTT, or pick a
saved position.

The boat keeps the destination it was last sent and reports it back in telemetry, which is
what "The boat's destination" below shows: it changes only once the boat has the command.

Saved positions (positions.py) are kept on the computer running this app, not on the boat:
typed-in coordinates or the boat's current position, named A, B, C... until renamed.
"""

import math
import time

import streamlit as st

import positions
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


def boat_position_e7(latest):
    """(lat_e7, lon_e7) of the boat's current fix, or None."""
    gps = (latest or {}).get("gps") or {}
    if not gps.get("fix_valid") or gps.get("lat_e7") is None:
        return None
    return gps["lat_e7"], gps["lon_e7"]


# ---- saved positions: button callbacks (they run before the page is redrawn) ----------

def flash(kind, text):
    """A message shown once, under the saving controls."""
    st.session_state["flash"] = (kind, text)


def save_typed():
    lat, lon = st.session_state["dest_lat"], st.session_state["dest_lon"]
    try:
        entry = positions.add(round(lat * 1e7), round(lon * 1e7), st.session_state["save_name"])
    except ValueError as e:
        flash("error", "Not saved: %s." % e)
        return
    st.session_state["save_name"] = ""
    flash("success", "Saved %.7f, %.7f as %s." % (lat, lon, entry["name"]))


def save_current():
    position = boat_position_e7(get_feed().snapshot()["latest"])
    if position is None:
        flash("error", "Not saved: the boat has no GPS fix right now.")
        return
    try:
        entry = positions.add(position[0], position[1], st.session_state["save_name"])
    except ValueError as e:
        flash("error", "Not saved: %s." % e)
        return
    st.session_state["save_name"] = ""
    flash("success", "Saved the boat's position (%.7f, %.7f) as %s." % (position[0] / 1e7, position[1] / 1e7, entry["name"]))


def send_saved(entry):
    try:
        get_feed().send_destination(entry["lat_e7"] / 1e7, entry["lon_e7"] / 1e7)
    except RuntimeError as e:
        flash("error", "%s not sent: %s." % (entry["name"], e))
        return
    flash("success", "Sent %s to the boat. It shows below once the boat has it." % entry["name"])


def use_saved(entry):
    st.session_state["dest_lat"] = entry["lat_e7"] / 1e7
    st.session_state["dest_lon"] = entry["lon_e7"] / 1e7
    flash("success", "%s's coordinates are in the fields above." % entry["name"])


def rename_saved(entry, key):
    try:
        positions.rename(entry["id"], st.session_state[key])
    except ValueError as e:
        flash("error", "Not renamed: %s." % e)
        return
    flash("success", "Renamed %s to %s." % (entry["name"], st.session_state[key].strip()))


def delete_saved(entry):
    positions.delete(entry["id"])
    flash("success", "Deleted %s (%.7f, %.7f)." % (entry["name"], entry["lat_e7"] / 1e7, entry["lon_e7"] / 1e7))


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

st.subheader("Saved positions")
st.caption("Kept on this computer only, for picking a destination without typing it in.")
name_col, typed_col, current_col = st.columns([2, 1, 1], vertical_alignment="bottom")
name_col.text_input("Name (optional; the next free letter if left empty)", key="save_name")
typed_col.button("Save the coordinates above", on_click=save_typed, use_container_width=True)
current_col.button("Save the boat's current position", on_click=save_current, use_container_width=True,
                   disabled=boat_position(feed.snapshot()["latest"]) is None,
                   help="Needs a GPS fix from the boat.")
if "flash" in st.session_state:
    kind, text = st.session_state.pop("flash")
    (st.success if kind == "success" else st.error)(text)

saved = positions.load()
if not saved:
    st.caption("None saved yet.")
for entry in saved:
    key = entry["id"]
    name, coords, send_col, use_col, rename_col, delete_col = st.columns([1.5, 3, 1, 1, 1, 1], vertical_alignment="center")
    name.markdown("**%s**" % entry["name"].replace("*", "\\*").replace("_", "\\_"))
    coords.text("%.7f, %.7f" % (entry["lat_e7"] / 1e7, entry["lon_e7"] / 1e7))
    send_col.button("Send", key="send_" + key, type="primary", on_click=send_saved, args=(entry,),
                    use_container_width=True, help="Send the boat to %s" % entry["name"])
    use_col.button("Edit", key="use_" + key, on_click=use_saved, args=(entry,), use_container_width=True,
                   help="Put these coordinates in the fields at the top, to adjust them")
    with rename_col.popover("Rename", use_container_width=True):
        st.text_input("New name", value=entry["name"], key="name_" + key)
        st.button("Rename", key="rename_" + key, on_click=rename_saved, args=(entry, "name_" + key))
    with delete_col.popover("Delete", use_container_width=True):
        st.write("Delete %s?" % entry["name"])
        st.button("Delete", key="delete_" + key, on_click=delete_saved, args=(entry,))


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
    named = positions.find(positions.load(), destination["lat_e7"], destination["lon_e7"])
    st.metric("Destination" + (" %s" % named["name"] if named else ""), "%.7f, %.7f" % (dest_lat, dest_lon))
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
