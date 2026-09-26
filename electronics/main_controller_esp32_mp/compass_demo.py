"""BNO08x compass test.

The bus scan found the sensor at 0x4B, which is a BNO08x-family address (BNO080/085/086). 
This uses the "bno08x" driver vendored into this
project (bno08x.py -- MIT-licensed, github.com/dobodu/BOSCH-BNO085-I2C-micropython-library,
itself adapted from Adafruit's CircuitPython BNO08x library).

Reads the fused, magnetometer-corrected heading 4 times a second and prints it to the
console (UART0/USB), along with the magnetometer calibration accuracy.

Wiring (BNO08x breakout -> ESP32), same bus as before:
  VIN -> 3.3V
  GND -> GND
  SDA -> GPIO21
  SCL -> GPIO22
No reset or interrupt pin wired -- the driver polls over I2C instead, which is slower to
notice a fresh reading but doesn't need extra wiring.

The Rotation Vector report fuses accel + gyro + magnetometer into an absolute heading, as
opposed to the driver's default "Game Rotation Vector" (accel + gyro only), which has no
absolute reference and drifts. set_quaternion_euler_vector() below switches the euler/
quaternion properties to read the Rotation Vector instead.

NOTE ON HEADING CONVENTION: the driver derives heading from a standard yaw formula; whether
0 degrees corresponds to magnetic north, and whether it increases clockwise (as a compass
does) or counterclockwise (as most math yaw does), depends on the chip's physical mounting
orientation and hasn't been verified against a real compass yet. Check it once running:
point the board at a known heading and confirm the printed number matches and increases as
you turn clockwise. Adjust HEADING_SIGN / HEADING_OFFSET_DEG below if it doesn't.
"""

from machine import Pin, I2C
import time

from bno08x import BNO08X, BNO_REPORT_ROTATION_VECTOR, REPORT_ACCURACY_STATUS

I2C_SDA_PIN = 21
I2C_SCL_PIN = 22
REPORT_INTERVAL_MS = 250  # 4/s
ROTATION_VECTOR_HZ = 10  # sensor's internal update rate; comfortably above the 4/s print rate

# Adjust these once you've checked the heading against a real compass (see note above).
HEADING_SIGN = 1  # set to -1 if heading increases counterclockwise instead of clockwise
HEADING_OFFSET_DEG = 0.0  # add a fixed offset if 0 degrees isn't pointing at magnetic north

# Once the magnetometer calibration accuracy has been "High" (3) continuously for this long,
# save the calibration to the chip's flash so it doesn't have to be redone after a reboot.
# Set to None to disable auto-save.
AUTO_SAVE_AFTER_STABLE_MS = 5000

i2c = I2C(0, sda=Pin(I2C_SDA_PIN), scl=Pin(I2C_SCL_PIN), freq=100_000, timeout=200_000)
bno = BNO08X(i2c, debug=False)
bno.enable_feature(BNO_REPORT_ROTATION_VECTOR, ROTATION_VECTOR_HZ)
bno.set_quaternion_euler_vector(BNO_REPORT_ROTATION_VECTOR)
bno.calibration()  # turn on continuous accel/gyro/mag self-calibration
print("BNO08x ready")

calibration_saved = False
high_accuracy_since = None
last_report = time.ticks_ms()

while True:
    now = time.ticks_ms()
    if time.ticks_diff(now, last_report) >= REPORT_INTERVAL_MS:
        last_report = time.ticks_add(last_report, REPORT_INTERVAL_MS)

        _roll, _tilt, pan = bno.euler  # (roll, tilt, pan); "pan" is yaw/heading, -180..180
        heading = (HEADING_SIGN * pan + HEADING_OFFSET_DEG) % 360

        accuracy = bno.calibration_status  # 0..3
        line = "heading=%.1f deg | mag calib=%s (%d)" % (heading, REPORT_ACCURACY_STATUS[accuracy], accuracy)

        if AUTO_SAVE_AFTER_STABLE_MS is not None and not calibration_saved:
            if accuracy == 3:
                if high_accuracy_since is None:
                    high_accuracy_since = now
                elif time.ticks_diff(now, high_accuracy_since) >= AUTO_SAVE_AFTER_STABLE_MS:
                    bno.calibration_save()
                    calibration_saved = True
                    line += " | calibration saved to flash"
            else:
                high_accuracy_since = None

        print(line)

    time.sleep_ms(20)
