"""Heading from the BNO08x (BNO080/085/086) over I2C.

The sensor sits at 0x4B, a BNO08x-family address. This uses the "bno08x" driver vendored
into this project (bno08x.py -- MIT-licensed, github.com/dobodu/BOSCH-BNO085-I2C-micropython-library,
itself adapted from Adafruit's CircuitPython BNO08x library).

Wiring (BNO08x breakout -> ESP32):
  VIN -> 3.3V
  GND -> GND
  SDA -> GPIO21
  SCL -> GPIO22
No reset or interrupt pin wired -- the driver polls over I2C instead, which is slower to
notice a fresh reading but doesn't need extra wiring.

The Rotation Vector report fuses accel + gyro + magnetometer into an absolute heading, as
opposed to the driver's default "Game Rotation Vector" (accel + gyro only), which has no
absolute reference and drifts. set_quaternion_euler_vector() switches the euler/quaternion
properties to read the Rotation Vector instead.

NOTE ON HEADING CONVENTION: the driver derives heading from a standard yaw formula; whether
0 degrees corresponds to magnetic north, and whether it increases clockwise (as a compass
does) or counterclockwise (as most math yaw does), depends on the chip's physical mounting
orientation and hasn't been verified against a real compass yet. Check it once running:
point the board at a known heading and confirm the printed number matches and increases as
you turn clockwise. Adjust HEADING_SIGN / HEADING_OFFSET_DEG below if it doesn't.
"""

import time

from bno08x import BNO08X, BNO_REPORT_ROTATION_VECTOR, REPORT_ACCURACY_STATUS

ROTATION_VECTOR_HZ = 10  # sensor's internal update rate

# Adjust these once you've checked the heading against a real compass (see note above).
HEADING_SIGN = 1  # set to -1 if heading increases counterclockwise instead of clockwise
HEADING_OFFSET_DEG = 0.0  # add a fixed offset if 0 degrees isn't pointing at magnetic north

# Once the magnetometer calibration accuracy has been "High" (3) continuously for this long,
# save the calibration to the chip's flash so it doesn't have to be redone after a reboot.
# Set to None to disable auto-save.
AUTO_SAVE_AFTER_STABLE_MS = 5000

ACCURACY_HIGH = 3

# calibration_status sends a request to the sensor on every call; the accuracy barely
# changes, so ask for it less often than the heading is read.
ACCURACY_INTERVAL_MS = 1000


class Compass:
    """Reads the fused heading and keeps the magnetometer calibration saved."""

    def __init__(self, i2c, rate_hz=ROTATION_VECTOR_HZ):
        self.bno = BNO08X(i2c, debug=False)
        self.bno.enable_feature(BNO_REPORT_ROTATION_VECTOR, rate_hz)
        self.bno.set_quaternion_euler_vector(BNO_REPORT_ROTATION_VECTOR)
        self.bno.calibration()  # turn on continuous accel/gyro/mag self-calibration

        self.heading = 0.0  # degrees, 0..360
        self.accuracy = 0  # magnetometer calibration accuracy, 0..3
        self.calibration_saved = False
        self.saved_now = False  # True only on the update() that saved the calibration
        self.high_accuracy_since = None

        # The driver's euler property keeps returning the last reading even if the sensor
        # stops sending, so freshness is judged by whether a new report object arrived:
        # the driver stores every report as a new tuple in its _readings dict.
        self.last_report = self.bno._readings.get(BNO_REPORT_ROTATION_VECTOR)
        self.stamp = 0
        self.seen = False
        self.accuracy_stamp = None

    def update(self, now):
        """Reads the latest heading and accuracy; saves the calibration once it's stable.
        I2C or driver errors propagate to the caller."""
        _roll, _tilt, pan = self.bno.euler  # (roll, tilt, pan); "pan" is yaw/heading, -180..180
        report = self.bno._readings.get(BNO_REPORT_ROTATION_VECTOR)
        if report is not self.last_report:
            self.last_report = report
            self.heading = (HEADING_SIGN * pan + HEADING_OFFSET_DEG) % 360
            self.stamp = now
            self.seen = True

        if self.accuracy_stamp is None or time.ticks_diff(now, self.accuracy_stamp) >= ACCURACY_INTERVAL_MS:
            self.accuracy = self.bno.calibration_status  # 0..3
            self.accuracy_stamp = now

        self.saved_now = False
        if AUTO_SAVE_AFTER_STABLE_MS is None or self.calibration_saved:
            return
        if self.accuracy != ACCURACY_HIGH:
            self.high_accuracy_since = None
        elif self.high_accuracy_since is None:
            self.high_accuracy_since = now
        elif time.ticks_diff(now, self.high_accuracy_since) >= AUTO_SAVE_AFTER_STABLE_MS:
            self.bno.calibration_save()
            self.calibration_saved = True
            self.saved_now = True

    def fresh(self, now, max_age_ms):
        """A new heading arrived within max_age_ms."""
        return self.seen and time.ticks_diff(now, self.stamp) <= max_age_ms

    def describe(self):
        """One-line status for the console."""
        line = "heading=%.1f deg | mag calib=%s (%d)" % (
            self.heading, REPORT_ACCURACY_STATUS[self.accuracy], self.accuracy)
        if self.saved_now:
            line += " | calibration saved to flash"
        return line
