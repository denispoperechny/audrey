"""Calculation tools for the captain: converting exactly between positions and
direction-and-distance, and between azimuths and directions relative to the bow, so the LLM
doesn't do the arithmetic itself (geo.py does it, as for the boat's own tools).

Azimuths: degrees clockwise from true north. Relative bearings: degrees from the bow, + right
(starboard), - left (port).
"""

from langchain_core.tools import tool

import geo


def _point(lat, lon):
    return {"lat": round(lat, 7), "lon": round(lon, 7)}


@tool
def gps_from_course(lat: float, lon: float, azimuth: float, distance: float) -> dict:
    """The position `distance` metres from (lat, lon) in the direction `azimuth` (degrees clockwise
    from true north)."""
    return _point(*geo.destination_point(lat, lon, azimuth, distance))


@tool
def course_between(from_lat: float, from_lon: float, to_lat: float, to_lon: float) -> dict:
    """The azimuth (degrees clockwise from true north) and the distance (metres) from one position
    to another."""
    azimuth, distance = geo.course_between(from_lat, from_lon, to_lat, to_lon)
    return {"azimuth_deg": round(azimuth, 1), "distance_m": round(distance, 1)}


@tool
def relative_from_azimuth(heading: float, azimuth: float) -> dict:
    """Where the direction `azimuth` is relative to the bow when the boat's heading is `heading`:
    degrees, + to the right (starboard), - to the left (port), -180..180."""
    return {"relative_deg": round(geo.relative_from_azimuth(heading, azimuth), 1)}


@tool
def azimuth_from_relative(heading: float, relative: float) -> dict:
    """The azimuth (degrees clockwise from true north) of a direction given relative to the bow
    (`relative` degrees, + right, - left) when the boat's heading is `heading`. E.g. a camera
    answer "+25° (right of the bow)" taken at heading 90 is azimuth 115."""
    return {"azimuth_deg": round(geo.azimuth_from_relative(heading, relative), 1)}


@tool
def split_into_legs(from_lat: float, from_lon: float, to_lat: float, to_lon: float, max_leg_m: float = 50) -> dict:
    """The straight way from one position to another in equal legs of at most `max_leg_m` metres:
    each leg's azimuth, distance and end position (the last one ends exactly at the target), and
    the total distance. Only the geometry: it knows nothing of shores or obstacles on the way."""
    if max_leg_m < 5:
        raise ValueError("max_leg_m has to be at least 5")
    legs = geo.legs(from_lat, from_lon, to_lat, to_lon, max_leg_m)
    return {"total_m": round(sum(d for _, d, _, _ in legs), 1),
            "legs": [{"azimuth_deg": round(a, 1), "distance_m": round(d, 1), "end": _point(la, lo)} for a, d, la, lo in legs]}


TOOLS = [gps_from_course, course_between, relative_from_azimuth, azimuth_from_relative, split_into_legs]
