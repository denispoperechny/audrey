"""Navigation: GPS position + compass heading + destination -> throttle and rudder.

A simple first version, for ground tests. Each control pass (move_to_destination):
  1. The offset from the boat to the destination, in meters east and north, with the
     equirectangular approximation from mission 2 (flat ground around the boat: fine for
     the distances a run covers).
  2. From it, the distance and the course to steer (bearing, degrees true).
  3. Closer than ARRIVAL_RADIUS_M: stop (_stop). Otherwise steer (_set_course): rudder in
     proportion to the difference between that course and the compass heading, throttle
     reduced as the rudder grows.
The heading is the compass's alone and the speed and course over ground are the GPS
module's own (from Doppler); nothing is fused, filtered or remembered between passes.

Still open, roughly in priority order:

Arrival and stopping
  - The 4 m arrival radius is inside the GPS error: the boat arrives, stops, drifts out and
    restarts, then hunts. Use hysteresis (stop under ~4 m, resume only beyond ~8 m) and a
    small state machine: NAVIGATING -> ARRIVED -> HOLDING.
  - Slow down in proportion to the distance near the destination instead of full throttle
    until the stop radius.
  - GPS speed noise is about 0.1-0.2 m/s, so the 0.15 m/s "still moving" threshold in
    _stop() is inside the noise, and the course over ground it compares with the heading
    is meaningless at that speed.

Control quality
  - Proportional-only rudder will oscillate on a slow-responding boat. Add damping (gyro yaw
    rate as the D term), a rudder rate limit and a small deadband.
  - For paths between waypoints, follow the line and correct cross-track error instead of
    always pointing at the destination; it handles current better.
  - Heading from the compass ignores what wind and current do to the boat's actual track;
    fusing it with the GPS course would correct that.

To check before going on the water
  - The rudder sign: positive is meant to turn right. Not verified against the servo.
  - MAGNETIC_DECLINATION_DEG is 0: set it for where the boat sails.
  - The math is plain Python: test_gnc.py runs on the desktop.
"""

import math

EARTH_RADIUS_M = 6371000  # mean Earth radius (spherical approximation), as in mission 2
M_PER_E7 = EARTH_RADIUS_M * math.pi / 180 / 1e7  # meters per 1e-7 degree of latitude
KN_TO_M_S = 0.514444

# The compass gives magnetic heading and a bearing between two positions is true: this is
# added to the heading to make them comparable (east positive). 0 = not set; the error it
# leaves is the local declination, a constant offset in the course steered.
MAGNETIC_DECLINATION_DEG = 0.0

ARRIVAL_RADIUS_M = 4


class NavInputs:
    """What the controller knows, filled in before every move_to_destination() call (20 Hz).

    The controller only calls when the heading and the GPS fix are fresh, so the heading
    and position here are current; the motion values have their own flag.

    Only the latest readings are here. The *_ms fields say when each was read: a reading is
    new when its *_ms changed since the last call (GPS: once a second, heading: 10 times),
    which is the moment to append it to whatever history the navigation wants to keep.
    They are time.ticks_ms() values, like `now`: compare them with time.ticks_diff().

    Positions come as integers of degrees * 1e7 and as floats. The floats are convenient
    but single precision on this board (about 1 m at these longitudes); take differences
    of the integers where that matters.
    """

    def __init__(self):
        self.now = 0  # time.ticks_ms() of this control pass

        self.dest_lat_e7 = 0  # where to go
        self.dest_lon_e7 = 0
        self.dest_lat = 0.0
        self.dest_lon = 0.0

        self.lat_e7 = 0  # last GPS fix
        self.lon_e7 = 0
        self.lat = 0.0
        self.lon = 0.0
        self.fix_ms = 0
        self.sats = 0  # satellites used in it
        self.hdop = None  # None = the module hasn't reported it yet

        self.motion_valid = False  # the two below are measured (by the module, from Doppler)
        self.sog_kn = 0.0  # speed over ground, knots
        self.cog_deg = 0.0  # course over ground, degrees true; meaningless at standstill
        self.motion_ms = 0

        self.heading = 0.0  # compass, degrees magnetic, 0..360 clockwise
        self.heading_ms = 0
        self.heading_accuracy = 0  # magnetometer calibration, 0 (unreliable) .. 3 (high)


# The latest decision, as (throttle %, rudder %), both -100..100. The controller reads it
# after each move_to_destination() call and sends it to the Pico.
controls = (0, 0)

# What the latest decision was based on, for telemetry.
state = "arrived"  # "navigating" or "arrived"
distance_m = 0.0  # to the destination
target_course_deg = 0.0  # course to steer to get there, degrees true
course_error_deg = 0.0  # target course - heading, -180..180; positive = turn right


def offset_m(lat_e7, lon_e7, to_lat_e7, to_lon_e7):
    """(east, north) in meters from one position to another, both in degrees * 1e7.

    Equirectangular approximation, as in mission 2: a degree of latitude is the same
    length everywhere, a degree of longitude shrinks with the cosine of the latitude.
    The differences are taken on the integers, so nothing is lost to float rounding."""
    x_m = (to_lon_e7 - lon_e7) * M_PER_E7 * math.cos(math.radians(lat_e7 / 1e7))
    y_m = (to_lat_e7 - lat_e7) * M_PER_E7
    return x_m, y_m


def course_deg(x_m, y_m):
    """Compass course along an (east, north) offset: 0 = north, 90 = east, 0..360."""
    return math.degrees(math.atan2(x_m, y_m)) % 360


def move_to_destination(nav):
    # nav: NavInputs
    global state, distance_m, target_course_deg, course_error_deg

    x_m, y_m = offset_m(nav.lat_e7, nav.lon_e7, nav.dest_lat_e7, nav.dest_lon_e7)
    distance_m = math.sqrt(x_m * x_m + y_m * y_m)
    target_course_deg = course_deg(x_m, y_m)

    current_head_course = (nav.heading + MAGNETIC_DECLINATION_DEG) % 360
    course_error_deg = _get_course_correction(target_course_deg, current_head_course)

    if distance_m < ARRIVAL_RADIUS_M:
        state = "arrived"
        speed_m_s = nav.sog_kn * KN_TO_M_S if nav.motion_valid else 0.0
        _stop(current_head_course, nav.cog_deg, speed_m_s)
        return

    state = "navigating"
    _set_course(target_course_deg, current_head_course)


def _get_course_correction(target_course, given_course):
    course_correction = target_course - given_course
    if course_correction > 180:
        course_correction = course_correction - 360
    if course_correction < -180:
        course_correction = course_correction + 360
    return course_correction


def _set_course(dest_course, curr_course):
    max_throttle = 0.4
    max_rudder_max_throttle = 0.2

    # for stability, should be lower than absolute. Max rudder is about 45 degrees (at value 1.0), which should be applied at 90 degrees course deviation.
    rudder_step_per_degree = 1 / 90

    course_correction = _get_course_correction(dest_course, curr_course)

    # in marine, positive rudder value corresponds to turning right
    # positive course deviation should make correction to the right - positive rudder value
    calculated_rudder = course_correction * rudder_step_per_degree

    if calculated_rudder > 1.0:
        calculated_rudder = 1.0
    if calculated_rudder < -1.0:
        calculated_rudder = -1.0

    calculated_throttle = max_throttle - max_rudder_max_throttle * abs(calculated_rudder)

    _set_controls(calculated_throttle, calculated_rudder)


def _stop(head_course, drift_course, speed_m_s):

    # if boat still moves forward
    if speed_m_s > 0.15:
        if abs(_get_course_correction(head_course, drift_course)) < 20:
            _set_controls(-0.3, 0.0)
            return

    _set_controls(0.0, 0.0)


def _set_controls(throttle, rudder):
    global controls
    controls = (round(throttle * 100), round(rudder * 100))
