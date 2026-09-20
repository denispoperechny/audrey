from machine import Pin, I2C
import math
import struct
import time

I2C_TARGET_ADDRESS = 0x31
I2C_SDA_PIN = 21
I2C_SCL_PIN = 22
SEND_INTERVAL_MS = 50  # 20 Hz; the Pico treats a command older than 200 ms as lost
REPORT_INTERVAL_MS = 1000
WAVE_PERIOD_MS = 10000  # one full sin/cos cycle
WAVE_AMPLITUDE = 100
LED_PIN = 2  # most ESP32 WROOM dev boards route the onboard LED to GPIO2

# Pico register map (register-addressed I2C target, little-endian).
REG_CMD = 0x00
CMD_FORMAT = "<bbBBB"  # throttle, rudder, flags, seq, checksum
REG_STATUS = 0x10
STATUS_LEN = 16
# heartbeat, flags, battery mV, rc mode/thr/rud us, out thr/rud us, overruns, cmd errors
STATUS_FORMAT = "<BBHHHHHHBB"

FLAG_ESP32_MODE = 0x01
FLAG_CMD_FRESH = 0x02
FLAG_FAILSAFE = 0x40


def compute_checksum(throttle, rudder, flags, seq):
    return ((throttle & 0xFF) + (rudder & 0xFF) + flags + seq) & 0xFF


def read_status(i2c):
    """Return the Pico status tuple, or None if the block's checksum doesn't match."""
    block = i2c.readfrom_mem(I2C_TARGET_ADDRESS, REG_STATUS, STATUS_LEN + 1)
    if sum(block[:STATUS_LEN]) & 0xFF != block[STATUS_LEN]:
        return None
    return struct.unpack(STATUS_FORMAT, block[:STATUS_LEN])


i2c = I2C(0, sda=Pin(I2C_SDA_PIN), scl=Pin(I2C_SCL_PIN), freq=100000)  # controller mode
led = Pin(LED_PIN, Pin.OUT)

seq = 0
now = time.ticks_ms()
last_send_time = now
last_report_time = now

while True:
    now = time.ticks_ms()
    if time.ticks_diff(now, last_send_time) < SEND_INTERVAL_MS:
        continue
    last_send_time = time.ticks_add(last_send_time, SEND_INTERVAL_MS)

    phase = 2 * math.pi * (now % WAVE_PERIOD_MS) / WAVE_PERIOD_MS
    throttle = round(WAVE_AMPLITUDE * math.cos(phase))
    rudder = round(WAVE_AMPLITUDE * math.sin(phase))
    flags = 0
    seq = (seq + 1) & 0xFF  # the Pico drops a frame whose seq didn't change
    checksum = compute_checksum(throttle, rudder, flags, seq)

    payload = struct.pack(CMD_FORMAT, throttle, rudder, flags, seq, checksum)
    try:
        i2c.writeto_mem(I2C_TARGET_ADDRESS, REG_CMD, payload)
        write_status = "ok"
    except OSError as e:
        write_status = "write failed (%s)" % e

    if time.ticks_diff(now, last_report_time) >= REPORT_INTERVAL_MS:
        last_report_time = time.ticks_add(last_report_time, REPORT_INTERVAL_MS)
        led.value(not led.value())

        line = "thr=%d rud=%d seq=%d: %s" % (throttle, rudder, seq, write_status)
        try:
            status = read_status(i2c)
        except OSError as e:
            line += " | status read failed (%s)" % e
        else:
            if status is None:
                line += " | status checksum mismatch"
            else:
                heartbeat, sflags, battery_mv, _, _, _, out_thr, out_rud, overruns, cmd_errors = status
                line += " | pico: hb=%d batt=%dmV %s%s%s out=%d/%dus overruns=%d cmd_errors=%d" % (
                    heartbeat,
                    battery_mv,
                    "ESP32" if sflags & FLAG_ESP32_MODE else "RC",
                    " cmd_fresh" if sflags & FLAG_CMD_FRESH else "",
                    " FAILSAFE" if sflags & FLAG_FAILSAFE else "",
                    out_thr,
                    out_rud,
                    overruns,
                    cmd_errors,
                )
        print(line)
