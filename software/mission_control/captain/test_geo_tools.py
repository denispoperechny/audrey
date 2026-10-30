"""python -m pytest -q captain      (from software/mission_control)"""

import pytest

import geo
from captain.geo_tools import (TOOLS, azimuth_from_relative, course_between, gps_from_course, relative_from_azimuth,
                               split_into_legs)

LAT, LON = 30.2443012, -95.4902590


def test_course_and_point_are_inverses():
    for azimuth in (0, 45, 90, 135, 180, 270, 359):
        p = gps_from_course.invoke({"lat": LAT, "lon": LON, "azimuth": azimuth, "distance": 73})
        back = course_between.invoke({"from_lat": LAT, "from_lon": LON, "to_lat": p["lat"], "to_lon": p["lon"]})
        assert abs(back["distance_m"] - 73) < 0.1 and abs(geo.wrap180(back["azimuth_deg"] - azimuth)) < 0.2, azimuth


def test_known_directions():
    north = gps_from_course.invoke({"lat": LAT, "lon": LON, "azimuth": 0, "distance": 100})
    assert north["lat"] > LAT and north["lon"] == pytest.approx(LON, abs=1e-7)
    assert abs(north["lat"] - LAT - 100 / 111195) < 1e-6  # 100 m of latitude
    east = course_between.invoke({"from_lat": LAT, "from_lon": LON, "to_lat": LAT, "to_lon": LON + 0.001})
    assert abs(east["azimuth_deg"] - 90) < 0.1 and abs(east["distance_m"] - 96.1) < 0.5  # longitude shrinks: cos(30°)


def test_relative_and_azimuth_across_north():
    assert relative_from_azimuth.invoke({"heading": 350, "azimuth": 10})["relative_deg"] == 20  # 20° right, not -340
    assert relative_from_azimuth.invoke({"heading": 10, "azimuth": 350})["relative_deg"] == -20
    assert azimuth_from_relative.invoke({"heading": 350, "relative": 25})["azimuth_deg"] == 15
    assert azimuth_from_relative.invoke({"heading": 90, "relative": -25})["azimuth_deg"] == 65


def test_split_into_legs():
    far = gps_from_course.invoke({"lat": LAT, "lon": LON, "azimuth": 120, "distance": 130})
    plan = split_into_legs.invoke({"from_lat": LAT, "from_lon": LON, "to_lat": far["lat"], "to_lon": far["lon"]})
    assert len(plan["legs"]) == 3 and abs(plan["total_m"] - 130) < 0.5
    assert all(abs(leg["distance_m"] - 130 / 3) < 0.5 and abs(leg["azimuth_deg"] - 120) < 0.5 for leg in plan["legs"])
    assert plan["legs"][-1]["end"] == far  # the last leg ends exactly at the target
    short = split_into_legs.invoke({"from_lat": LAT, "from_lon": LON, "to_lat": far["lat"], "to_lon": far["lon"], "max_leg_m": 200})
    assert len(short["legs"]) == 1


def test_tools_are_described_for_the_model():
    for t in TOOLS:
        assert t.description and t.args, t.name
