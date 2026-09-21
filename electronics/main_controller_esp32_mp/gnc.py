"""Navigation concept: GPS position -> bearing/distance -> heading error -> rudder/throttle.

The pipeline is the standard one and the structure is fine. What is still open, roughly in
priority order:

Heading source (biggest gap)
  - Heading from GPS movement (current_head_course = current_move_course) is unreliable.
    Consumer GPS is noisy by 2-3 m; at 1 Hz and low boat speed the position moves less than
    that over the averaging window, so the course is random, and it is undefined at standstill.
    Even when moving, course over ground is not heading (wind and current push the boat
    sideways), and averaging adds lag that makes a proportional-only controller weave.
  - The MPU6050 has NO magnetometer: accelerometer + gyro only. The gyro gives yaw rate and
    drifts; nothing gives absolute heading. Use a magnetometer (QMC5883L, LSM303, BNO055,
    BNO085, MPU9250), ideally fused with the gyro and GPS course (complementary filter).

GPS input
  - Take speed and course from the module's own RMC sentence (Doppler-based, much better
    than differencing positions). Read GGA/GSA too: check fix validity, satellite count and
    HDOP, and treat stale or invalid readings as "no data" (None), not as a position.
  - GPS speed noise is about 0.1-0.2 m/s, so the 0.15 m/s "still moving" threshold in
    _stop() is inside the noise.

Arrival and stopping
  - The 4 m arrival radius is inside the GPS error: the boat arrives, stops, drifts out and
    restarts, then hunts. Use hysteresis (stop under ~4 m, resume only beyond ~8 m) and a
    small state machine: NAVIGATING -> ARRIVED -> HOLDING.
  - Slow down in proportion to the distance near the destination instead of full throttle
    until the stop radius.
  - move_to_destination() is stateless; the waypoint list and the arrived flag need to live
    somewhere.

Control quality
  - Proportional-only rudder will oscillate on a slow-responding boat. Add damping (gyro yaw
    rate as the D term), a rudder rate limit and a small deadband.
  - For paths between waypoints, follow the line and correct cross-track error instead of
    always pointing at the destination; it handles current better.
  - The rudder gain comment and the value disagree: 0.01 per degree reaches full rudder at
    100 degrees, the comment says 90 (that would be ~0.0111).

Safety
  - If the GPS fix is lost or stale, command neutral from the ESP32 itself. The Pico's
    failsafe only notices missing I2C frames, and the ESP32 keeps re-sending the last command.
  - Keep in mind the ESP32 network thread can stall the I2C loop for 200+ ms (see the Pico's
    FAILSAFE_MS); measure send gaps before relying on the 200 ms failsafe.

Implementation
  - Write the math (haversine or flat-earth distance, initial bearing, angle wrap, rudder
    mapping) as pure functions, unit test it with plain Python on the desktop, and check the
    rudder sign and the oscillation against a tiny boat simulator before going on the water.
  - Clean up the typos: _get_cuurent_gps_coordinates, destiantion; the name
    max_rudder_max_throttle is confusing; "current" position should not come from
    _get_trailing_gps_coordinates(1).
"""


def _get_cuurent_gps_coordinates():
    lat = None
    lon = None
    # todo: read gps uart
    return lat, lon

def _get_trailing_gps_coordinates(n_readings):
    lat = None
    lon = None
    current_gps = _get_cuurent_gps_coordinates()
    # todo: store into an array of 3 last readings:

    # todo: read last N and average

    return lat, lon


def move_to_destination(gps_lat, gps_lon):
    trailing_gps_lat, trailing_gps_lon = _get_trailing_gps_coordinates(3)
    current_gps_lat, current_gps_lon = _get_trailing_gps_coordinates(1)

    current_move_course = 0
    # todo: identify the move course based on current and trailing GPS

    current_head_course = current_move_course # until we will plug mpu6050

    distance_to_destination = 0
    speed_m_s = 0
    # todo: calculate distance_to_destination based on destination and current GPS
    # todo: calculate speed_m_s based on trailing and current GPS
    if distance_to_destination < 4:
        _stop(current_head_course, current_move_course, speed_m_s)
        return

    target_course = 0
    # todo: calculate target_course based on destiantion and current gps

    _set_course(target_course, current_head_course)


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
    rudder_step_per_degree = 0.01 

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
    # todo: multiply by 100 and round inputs
    # todo: set the state that will be passed into the i2c
    pass

