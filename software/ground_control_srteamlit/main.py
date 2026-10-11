"""Audrey ground control: live battery, compass and position of the boat, from MQTT.

    pip install -r requirements.txt
    streamlit run main.py

Telemetry arrives once a second (see telemetry.py); the page refreshes itself every
REFRESH_S. Broker settings: see telemetry.load_mqtt_config(). The destination is set on
its own page (pages/destination.py).
"""

import math
import time

import pydeck as pdk
import streamlit as st

from telemetry import get_feed

REFRESH_S = 1
STALE_S = 5  # no telemetry for this long = show the data as stale
HEADING_LINE_M = 25  # length of the heading line drawn from the boat on the map

# Resting voltage per LiPo / Li-ion cell -> % charge. Under load the voltage sags, so the
# estimate reads low while the motors run.
LIPO_CURVE = (
    (3.27, 0), (3.61, 5), (3.69, 10), (3.71, 15), (3.73, 20), (3.75, 25), (3.77, 30),
    (3.79, 35), (3.80, 40), (3.82, 45), (3.84, 50), (3.85, 55), (3.87, 60), (3.91, 65),
    (3.95, 70), (3.98, 75), (4.02, 80), (4.08, 85), (4.11, 90), (4.15, 95), (4.20, 100),
)

COMPASS_POINTS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
ACCURACY_NAMES = ("unreliable", "low", "medium", "high")


def battery_percent(cell_v):
    if cell_v <= LIPO_CURVE[0][0]:
        return 0
    for (v0, p0), (v1, p1) in zip(LIPO_CURVE, LIPO_CURVE[1:]):
        if cell_v <= v1:
            return round(p0 + (p1 - p0) * (cell_v - v0) / (v1 - v0))
    return 100


def ago(seconds):
    if seconds < 60:
        return "%d s ago" % seconds
    if seconds < 3600:
        return "%d min ago" % (seconds // 60)
    return "%.1f h ago" % (seconds / 3600)


def compass_svg(heading, active):
    """A compass rose with a needle pointing at `heading` (degrees, 0 = north, clockwise)."""
    needle = "#d62728" if active else "#999999"
    ticks = []
    for deg in range(0, 360, 30):
        rad = math.radians(deg)
        inner = 78 if deg % 90 else 70
        ticks.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="currentColor" stroke-width="2"/>' % (
            100 + inner * math.sin(rad), 100 - inner * math.cos(rad),
            100 + 88 * math.sin(rad), 100 - 88 * math.cos(rad)))
    labels = "".join(
        '<text x="%d" y="%d" text-anchor="middle" dominant-baseline="central" '
        'font-size="16" font-weight="bold" fill="currentColor">%s</text>' % (x, y, name)
        for name, x, y in (("N", 100, 52), ("E", 148, 100), ("S", 100, 148), ("W", 52, 100)))
    return (
        '<svg viewBox="0 0 200 200" width="220" height="220" style="display:block;margin:auto">'
        '<circle cx="100" cy="100" r="90" fill="none" stroke="currentColor" stroke-width="3"/>'
        + "".join(ticks) + labels +
        '<g transform="rotate(%.1f 100 100)">'
        '<polygon points="100,22 109,100 100,92 91,100" fill="%s"/>'
        '<polygon points="100,178 109,100 100,108 91,100" fill="currentColor" opacity="0.35"/>'
        '</g><circle cx="100" cy="100" r="5" fill="currentColor"/></svg>'
    ) % (heading, needle)


def point_ahead(lat, lon, heading_deg, meters):
    """(lat, lon) `meters` from (lat, lon) along `heading_deg`; fine for short distances."""
    rad = math.radians(heading_deg)
    dlat = meters * math.cos(rad) / 111_320
    dlon = meters * math.sin(rad) / (111_320 * max(math.cos(math.radians(lat)), 1e-6))
    return lat + dlat, lon + dlon


def show_battery(snap, cells):
    st.subheader("Battery")
    battery = snap["battery"]
    if battery is None:
        st.info("No battery reading yet (the Pico's status hasn't come through).")
        return
    mv, low, at = battery
    volts = mv / 1000
    percent = battery_percent(volts / cells)
    st.metric("Pack voltage", "%.2f V" % volts, help="%.2f V per cell, %d cells" % (volts / cells, cells))
    st.progress(percent / 100, text="~%d%% (estimate from resting voltage)" % percent)
    if low:
        st.error("The Pico reports LOW BATTERY.")
    bcu = (snap["latest"] or {}).get("bcu")
    if bcu is None:
        st.warning("Last known value, %s: the Pico's status isn't coming through right now." % ago(time.time() - at))


def show_compass(latest):
    st.subheader("Compass")
    compass = latest.get("compass")
    if compass is None or compass.get("heading") is None:
        st.info("No compass on the boat right now.")
        return
    heading = compass["heading"]
    fresh = bool(compass.get("fresh"))
    accuracy = compass.get("accuracy") or 0
    st.markdown(compass_svg(heading, fresh), unsafe_allow_html=True)
    st.metric("Heading (magnetic)", "%.0f° %s" % (heading, COMPASS_POINTS[round(heading / 22.5) % 16]))
    st.caption("Calibration: %s (%d/3)" % (ACCURACY_NAMES[min(max(accuracy, 0), 3)], accuracy))
    if not fresh:
        st.warning("Stale: no new heading from the compass.")


def show_gps(latest):
    gps = latest.get("gps") or {}
    st.subheader("GPS")
    if not gps.get("link_ok"):
        st.error("No data from the GNSS module.")
    elif gps.get("fix_valid"):
        quality = "good" if gps.get("fix_good") else "LOW QUALITY"
        st.success("Fix (%s): %d satellites, HDOP %s" % (
            quality, gps.get("sats") or 0, "-" if gps.get("hdop") is None else "%.1f" % gps["hdop"]))
    else:
        st.warning("Waiting for a fix.")
    if gps.get("motion_valid"):
        st.caption("Speed %.1f kn, course %.0f°" % (gps.get("sog_kn") or 0, gps.get("cog_deg") or 0))


def show_map(latest, track, follow):
    gps = latest.get("gps") or {}
    if gps.get("age_ms") is None:
        st.info("No position yet: the boat hasn't had a GPS fix since it started.")
        return
    lat, lon = gps["lat_e7"] / 1e7, gps["lon_e7"] / 1e7
    fix_valid = bool(gps.get("fix_valid"))
    if not fix_valid:
        st.caption("Showing the last known position (fix lost, %d s old)." % (gps["age_ms"] // 1000))

    layers = []
    if len(track) >= 2:
        layers.append(pdk.Layer(
            "PathLayer", [{"path": [[p[1], p[0]] for p in track]}],
            get_path="path", get_color=[30, 120, 220], width_min_pixels=3))
    compass = latest.get("compass") or {}
    if compass.get("heading") is not None and compass.get("fresh"):
        tip = point_ahead(lat, lon, compass["heading"], HEADING_LINE_M)
        layers.append(pdk.Layer(
            "LineLayer", [{"from": [lon, lat], "to": [tip[1], tip[0]]}],
            get_source_position="from", get_target_position="to",
            get_color=[214, 39, 40], get_width=4))
    layers.append(pdk.Layer(
        "ScatterplotLayer", [{"position": [lon, lat]}], get_position="position",
        get_fill_color=[40, 170, 60] if fix_valid else [150, 150, 150],
        get_line_color=[255, 255, 255], stroked=True, line_width_min_pixels=2,
        radius_min_pixels=8, radius_max_pixels=8))

    if follow or "view" not in st.session_state:
        st.session_state["view"] = (lat, lon)
    view_lat, view_lon = st.session_state["view"]
    st.pydeck_chart(pdk.Deck(
        layers=layers, map_provider="carto", map_style="light",
        initial_view_state=pdk.ViewState(latitude=view_lat, longitude=view_lon, zoom=17)))
    st.caption("%.7f, %.7f" % (lat, lon))


st.set_page_config(page_title="Audrey ground control", layout="wide")
st.title("Audrey ground control")

with st.sidebar:
    cells = st.number_input("Battery cells in series", min_value=1, max_value=6, value=2,
                            help="For the charge estimate (LiPo / Li-ion).")
    follow = st.toggle("Map follows the boat", value=True)

feed = get_feed()


@st.fragment(run_every=REFRESH_S)
def live():
    snap = feed.snapshot()
    latest = snap["latest"]
    age = None if snap["received_at"] is None else time.time() - snap["received_at"]

    status = "MQTT connected" if snap["connected"] else "MQTT disconnected"
    if snap["error"]:
        status += " (%s)" % snap["error"]
    if age is not None:
        status += " · last telemetry %s · %d messages" % (ago(age), snap["messages"])
    st.caption(status)

    if latest is None:
        st.info("Waiting for telemetry from the boat...")
        return
    if age > STALE_S:
        st.error("No telemetry for %s: everything below is the last known state." % ago(age))

    left, middle, right = st.columns([1, 1, 2])
    with left:
        show_battery(snap, cells)
        show_gps(latest)
    with middle:
        show_compass(latest)
    with right:
        show_map(latest, snap["track"], follow)


live()
