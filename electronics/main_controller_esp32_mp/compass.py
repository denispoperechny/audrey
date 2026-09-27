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

    def update(self, now):
        """Reads the latest heading and accuracy; saves the calibration once it's stable."""
        _roll, _tilt, pan = self.bno.euler  # (roll, tilt, pan); "pan" is yaw/heading, -180..180
        self.heading = (HEADING_SIGN * pan + HEADING_OFFSET_DEG) % 360
        self.accuracy = self.bno.calibration_status  # 0..3

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

    def describe(self):
        """One-line status for the console."""
        line = "heading=%.1f deg | mag calib=%s (%d)" % (
            self.heading, REPORT_ACCURACY_STATUS[self.accuracy], self.accuracy)
        if self.saved_now:
            line += " | calibration saved to flash"
        return line
