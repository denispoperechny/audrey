"""Positions, directions and distances on the water: the one place mission control works them out
(the boat's MCP tools, the captain's state and its calculation tools all use these).

Azimuths are degrees clockwise from true north, 0..360; relative bearings are degrees from the
bow, -180..180, + to the right (starboard). Spherical earth: well within a metre over the few
hundred metres the boat goes.
"""

import math

EARTH_RADIUS_M = 6371000


def wrap180(a):
    return (a + 180) % 360 - 180


def destination_point(lat, lon, azimuth_deg, distance_m):
    """The point distance_m from (lat, lon) along azimuth_deg."""
    phi1, lam1, theta = math.radians(lat), math.radians(lon), math.radians(azimuth_deg)
    delta = distance_m / EARTH_RADIUS_M
    phi2 = math.asin(math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta))
    lam2 = lam1 + math.atan2(math.sin(theta) * math.sin(delta) * math.cos(phi1),
                             math.cos(delta) - math.sin(phi1) * math.sin(phi2))
    return math.degrees(phi2), (math.degrees(lam2) + 540) % 360 - 180


def course_between(lat1, lon1, lat2, lon2):
    """(azimuth from point 1 to point 2, distance in metres)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlam = phi2 - phi1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    distance = 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))
    azimuth = math.degrees(math.atan2(math.sin(dlam) * math.cos(phi2),
                                      math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)))
    return azimuth % 360, distance


def relative_from_azimuth(heading_deg, azimuth_deg):
    """Where an azimuth is relative to the bow: -180..180, + right."""
    return wrap180(azimuth_deg - heading_deg)


def azimuth_from_relative(heading_deg, relative_deg):
    """The azimuth of a direction given relative to the bow (+ right)."""
    return (heading_deg + relative_deg) % 360


def legs(lat1, lon1, lat2, lon2, max_leg_m):
    """The way from point 1 to point 2 in equal legs of at most max_leg_m, each as (azimuth from
    the leg's start, distance, end lat, end lon); the last leg ends exactly at point 2."""
    _, total = course_between(lat1, lon1, lat2, lon2)
    count = max(1, math.ceil(total / max_leg_m - 1e-9))
    out, start = [], (lat1, lon1)
    for i in range(1, count + 1):
        f = i / count
        # Along the straight line: the point at fraction f of the way (fine at these distances).
        end = (lat2, lon2) if i == count else (lat1 + (lat2 - lat1) * f, lon1 + (lon2 - lon1) * f)
        azimuth, distance = course_between(*start, *end)
        out.append((azimuth, distance, end[0], end[1]))
        start = end
    return out
