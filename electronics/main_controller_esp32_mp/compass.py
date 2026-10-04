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

The Magnetometer report is enabled too, only for its accuracy field: the driver takes the
magnetometer calibration accuracy (0..3) from those reports and nowhere else. Without them
it stays 0 forever, and the calibration is never auto-saved -- so every boot starts
uncalibrated, with a heading that's off until the chip has re-learned the field.

CALIBRATING: in the boat, in its final position, with battery and motors installed (their
magnetic fields are part of what gets corrected). Move it slowly through every orientation
-- figure-8s, rolls, pitches, a couple of slow flat turns -- until "mag calib" reaches 3
(High). After 3 for AUTO_SAVE_AFTER_STABLE_MS it's saved to the chip's flash ("compass:
calibration saved to flash" on the console, "saved" in the status line and telemetry), and
later boots start from it. Redo it after moving the sensor or anything magnetic near it.

WHERE CALIBRATION LIVES: in the BNO08x's own flash. After any reset the chip reloads the
last saved calibration by itself; only what it learned since the last save is lost. Saves
happen in two ways: the auto-save above, and the chip's own periodic save, which is turned
on at init (SH-2 "Configure Periodic DCD Save"). Initializing resets the chip (the driver's
soft reset), so the controller avoids it: when headings stop, it first tries
restart_reports(), which re-enables the reports without a reset.

NOTE ON HEADING CONVENTION: the driver derives heading from a standard yaw formula; whether
0 degrees corresponds to magnetic north, and whether it increases clockwise (as a compass
does) or counterclockwise (as most math yaw does), depends on the chip's physical mounting
orientation and hasn't been verified against a real compass yet. Check it once running:
point the board at a known heading and confirm the printed number matches and increases as
you turn clockwise. Adjust HEADING_SIGN / HEADING_OFFSET_DEG below if it doesn't.
"""

import time

from bno08x import (BNO08X, BNO_REPORT_MAGNETOMETER, BNO_REPORT_ROTATION_VECTOR, ME_SAVE_DCD_PERIODIC_CDE,
                    REPORT_ACCURACY_STATUS)

ROTATION_VECTOR_HZ = 10  # sensor's internal update rate
MAGNETOMETER_HZ = 2  # only read for its calibration accuracy, which changes slowly

# Adjust these once you've checked the heading against a real compass (see note above).
HEADING_SIGN = 1  # set to -1 if heading increases counterclockwise instead of clockwise
# Mounting: the chip reads 0 when the board's Y+ arrow points at magnetic north (checked
# 2026-10-03). The board is mounted with Y+ toward the stern, so 180 makes 0 = bow north.
HEADING_OFFSET_DEG = 180.0

# Once the magnetometer calibration accuracy has been "High" (3) continuously for this long,
# save the calibration to the chip's flash so it doesn't have to be redone after a reboot.
# Set to None to disable auto-save.
AUTO_SAVE_AFTER_STABLE_MS = 5000
SAVE_RETRY_MS = 30000  # a failed save blocks for up to 2 s (driver timeout); don't retry sooner

ACCURACY_HIGH = 3


class Compass:
    """Reads the fused heading and keeps the magnetometer calibration saved."""

    def __init__(self, i2c, rate_hz=ROTATION_VECTOR_HZ):
        self.bno = BNO08X(i2c, debug=False)
        self.rate_hz = rate_hz
        self._enable_reports()
        self.bno.set_quaternion_euler_vector(BNO_REPORT_ROTATION_VECTOR)
        # Let the chip also save its calibration to flash periodically by itself. Parameter
        # P0 = 0 enables it (the driver's ME_SAVE_DCD_PERIODIC_*_SUBCDE constants are both
        # 0, so they're not used). The chip doesn't confirm this command.
        self.bno._send_ME_cde(ME_SAVE_DCD_PERIODIC_CDE, [0, 0, 0, 0, 0, 0, 0, 0, 0])

        self.heading = 0.0  # degrees, 0..360
        self.accuracy = 0  # magnetometer calibration accuracy, 0..3
        self.calibration_saved = False  # auto-saved since this init (the chip may also hold
        # an older save, and saves periodically by itself)
        self.saves = 0
        self.save_failures = 0
        self.last_save_attempt = None
        self.high_accuracy_since = None
        self.report_restarts = 0

        # The driver's euler property keeps returning the last reading even if the sensor
        # stops sending, so freshness is judged by whether a new report object arrived:
        # the driver stores every report as a new tuple in its _readings dict.
        self.last_report = self.bno._readings.get(BNO_REPORT_ROTATION_VECTOR)
        self.stamp = 0
        self.seen = False

    def _enable_reports(self):
        self.bno.enable_feature(BNO_REPORT_ROTATION_VECTOR, self.rate_hz)
        self.bno.enable_feature(BNO_REPORT_MAGNETOMETER, MAGNETOMETER_HZ)
        self.bno.calibration()  # turn on continuous accel/gyro/mag self-calibration

    def restart_reports(self):
        """Re-enables the reports without resetting the chip, so it keeps its calibration.
        Takes milliseconds; raises on I2C errors."""
        self.report_restarts += 1
        self._enable_reports()

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

        # Kept up to date from the Magnetometer reports read along with the heading. (The
        # driver's calibration_status property returns the same value, after sending a
        # pointless request over I2C.)
        self.accuracy = self.bno._magnetometer_accuracy  # 0..3

        if AUTO_SAVE_AFTER_STABLE_MS is None or self.calibration_saved:
            return
        if self.accuracy != ACCURACY_HIGH:
            self.high_accuracy_since = None
        elif self.high_accuracy_since is None:
            self.high_accuracy_since = now
        elif time.ticks_diff(now, self.high_accuracy_since) >= AUTO_SAVE_AFTER_STABLE_MS:
            if self.last_save_attempt is not None and time.ticks_diff(now, self.last_save_attempt) < SAVE_RETRY_MS:
                return
            self.last_save_attempt = now
            try:
                self.bno.calibration_save()  # waits for the chip to confirm
            except RuntimeError as e:
                self.save_failures += 1
                print("compass: calibration save failed (#%d): %s" % (self.save_failures, e))
                return
            self.calibration_saved = True
            self.saves += 1
            print("compass: calibration saved to flash")

    def fresh(self, now, max_age_ms):
        """A new heading arrived within max_age_ms."""
        return self.seen and time.ticks_diff(now, self.stamp) <= max_age_ms

    def describe(self):
        """One-line status for the console."""
        return "heading=%.1f deg | mag calib=%s (%d), %s" % (
            self.heading, REPORT_ACCURACY_STATUS[self.accuracy], self.accuracy,
            "saved" if self.calibration_saved else "not saved since init")
