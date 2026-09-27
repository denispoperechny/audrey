"""Link to the body control unit (the Pico, electronics/body_control_unit_rpipico_mp).

The Pico is a register-addressed I2C target at 0x31 (register map in its main.py):
  0x00  command frame, ESP32 -> Pico: throttle int8, rudder int8 (-100..100 %), flags,
        seq, checksum = (throttle + rudder + flags + seq) & 0xFF as unsigned bytes.
        seq must change on every frame, or the Pico treats it as stale.
  0x10  status block, Pico -> ESP32: 16 bytes + a sum-of-bytes checksum at 0x20.

The Pico goes to neutral if it sees no fresh command for 750 ms (its CMD_FAILSAFE_MS), so
send() has to keep being called -- the old main loop did it at 20 Hz.
"""

import struct

BCU_I2C_ADDR = 0x31

REG_CMD = 0x00
CMD_FORMAT = "<bbBBB"  # throttle, rudder, flags, seq, checksum
REG_STATUS = 0x10
STATUS_LEN = 16
# heartbeat, flags, battery mV, rc mode/thr/rud us, out thr/rud us, overruns, cmd errors
STATUS_FORMAT = "<BBHHHHHHBB"

FLAG_ESP32_MODE = 0x01  # RC mode switch hands control to the ESP32
FLAG_CMD_FRESH = 0x02  # the Pico has a fresh command from us
FLAG_RC_THROTTLE_FRESH = 0x04
FLAG_RC_RUDDER_FRESH = 0x08
FLAG_RC_MODE_FRESH = 0x10
FLAG_LOW_BATTERY = 0x20
FLAG_FAILSAFE = 0x40  # outputs forced to neutral
FLAG_WDT_RESET = 0x80  # the Pico's last boot was a watchdog reset


def compute_checksum(throttle, rudder, flags, seq):
    return ((throttle & 0xFF) + (rudder & 0xFF) + flags + seq) & 0xFF


def clamp_percent(value):
    return max(-100, min(100, int(value)))


class BodyControlUnit:
    """Sends throttle/rudder commands to the Pico and reads back its status block."""

    def __init__(self, i2c, addr=BCU_I2C_ADDR):
        self.i2c = i2c
        self.addr = addr
        self.seq = 0

        # Last command sent, and whether the write made it.
        self.throttle = 0
        self.rudder = 0
        self.write_ok = False
        self.write_errors = 0

        # Failed status reads, by cause: the bus (NACK or timeout) vs. a bad checksum.
        self.status_ok = False
        self.status_i2c_errors = 0
        self.status_checksum_errors = 0

        # Fields of the last good status block.
        self.heartbeat = 0
        self.flags = 0
        self.battery_mv = 0
        self.rc_mode_us = 0  # 0 = no signal
        self.rc_throttle_us = 0
        self.rc_rudder_us = 0
        self.out_throttle_us = 0  # what the Pico is actually sending to the servos
        self.out_rudder_us = 0
        self.overruns = 0
        self.cmd_errors = 0  # our frames the Pico rejected for a bad checksum

    def send(self, throttle, rudder, flags=0):
        """Writes one command frame (throttle/rudder in %). Returns True if the write went through."""
        throttle = clamp_percent(throttle)
        rudder = clamp_percent(rudder)
        self.seq = (self.seq + 1) & 0xFF  # the Pico drops a frame whose seq didn't change
        checksum = compute_checksum(throttle, rudder, flags, self.seq)
        payload = struct.pack(CMD_FORMAT, throttle, rudder, flags, self.seq, checksum)
        self.throttle = throttle
        self.rudder = rudder
        try:
            self.i2c.writeto_mem(self.addr, REG_CMD, payload)
        except OSError:
            self.write_ok = False
            self.write_errors += 1
            return False
        self.write_ok = True
        return True

    def read_status(self):
        """Reads the status block. Returns True if a good one arrived."""
        try:
            block = self.i2c.readfrom_mem(self.addr, REG_STATUS, STATUS_LEN + 1)
        except OSError:
            self.status_ok = False
            self.status_i2c_errors += 1
            return False
        if sum(block[:STATUS_LEN]) & 0xFF != block[STATUS_LEN]:
            self.status_ok = False
            self.status_checksum_errors += 1
            return False
        (self.heartbeat, self.flags, self.battery_mv, self.rc_mode_us, self.rc_throttle_us,
         self.rc_rudder_us, self.out_throttle_us, self.out_rudder_us, self.overruns,
         self.cmd_errors) = struct.unpack(STATUS_FORMAT, block[:STATUS_LEN])
        self.status_ok = True
        return True

    def status_dict(self):
        """The last status as a dict for telemetry, or None if the last read failed."""
        if not self.status_ok:
            return None
        flags = self.flags
        return {
            "heartbeat": self.heartbeat,
            "esp32_mode": bool(flags & FLAG_ESP32_MODE),
            "cmd_fresh": bool(flags & FLAG_CMD_FRESH),
            "failsafe": bool(flags & FLAG_FAILSAFE),
            "low_battery": bool(flags & FLAG_LOW_BATTERY),
            "wdt_reset": bool(flags & FLAG_WDT_RESET),
            "battery_mv": self.battery_mv,
            "rc_mode_us": self.rc_mode_us,
            "rc_throttle_us": self.rc_throttle_us,
            "rc_rudder_us": self.rc_rudder_us,
            "out_throttle_us": self.out_throttle_us,
            "out_rudder_us": self.out_rudder_us,
            "overruns": self.overruns,
            "cmd_errors": self.cmd_errors,
        }

    def describe(self):
        """One-line status for the console."""
        line = "bcu: cmd thr=%d rud=%d seq=%d %s" % (
            self.throttle, self.rudder, self.seq,
            "ok" if self.write_ok else "write failed (errors: %d)" % self.write_errors)
        if not self.status_ok:
            return line + " | status read failed (i2c errors: %d, checksum: %d)" % (
                self.status_i2c_errors, self.status_checksum_errors)
        flags = self.flags
        return line + " | hb=%d batt=%dmV %s%s%s%s out=%d/%dus overruns=%d cmd_errors=%d" % (
            self.heartbeat,
            self.battery_mv,
            "ESP32" if flags & FLAG_ESP32_MODE else "RC",
            " cmd_fresh" if flags & FLAG_CMD_FRESH else "",
            " FAILSAFE" if flags & FLAG_FAILSAFE else "",
            " LOW-BATTERY" if flags & FLAG_LOW_BATTERY else "",
            self.out_throttle_us,
            self.out_rudder_us,
            self.overruns,
            self.cmd_errors,
        )
