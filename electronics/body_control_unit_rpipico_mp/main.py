"""Body control unit (RP2040).

Reads three RC PWM inputs (PIO) and commands from the ESP32 (I2C target, register map),
decides throttle/rudder servo pulses, drives them with hardware PWM, and reports the
battery voltage and status back over I2C and the UART/USB console.

I2C register map (little-endian, 8-bit register address, target address I2C_ADDR).
The ESP32 writes register 0x00 followed by the bytes, or writes the register address and
then reads:

  0x00  throttle      int8   -100..100 (%)          ESP32 -> Pico (command frame)
  0x01  rudder        int8   -100..100 (%)
  0x02  flags         uint8  reserved
  0x03  seq           uint8  must change on every frame, or the frame is stale
  0x04  checksum      uint8  (throttle + rudder + flags + seq) & 0xFF, as unsigned bytes

  0x10  heartbeat     uint8  increments every loop pass   Pico -> ESP32 (status block)
  0x11  status flags  uint8  FLAG_* bits below
  0x12  battery       uint16 mV
  0x14  rc mode       uint16 us (0 = no signal)
  0x16  rc throttle   uint16 us (0 = no signal)
  0x18  rc rudder     uint16 us (0 = no signal)
  0x1A  out throttle  uint16 us (what is being sent to the servo)
  0x1C  out rudder    uint16 us
  0x1E  overruns      uint8  loop passes that took longer than LOOP_MS (wraps)
  0x1F  cmd errors    uint8  command frames with a bad checksum (wraps)
  0x20  checksum      uint8  sum of bytes 0x10..0x1F & 0xFF; re-read if it doesn't match
"""

import gc
import os
import select
import struct
import sys
import time

import machine
import rp2
from machine import ADC, PWM, WDT, I2CTarget, Pin

# ---- Pins (see pins_map.txt) ----
OUT_THROTTLE_PIN = 2  # PWM Write 1
OUT_RUDDER_PIN = 4  # PWM Write 2
IN_THROTTLE_PIN = 3  # PWM Read 1
IN_RUDDER_PIN = 5  # PWM Read 2
IN_MODE_PIN = 7  # PWM Read 3: selects RC (low) or ESP32 (high) control
I2C_ID = 1
I2C_SDA_PIN = 14
I2C_SCL_PIN = 15
I2C_ADDR = 0x31
BATTERY_ADC_PIN = 28  # 2S pack through a 10k/5k divider: Vbat = Vadc * 3

# ---- Servo / RC signal ----
SERVO_HZ = 50
NEUTRAL_US = 1500
MIN_US = 1000
MAX_US = 2000
US_PER_PERCENT = 5  # ESP32 command -100..100 -> 1000..2000 us
RC_VALID_MIN_US = 800  # measured pulses outside this range are ignored as noise
RC_VALID_MAX_US = 2200
MODE_HIGH_US = 1600  # above: ESP32 control; below MODE_LOW_US: RC control; between: keep
MODE_LOW_US = 1400

# ---- Timing ----
LOOP_MS = 20  # 50 Hz
FAILSAFE_MS = 200  # a source older than this is lost and its output goes neutral
REPORT_MS = 1000  # console print period
WDT_MS = 2000

# ---- Battery ----
ADC_VREF_MV = 3300
DIVIDER_RATIO = 3
ADC_SAMPLES = 8
LOW_BATTERY_MV = 6400  # 3.2 V per cell; reported only, no action taken

# ---- I2C register map ----
REG_CMD = 0x00
REG_STATUS = 0x10
STATUS_LEN = 16
REG_STATUS_SUM = REG_STATUS + STATUS_LEN
MEM_SIZE = REG_STATUS_SUM + 1

FLAG_ESP32_MODE = 0x01
FLAG_CMD_FRESH = 0x02
FLAG_RC_THROTTLE_FRESH = 0x04
FLAG_RC_RUDDER_FRESH = 0x08
FLAG_RC_MODE_FRESH = 0x10
FLAG_LOW_BATTERY = 0x20
FLAG_FAILSAFE = 0x40
FLAG_WDT_RESET = 0x80

# Production mode: the watchdog is only started when this file exists on the board. Once
# started a watchdog can't be stopped, and it would reset the board WDT_MS after a Ctrl+C
# to the REPL, which breaks uploads. So in production mode the board first sits at neutral
# for BOOT_WINDOW_MS with the watchdog off, listening for Ctrl+C: reset the board, then run
# mpremote within that window to upload or to remove the flag.
# Enable: mpremote fs touch :production     Disable: mpremote fs rm :production
PRODUCTION_FLAG_FILE = "production"
BOOT_WINDOW_MS = 3000


# ---- Decision logic (pure, no hardware access) ----


def clamp_us(us):
    if us < MIN_US:
        return MIN_US
    if us > MAX_US:
        return MAX_US
    return us


def percent_to_us(percent):
    if percent < -100:
        percent = -100
    elif percent > 100:
        percent = 100
    return NEUTRAL_US + percent * US_PER_PERCENT


def select_mode(was_esp32, mode_us):
    """True = ESP32 control. mode_us is None when the mode input is lost (-> RC)."""
    if mode_us is None:
        return False
    if mode_us > MODE_HIGH_US:
        return True
    if mode_us < MODE_LOW_US:
        return False
    return was_esp32


def decide(use_esp32, rc_throttle_us, rc_rudder_us, cmd_throttle, cmd_rudder):
    """Return (throttle_us, rudder_us). An input that is None is lost -> neutral."""
    if use_esp32:
        throttle = None if cmd_throttle is None else percent_to_us(cmd_throttle)
        rudder = None if cmd_rudder is None else percent_to_us(cmd_rudder)
    else:
        throttle = None if rc_throttle_us is None else clamp_us(rc_throttle_us)
        rudder = None if rc_rudder_us is None else clamp_us(rc_rudder_us)
    return (
        NEUTRAL_US if throttle is None else throttle,
        NEUTRAL_US if rudder is None else rudder,
    )


# ---- Hardware ----


# Measures the high time of each pulse in us and pushes it to the RX FIFO. Two PIO cycles
# per count, so the state machine runs at 2 MHz. If the pin has no signal the program just
# waits, and the loop sees no new data.
@rp2.asm_pio()
def _pulse_width():
    wrap_target()
    wait(0, pin, 0)
    wait(1, pin, 0)
    mov(x, invert(null))
    label("count")
    jmp(pin, "high")
    jmp("done")
    label("high")
    jmp(x_dec, "count")
    label("done")
    mov(isr, invert(x))
    push(noblock)
    wrap()


class PulseInput:
    def __init__(self, sm_id, gpio):
        pin = Pin(gpio, Pin.IN, Pin.PULL_DOWN)
        self.sm = rp2.StateMachine(sm_id, _pulse_width, freq=2_000_000, in_base=pin, jmp_pin=pin)
        self.sm.active(1)
        self.us = 0
        self.stamp = 0
        self.seen = False

    def poll(self, now):
        sm = self.sm
        for _ in range(8):  # drain the FIFO; the last valid pulse wins
            if not sm.rx_fifo():
                break
            us = sm.get()
            if RC_VALID_MIN_US <= us <= RC_VALID_MAX_US:
                self.us = us
                self.stamp = now
                self.seen = True

    def fresh(self, now):
        return self.seen and time.ticks_diff(now, self.stamp) <= FAILSAFE_MS

    def value(self, now):
        return self.us if self.fresh(now) else None


class CommandLink:
    """Reads the ESP32 command frame from the I2C register memory."""

    def __init__(self, mem):
        self.mem = mem
        self.seq = mem[REG_CMD + 3]  # the zeroed memory at boot is not a fresh frame
        self.stamp = 0
        self.seen = False
        self.throttle = 0
        self.rudder = 0
        self.errors = 0

    def poll(self, now):
        m = self.mem
        seq = m[REG_CMD + 3]
        if seq == self.seq:
            return
        if (m[REG_CMD] + m[REG_CMD + 1] + m[REG_CMD + 2] + seq) & 0xFF != m[REG_CMD + 4]:
            self.errors = (self.errors + 1) & 0xFF
            return  # torn or corrupt frame; the next pass sees the complete one
        self.seq = seq
        self.throttle = m[REG_CMD] - 256 if m[REG_CMD] > 127 else m[REG_CMD]
        self.rudder = m[REG_CMD + 1] - 256 if m[REG_CMD + 1] > 127 else m[REG_CMD + 1]
        self.stamp = now
        self.seen = True

    def fresh(self, now):
        return self.seen and time.ticks_diff(now, self.stamp) <= FAILSAFE_MS


class Battery:
    def __init__(self, gpio):
        self.adc = ADC(Pin(gpio))
        self.mv = self._sample()

    def _sample(self):
        adc = self.adc
        raw = 0
        for _ in range(ADC_SAMPLES):
            raw += adc.read_u16()
        return raw // ADC_SAMPLES * ADC_VREF_MV * DIVIDER_RATIO // 65535

    def update(self):
        self.mv += (self._sample() - self.mv) >> 2  # light low-pass against motor noise
        return self.mv


# Ctrl+C sent over the dupterm UART can't interrupt a running script by itself, but
# reading it from stdin raises KeyboardInterrupt. So the loop reads all pending stdin
# (USB + UART) every pass; a Ctrl+C then ends the script and drops to the REPL, which
# lets mpremote upload over UART. Everything else that was read is returned as text.
_poll = select.poll()
_poll.register(sys.stdin, select.POLLIN)


def read_input():
    data = ""
    while _poll.poll(0):
        data += sys.stdin.read(1)
    return data


def main():
    watchdog_reset = machine.reset_cause() == machine.WDT_RESET

    mem = bytearray(MEM_SIZE)
    status_view = memoryview(mem)[REG_STATUS : REG_STATUS + STATUS_LEN]
    target = I2CTarget(I2C_ID, I2C_ADDR, mem=mem, scl=Pin(I2C_SCL_PIN), sda=Pin(I2C_SDA_PIN))

    out_throttle = PWM(Pin(OUT_THROTTLE_PIN))
    out_rudder = PWM(Pin(OUT_RUDDER_PIN))
    for pwm in (out_throttle, out_rudder):
        pwm.freq(SERVO_HZ)
        pwm.duty_ns(NEUTRAL_US * 1000)  # neutral from the very first frame

    rc_throttle = PulseInput(0, IN_THROTTLE_PIN)
    rc_rudder = PulseInput(1, IN_RUDDER_PIN)
    rc_mode = PulseInput(2, IN_MODE_PIN)
    command = CommandLink(mem)
    battery = Battery(BATTERY_ADC_PIN)

    use_esp32 = False
    heartbeat = 0
    overruns = 0
    last_report = time.ticks_ms()

    next_tick = 0
    wdt = None
    try:
        if PRODUCTION_FLAG_FILE in os.listdir():
            end = time.ticks_add(time.ticks_ms(), BOOT_WINDOW_MS)
            while time.ticks_diff(end, time.ticks_ms()) > 0:
                read_input()  # a Ctrl+C here drops to the REPL with no watchdog running
                time.sleep_ms(LOOP_MS)
            wdt = WDT(timeout=WDT_MS)
        print("watchdog:", "on" if wdt else "off (dev mode)")

        next_tick = time.ticks_add(time.ticks_ms(), LOOP_MS)
        while True:
            now = time.ticks_ms()
            data = read_input()
            if data:
                print("rx:", repr(data))

            rc_throttle.poll(now)
            rc_rudder.poll(now)
            rc_mode.poll(now)
            command.poll(now)

            use_esp32 = select_mode(use_esp32, rc_mode.value(now))
            cmd_ok = command.fresh(now)
            rc_thr_us = rc_throttle.value(now)
            rc_rud_us = rc_rudder.value(now)
            out_thr_us, out_rud_us = decide(
                use_esp32,
                rc_thr_us,
                rc_rud_us,
                command.throttle if cmd_ok else None,
                command.rudder if cmd_ok else None,
            )
            out_throttle.duty_ns(out_thr_us * 1000)
            out_rudder.duty_ns(out_rud_us * 1000)

            battery_mv = battery.update()
            if use_esp32:
                failsafe = not cmd_ok
            else:
                failsafe = rc_thr_us is None or rc_rud_us is None

            flags = 0
            if use_esp32:
                flags |= FLAG_ESP32_MODE
            if cmd_ok:
                flags |= FLAG_CMD_FRESH
            if rc_thr_us is not None:
                flags |= FLAG_RC_THROTTLE_FRESH
            if rc_rud_us is not None:
                flags |= FLAG_RC_RUDDER_FRESH
            if rc_mode.fresh(now):
                flags |= FLAG_RC_MODE_FRESH
            if battery_mv < LOW_BATTERY_MV:
                flags |= FLAG_LOW_BATTERY
            if failsafe:
                flags |= FLAG_FAILSAFE
            if watchdog_reset:
                flags |= FLAG_WDT_RESET

            heartbeat = (heartbeat + 1) & 0xFF
            struct.pack_into(
                "<BBHHHHHHBB",
                status_view,
                0,
                heartbeat,
                flags,
                battery_mv,
                rc_mode.us if rc_mode.fresh(now) else 0,
                rc_thr_us or 0,
                rc_rud_us or 0,
                out_thr_us,
                out_rud_us,
                overruns,
                command.errors,
            )
            mem[REG_STATUS_SUM] = sum(status_view) & 0xFF

            if time.ticks_diff(now, last_report) >= REPORT_MS:
                last_report = time.ticks_add(last_report, REPORT_MS)
                print(
                    "battery: %d.%02d V | %s | thr %d rud %d us%s"
                    % (
                        battery_mv // 1000,
                        battery_mv % 1000 // 10,
                        "ESP32" if use_esp32 else "RC",
                        out_thr_us,
                        out_rud_us,
                        " | FAILSAFE" if failsafe else "",
                    )
                )

            # Collect garbage now, in the idle part of the slot, so a collection never
            # lands in the middle of a pass (a full collect takes ~0.5 ms).
            gc.collect()
            if wdt:
                wdt.feed()

            remaining = time.ticks_diff(next_tick, time.ticks_ms())
            if remaining > 0:
                time.sleep_ms(remaining)
            else:
                overruns = (overruns + 1) & 0xFF
                next_tick = time.ticks_ms()
            next_tick = time.ticks_add(next_tick, LOOP_MS)
    finally:
        for pwm in (out_throttle, out_rudder):
            pwm.deinit()  # stop pulses so the servos don't buzz after Ctrl+C
        for rc in (rc_throttle, rc_rudder, rc_mode):
            rc.sm.active(0)
        target.deinit()


if __name__ == "__main__":
    main()
