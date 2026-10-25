"""Heading from the BNO08x (BNO080/085/086) over a UART (UART-SHTP, 3 Mbaud).

This uses the "bno08x" driver vendored into this project (bno08x.py -- MIT-licensed,
github.com/dobodu/BOSCH-BNO085-I2C-micropython-library, itself adapted from Adafruit's
CircuitPython BNO08x library), with its I2C transport replaced by a UART one: the chip's
I2C doesn't work reliably with the ESP32-S3 (see controller.py). The I2C original is kept
as archive_bno08x.py.

Wiring (BNO08x breakout -> ESP32; the UART pins are in controller.BOARD_PINS):
  VIN -> 3.3V
  GND -> GND
  PS1 -> 3.3V, PS0 -> GND (selects UART-SHTP; read by the chip at power-on / reset)
  SDA -> the UART's RX pin (in this mode it's the chip's TX)
  SCL -> the UART's TX pin (the chip's RX)
No reset or interrupt pin wired -- the driver reads whatever the UART has received since
the last update, and resets the chip with a command.

The Rotation Vector report fuses accel + gyro + magnetometer into an absolute heading, as
opposed to the driver's default "Game Rotation Vector" (accel + gyro only), which has no
absolute reference and drifts. set_quaternion_euler_vector() switches the euler/quaternion
properties to read the Rotation Vector instead. The Game Rotation Vector is enabled too and
read as gyro_yaw: unaffected by magnetic fields, it is the main input of virtual_compass.py,
which turns it, this heading and the GPS track into the heading the navigation uses.

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

NOTE ON HEADING CONVENTION: the driver derives heading from a standard yaw formula, which
increases counterclockwise (the math convention) and has its zero wherever the chip's axes
put it. HEADING_SIGN and HEADING_OFFSET_DEG below turn it into a compass heading: 0 = bow
at magnetic north, increasing clockwise. Both were checked on the boat; recheck them after
remounting the sensor: bow north should read 0 (or 360), and a quarter turn to the right 90.
"""

import math
import time

from machine import UART

from bno08x import (BNO08X, BNO_REPORT_GAME_ROTATION_VECTOR, BNO_REPORT_MAGNETOMETER, BNO_REPORT_ROTATION_VECTOR,
                    ME_CALIBRATION_CDE, ME_CALIBRATION_CONFIG_SUBCDE, ME_SAVE_DCD_PERIODIC_CDE,
                    REPORT_ACCURACY_STATUS, UART_BAUDRATE)

ROTATION_VECTOR_HZ = 10  # sensor's internal update rate
MAGNETOMETER_HZ = 2  # only read for its calibration accuracy, which changes slowly

# The chip's yaw increases counterclockwise: -1 makes the heading increase clockwise.
# (Checked 2026-10-11: with +1, bow north read 360 and a quarter turn left read 90.)
HEADING_SIGN = -1
# Mounting: the chip reads 0 when the board's Y+ arrow points at magnetic north (checked
# 2026-10-03). The board is mounted with Y+ toward the stern, so 180 makes 0 = bow north.
HEADING_OFFSET_DEG = 180.0

# Once the magnetometer calibration accuracy has been "High" (3) continuously for this long,
# save the calibration to the chip's flash so it doesn't have to be redone after a reboot.
# Set to None to disable auto-save.
AUTO_SAVE_AFTER_STABLE_MS = 5000
SAVE_RETRY_MS = 30000  # a failed save blocks for up to 2 s (driver timeout); don't retry sooner

ACCURACY_HIGH = 3

UART_RX_BUFFER = 2048  # the sensor sends ~300 bytes/s; this rides out a loop stalled for seconds


class Compass:
    """Reads the fused heading and keeps the magnetometer calibration saved."""

    def __init__(self, uart_id, rx_pin, tx_pin, rate_hz=ROTATION_VECTOR_HZ):
        # timeout=0: reads return what has arrived and never block
        uart = UART(uart_id, baudrate=UART_BAUDRATE, rx=rx_pin, tx=tx_pin, rxbuf=UART_RX_BUFFER, timeout=0)
        self.bno = BNO08X(uart, debug=False)
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

        # The gyro yaw: the game rotation vector, gyro and accelerometer only (no
        # magnetometer), so no magnetic field disturbs it, but its zero is wherever the chip
        # started and it drifts slowly. virtual_compass.py turns it into the heading.
        self.gyro_yaw = 0.0  # degrees, 0..360, increasing clockwise like heading
        self.last_gyro_report = self.bno._readings.get(BNO_REPORT_GAME_ROTATION_VECTOR)
        self.gyro_stamp = 0
        self.gyro_seen = False

    def _enable_reports(self):
        self.bno.enable_feature(BNO_REPORT_ROTATION_VECTOR, self.rate_hz)
        self.bno.enable_feature(BNO_REPORT_GAME_ROTATION_VECTOR, self.rate_hz)
        self.bno.enable_feature(BNO_REPORT_MAGNETOMETER, MAGNETOMETER_HZ)
        # Continuous self-calibration for the accelerometer and magnetometer, but not the gyro
        # (the driver's calibration() turns on all three). With it on, the gyro yaw drifted on
        # the water by -1 to +1.5 deg/s, wandering over minutes and back to 0 whenever the boat
        # was truly still: the chip appears to re-learn the gyro's bias wrongly when it is
        # never quite still (mission 5). Off, it keeps the gyro calibration it has saved.
        self.bno._send_ME_cde(ME_CALIBRATION_CDE, [1, 0, 1, ME_CALIBRATION_CONFIG_SUBCDE, 0, 0, 0, 0, 0])

    def restart_reports(self):
        """Re-enables the reports without resetting the chip, so it keeps its calibration.
        Takes milliseconds; raises if the chip doesn't confirm."""
        self.report_restarts += 1
        self._enable_reports()

    def update(self, now):
        """Reads the latest heading and accuracy; saves the calibration once it's stable.
        Driver errors propagate to the caller."""
        _roll, _tilt, pan = self.bno.euler  # (roll, tilt, pan); "pan" is yaw/heading, -180..180
        report = self.bno._readings.get(BNO_REPORT_ROTATION_VECTOR)
        if report is not self.last_report:
            self.last_report = report
            self.heading = (HEADING_SIGN * pan + HEADING_OFFSET_DEG) % 360
            self.stamp = now
            self.seen = True
        game = self.bno._readings.get(BNO_REPORT_GAME_ROTATION_VECTOR)
        if game is not self.last_gyro_report:
            self.last_gyro_report = game
            i, j, k, real = game[0], game[1], game[2], game[3]
            yaw = math.degrees(math.atan2(2.0 * (real * k + i * j), 1.0 - 2.0 * (j * j + k * k)))
            self.gyro_yaw = (HEADING_SIGN * yaw) % 360
            self.gyro_stamp = now
            self.gyro_seen = True

        # Kept up to date from the Magnetometer reports read along with the heading. (The
        # driver's calibration_status property returns the same value, after sending a
        # pointless request.)
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

    def gyro_fresh(self, now, max_age_ms):
        """A new gyro yaw arrived within max_age_ms."""
        return self.gyro_seen and time.ticks_diff(now, self.gyro_stamp) <= max_age_ms

    def describe(self):
        """One-line status for the console."""
        return "heading=%.1f deg | mag calib=%s (%d), %s" % (
            self.heading, REPORT_ACCURACY_STATUS[self.accuracy], self.accuracy,
            "saved" if self.calibration_saved else "not saved since init")
