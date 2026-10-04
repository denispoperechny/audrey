"""Main vehicle controller (MicroPython). Started by main.py via run().

Runs on a Seeed XIAO ESP32-S3 (8 MB PSRAM) or the original ESP32 WROOM dev board; the
board is detected at startup and picks its pins from BOARD_PINS.

One cooperative loop runs fixed-rate tasks; WiFi/MQTT run in their own threads (mqtt.py)
so a network stall can't hold up the loop.

  task        period   what
  control       50 ms  decide throttle/rudder, send to the body control unit (bcu.py)
  compass      100 ms  read heading (compass.py), print it on the console
  gps          100 ms  read NMEA from the L76K GNSS module on a UART (gps.py)
  bcu_status  1000 ms  read the Pico's status block
  mqtt        1000 ms  handle received commands, queue telemetry
  report      1000 ms  heartbeat LED, garbage collection

The Pico is on the I2C bus at 0x31; the BNO08x compass and the L76K GNSS module each have
their own UART (pins in BOARD_PINS).

Resiliency -- this is an autonomous boat, so no single failure may stop the loop:
  - Every task runs inside its own try/except. An exception is counted, logged (rate-
    limited) and the loop moves on; the other tasks keep their schedule. Only Ctrl+C
    (KeyboardInterrupt) gets through, so mpremote can still take over.
  - A task that falls behind skips the missed runs instead of bursting to catch up.
  - Nothing is used without being fresh: the heading has to be newer than
    COMPASS_MAX_AGE_MS and the GPS fix has to be valid (gps.py), or control holds neutral.
    The Pico independently goes neutral if our commands stop for 750 ms.
  - A compass that stops sending gets its reports re-enabled first (no reset, so it keeps
    its calibration); only if that doesn't help is it re-initialized, at most every
    COMPASS_RETRY_MS. A missing compass doesn't fail boot.
  - If I2C writes to the Pico keep failing, the bus is recovered (9 SCL pulses + STOP to
    free a target stuck holding SDA, then the peripheral is re-created), at most every
    I2C_RECOVERY_INTERVAL_MS, backing off to I2C_RECOVERY_MAX_INTERVAL_MS while it doesn't
    help (e.g. the Pico is simply unplugged).
  - Missing config.py or a failing network start only disables MQTT.
  - Production mode (a file named "production" on the board) starts a hardware watchdog
    that resets the chip if the loop stops for WDT_MS. It can't be stopped once running,
    so the board first holds neutral for BOOT_WINDOW_MS, when Ctrl+C still gets through:
    reset, then run mpremote within that window to upload or remove the flag.
      Enable: mpremote fs touch :production     Disable: mpremote fs rm :production
  - Anything that escapes all of the above (a bug in the loop itself) is caught in
    main.py, which prints the error, commands neutral and resets the chip.

Navigation isn't wired in yet (see gnc.py): control() always commands neutral, but the
structure -- health checks first, then a decision with a reason -- is where it goes.
"""

import gc
import json
import os
import sys
import time

import esp32
import machine
from machine import I2C, Pin, SoftI2C

from bcu import BodyControlUnit
from gps import GpsLink

# compass (and its large bno08x driver) is imported only after WiFi has started; see
# Controller.__init__.

# ---- Pins ----
# Keyed by the start of the MicroPython build name (sys.implementation._build).
BOARD_PINS = {
    # Seeed XIAO ESP32-S3: I2C on the header's D4 (GPIO5) / D5 (GPIO6); user LED on GPIO21.
    # D6/D7 (GPIO43/44) are UART0 on the header: the MicroPython console, kept for it.
    # The BNO08x compass doesn't work on I2C with the S3 (2026-10-03: on hardware I2C, at
    # 100 and 50 kHz, init fails; bit-banged, reads return garbage every few minutes and the
    # compass then needs a reset), so it's on UART2 at 3 Mbaud (compass.py):
    # D1 (GPIO2) = RX <- compass SDA, D0 (GPIO1) = TX -> compass SCL.
    # With the compass gone from it, the I2C bus (now only the Pico) is back on the hardware
    # peripheral at 100 kHz.
    # L76K on UART1: D10 (GPIO9) = RX <- module TX, D9 (GPIO8) = TX -> module RX.
    "ESP32_GENERIC_S3": {"name": "XIAO ESP32-S3", "sda": 5, "scl": 6, "led": 21,
                         "soft_i2c": False, "i2c_freq": 100_000,
                         "gnss_uart": 1, "gnss_rx": 9, "gnss_tx": 8,
                         "compass_uart": 2, "compass_rx": 2, "compass_tx": 1},
    # ESP32 WROOM dev board: the default I2C pins; most of these boards put the LED on GPIO2.
    # L76K on UART2's usual pins, compass on UART1 moved off its default pins, which this
    # board uses for its flash (neither tried on this board).
    "ESP32_GENERIC": {"name": "ESP32 WROOM", "sda": 21, "scl": 22, "led": 2,
                      "soft_i2c": False, "i2c_freq": 100_000,
                      "gnss_uart": 2, "gnss_rx": 16, "gnss_tx": 17,
                      "compass_uart": 1, "compass_rx": 26, "compass_tx": 25},
}


def detect_board():
    build = getattr(sys.implementation, "_build", "")
    # Longest key first, so "ESP32_GENERIC_S3-..." doesn't match "ESP32_GENERIC".
    for key in sorted(BOARD_PINS, key=len, reverse=True):
        if build.startswith(key):
            return BOARD_PINS[key]
    raise RuntimeError("unknown board build %r; add it to BOARD_PINS" % build)


BOARD = detect_board()
I2C_SDA_PIN = BOARD["sda"]
I2C_SCL_PIN = BOARD["scl"]
LED_PIN = BOARD["led"]
I2C_SOFT = BOARD["soft_i2c"]
I2C_FREQ = BOARD["i2c_freq"]
I2C_TIMEOUT_US = 200_000  # generous: a target may stretch SCL

# ---- Task periods ----
CONTROL_PERIOD_MS = 50  # 20 Hz; the Pico treats a command older than 750 ms as lost
COMPASS_PERIOD_MS = 100  # 10 Hz
GPS_PERIOD_MS = 100  # the module sends 1 fix/s; polling often keeps fix timestamps close
# to when it arrived, and the UART buffers what comes in between
BCU_STATUS_PERIOD_MS = 1000
MQTT_PERIOD_MS = 1000
REPORT_PERIOD_MS = 1000

# ---- Health / recovery ----
COMPASS_MAX_AGE_MS = 500  # a heading older than this isn't used for control
COMPASS_LOST_MS = 3000  # no new heading for this long: re-enable its reports (no reset, ms);
# still none COMPASS_LOST_MS later: re-initialize it, which resets the chip (~1.5 s blocking)
COMPASS_RETRY_MS = 10000  # re-initialize at most this often
I2C_RECOVERY_AFTER_FAILS = 20  # consecutive failed writes to the Pico (1 s at 20 Hz)
I2C_RECOVERY_INTERVAL_MS = 5000  # doubles after each recovery that didn't help...
I2C_RECOVERY_MAX_INTERVAL_MS = 60000  # ...up to this; back to the start on a good write
ERROR_LOG_INTERVAL_MS = 5000  # per task, print at most one error line this often

# ---- Watchdog ----
PRODUCTION_FLAG_FILE = "production"
BOOT_WINDOW_MS = 3000
WDT_MS = 8000  # longest intentional block is a compass init or calibration save, ~2 s

RESET_CAUSES = {
    machine.PWRON_RESET: "power-on",
    machine.HARD_RESET: "hard",
    machine.WDT_RESET: "watchdog",
    machine.DEEPSLEEP_RESET: "deep sleep",
    machine.SOFT_RESET: "soft",
}


def make_i2c():
    if I2C_SOFT:
        return SoftI2C(sda=Pin(I2C_SDA_PIN), scl=Pin(I2C_SCL_PIN), freq=I2C_FREQ, timeout=I2C_TIMEOUT_US)
    return I2C(0, sda=Pin(I2C_SDA_PIN), scl=Pin(I2C_SCL_PIN), freq=I2C_FREQ, timeout=I2C_TIMEOUT_US)


def unstick_i2c_bus():
    """Frees a bus held by a target stuck mid-byte: clock SCL until it lets go of SDA,
    then send a STOP. The hardware peripheral has to be re-created afterwards."""
    scl = Pin(I2C_SCL_PIN, Pin.OPEN_DRAIN, value=1)
    sda = Pin(I2C_SDA_PIN, Pin.OPEN_DRAIN, value=1)
    for _ in range(9):
        scl(0)
        time.sleep_us(5)
        scl(1)
        time.sleep_us(5)
    sda(0)
    time.sleep_us(5)
    scl(1)
    time.sleep_us(5)
    sda(1)
    time.sleep_us(5)


def memory_stats():
    """MicroPython heap (it grows on demand by taking system RAM, and doesn't give it
    back) and the remaining system RAM, which WiFi and the TLS handshake allocate from."""
    heap_free = gc.mem_free()
    idf = esp32.idf_heap_info(esp32.HEAP_DATA)
    return {
        "heap_total": heap_free + gc.mem_alloc(),
        "heap_free": heap_free,
        "sys_free": sum(region[1] for region in idf),
        "sys_largest": max(region[2] for region in idf),
    }


def file_exists(name):
    try:
        os.stat(name)
        return True
    except OSError:
        return False


class Task:
    """A fixed-rate job; exceptions are contained, counted and logged."""

    def __init__(self, name, period_ms, fn):
        self.name = name
        self.period_ms = period_ms
        self.fn = fn
        self.due = time.ticks_ms()
        self.runs = 0
        self.skipped = 0  # runs dropped because the loop fell a whole period behind
        self.errors = 0
        self.last_error = ""
        self.last_log = None
        self.max_ms = 0  # longest run so far

    def run_if_due(self):
        now = time.ticks_ms()
        if time.ticks_diff(now, self.due) < 0:
            return
        self.due = time.ticks_add(self.due, self.period_ms)
        behind = time.ticks_diff(now, self.due)
        if behind >= 0:
            self.skipped += behind // self.period_ms + 1
            self.due = time.ticks_add(now, self.period_ms)

        try:
            self.fn(now)
        except Exception as e:  # KeyboardInterrupt is not an Exception, so Ctrl+C still works
            self.errors += 1
            self.last_error = "%s: %s" % (type(e).__name__, e)
            if self.last_log is None or time.ticks_diff(now, self.last_log) >= ERROR_LOG_INTERVAL_MS:
                self.last_log = now
                print("task %s error #%d: %s" % (self.name, self.errors, self.last_error))
        self.runs += 1
        self.max_ms = max(self.max_ms, time.ticks_diff(time.ticks_ms(), now))

    def ms_until_due(self, now):
        return time.ticks_diff(self.due, now)

    def stats(self):
        return {"runs": self.runs, "errors": self.errors, "skipped": self.skipped,
                "max_ms": self.max_ms, "last_error": self.last_error}


class Controller:
    def __init__(self):
        self.boot_ms = time.ticks_ms()
        self.reset_cause = RESET_CAUSES.get(machine.reset_cause(), str(machine.reset_cause()))
        self.led = Pin(LED_PIN, Pin.OUT)

        self.i2c = make_i2c()
        self.bcu = BodyControlUnit(self.i2c)
        self.bcu.send(0, 0)  # neutral as early as possible

        # WiFi before anything memory-hungry: its driver allocates from the same RAM the
        # MicroPython heap grows into, and compiling bno08x.py first left it with
        # "WiFi Out of Memory".
        gc.collect()
        self.mqtt = self.start_mqtt()

        self.gps = GpsLink(BOARD["gnss_uart"], BOARD["gnss_rx"], BOARD["gnss_tx"], time.ticks_ms())
        self.compass = None
        self.compass_attempt = None  # last init or report restart
        self.compass_init_at = None  # last init
        self.compass_inits = 0
        self.compass_restart_tried = False  # reports re-enabled since the last heading
        self.try_init_compass(time.ticks_ms())

        self.i2c_fail_streak = 0
        self.i2c_recoveries = 0
        self.last_i2c_recovery = None
        self.i2c_recovery_interval = I2C_RECOVERY_INTERVAL_MS

        # Latest control decision, for telemetry and the console.
        self.throttle = 0
        self.rudder = 0
        self.reason = "boot"

        self.last_command = None  # last parsed MQTT command (dict)
        self.last_command_ms = None
        self.bad_commands = 0

        self.tasks = [
            Task("control", CONTROL_PERIOD_MS, self.control),
            Task("compass", COMPASS_PERIOD_MS, self.read_compass),
            Task("gps", GPS_PERIOD_MS, self.read_gps),
            Task("bcu_status", BCU_STATUS_PERIOD_MS, self.read_bcu_status),
            Task("mqtt", MQTT_PERIOD_MS, self.exchange_mqtt),
            Task("report", REPORT_PERIOD_MS, self.report),
        ]

    # ---- startup helpers ---------------------------------------------------------------

    def start_mqtt(self):
        try:
            from config import config
            from mqtt import MqttLink

            link = MqttLink(config)
            link.start()
            return link
        except Exception as e:
            print("mqtt disabled: %s: %s" % (type(e).__name__, e))
            return None

    def try_init_compass(self, now):
        """(Re)creates the compass. Blocks ~1 s when the sensor is there; fails fast when
        it isn't. Failures are reported and retried later, never raised."""
        self.compass_attempt = now
        self.compass_init_at = now
        self.compass = None
        self.compass_restart_tried = False
        try:
            from compass import Compass

            self.compass = Compass(BOARD["compass_uart"], BOARD["compass_rx"], BOARD["compass_tx"])
            self.compass_inits += 1
            print("compass ready (init #%d)" % self.compass_inits)
        except Exception as e:
            print("compass init failed: %s: %s" % (type(e).__name__, e))

    # ---- tasks -----------------------------------------------------------------------

    def control(self, now):
        self.throttle, self.rudder, self.reason = self.decide(now)
        if self.bcu.send(self.throttle, self.rudder):
            self.i2c_fail_streak = 0
            self.i2c_recovery_interval = I2C_RECOVERY_INTERVAL_MS
        else:
            self.i2c_fail_streak += 1
            if self.i2c_fail_streak >= I2C_RECOVERY_AFTER_FAILS:
                self.recover_i2c(now)

    def decide(self, now):
        """Returns (throttle %, rudder %, reason). Health checks first: anything we can't
        trust means neutral."""
        if not self.heading_ok(now):
            return 0, 0, "hold: no heading"
        if not self.gps.fix_valid(now):
            return 0, 0, "hold: no gps fix"
        # Navigation goes here (gnc.py) once it's wired in.
        return 0, 0, "idle"

    def read_compass(self, now):
        compass = self.compass
        if not self.compass_lost(now):
            compass.update(now)  # errors are counted by the task; staleness decides recovery
            print("%.1f" % compass.heading)
            if compass.fresh(now, COMPASS_LOST_MS):
                self.compass_restart_tried = False
            return
        if compass is not None and not self.compass_restart_tried:
            # Cheap first: re-enable the reports. Re-initializing would reset the chip, which
            # drops the calibration it learned since its last save and blocks for ~1.5 s.
            self.compass_restart_tried = True
            self.compass_attempt = now
            print("compass: no heading for %d ms, re-enabling its reports" % COMPASS_LOST_MS)
            compass.restart_reports()  # an error here is counted by the task
        elif time.ticks_diff(now, self.compass_init_at) >= COMPASS_RETRY_MS:
            self.try_init_compass(now)

    def read_gps(self, now):
        self.gps.poll(now)

    def read_bcu_status(self, now):
        self.bcu.read_status()

    def exchange_mqtt(self, now):
        if self.mqtt is None:
            return
        while True:
            item = self.mqtt.get_command()
            if item is None:
                break
            self.handle_command(item[0], item[1])
        self.mqtt.publish(self.telemetry(now))

    def report(self, now):
        self.led.value(not self.led.value())
        gc.collect()  # at a known moment, instead of whenever an allocation triggers it

    # ---- helpers ---------------------------------------------------------------------

    def compass_lost(self, now):
        """No sensor, or no new heading for COMPASS_LOST_MS, counting from the last init or
        report restart if that's more recent."""
        compass = self.compass
        if compass is None:
            return True
        if time.ticks_diff(now, self.compass_attempt) <= COMPASS_LOST_MS:
            return False  # give a fresh init or report restart its full window
        return not compass.seen or time.ticks_diff(now, compass.stamp) > COMPASS_LOST_MS

    def describe_compass(self, now):
        if self.compass is None:
            return "compass: not available"
        line = self.compass.describe()
        return line if self.compass.fresh(now, COMPASS_MAX_AGE_MS) else line + " (STALE)"

    def heading_ok(self, now):
        return self.compass is not None and self.compass.fresh(now, COMPASS_MAX_AGE_MS)

    def handle_command(self, payload, received_ms):
        """Parses and records a command. Commands don't drive the outputs yet -- that's
        a deliberate next step, not an accidental one."""
        try:
            command = json.loads(payload)
            if not isinstance(command, dict):
                raise ValueError("not a JSON object")
        except ValueError as e:
            self.bad_commands += 1
            print("mqtt command rejected (%s): %r" % (e, payload))
            return
        self.last_command = command
        self.last_command_ms = received_ms
        print("mqtt command: %r" % (command,))

    def recover_i2c(self, now):
        if self.last_i2c_recovery is not None:
            if time.ticks_diff(now, self.last_i2c_recovery) < self.i2c_recovery_interval:
                return
            # Still failing since the last recovery: it didn't help, so try less often.
            self.i2c_recovery_interval = min(self.i2c_recovery_interval * 2, I2C_RECOVERY_MAX_INTERVAL_MS)
        self.last_i2c_recovery = now
        self.i2c_recoveries += 1
        print("i2c: %d failed writes in a row, recovering bus (#%d, next in >= %d s)" % (
            self.i2c_fail_streak, self.i2c_recoveries, self.i2c_recovery_interval // 1000))
        unstick_i2c_bus()
        self.i2c = make_i2c()
        self.bcu.i2c = self.i2c
        self.i2c_fail_streak = 0

    def describe_memory(self):
        m = memory_stats()
        return "mem: heap %d/%d KB free, sys %d KB free (largest %d KB)" % (
            m["heap_free"] // 1024, m["heap_total"] // 1024, m["sys_free"] // 1024, m["sys_largest"] // 1024)

    def task_errors_summary(self):
        failing = ["%s=%d" % (t.name, t.errors) for t in self.tasks if t.errors]
        return "task errors: " + (", ".join(failing) if failing else "none")

    def telemetry(self, now):
        compass = self.compass
        return {
            "uptime_s": time.ticks_diff(now, self.boot_ms) // 1000,
            "reset_cause": self.reset_cause,
            "memory": memory_stats(),
            "output": {"throttle": self.throttle, "rudder": self.rudder, "reason": self.reason},
            "compass": None if compass is None else {
                "heading": compass.heading,
                "accuracy": compass.accuracy,
                "fresh": compass.fresh(now, COMPASS_MAX_AGE_MS),
                "inits": self.compass_inits,
                "report_restarts": compass.report_restarts,
                "calibration_saved": compass.calibration_saved,
                "saves": compass.saves,
                "save_failures": compass.save_failures,
            },
            "gps": self.gps.status_dict(now),
            "bcu": self.bcu.status_dict(),
            "bcu_errors": {
                "write": self.bcu.write_errors,
                "status_i2c": self.bcu.status_i2c_errors,
                "status_checksum": self.bcu.status_checksum_errors,
            },
            "i2c_recoveries": self.i2c_recoveries,
            "last_command": self.last_command,
            "bad_commands": self.bad_commands,
            "tasks": {t.name: t.stats() for t in self.tasks},
        }

    # ---- main loop -------------------------------------------------------------------

    def hold_boot_window(self):
        """Production mode only: hold neutral with no watchdog yet, so Ctrl+C can still
        reach the REPL for uploads."""
        print("production mode: %d ms boot window (Ctrl+C now to stop)" % BOOT_WINDOW_MS)
        end = time.ticks_add(time.ticks_ms(), BOOT_WINDOW_MS)
        while time.ticks_diff(end, time.ticks_ms()) > 0:
            try:
                self.bcu.send(0, 0)
            except Exception:
                pass
            time.sleep_ms(CONTROL_PERIOD_MS)

    def run(self):
        wdt = None
        if file_exists(PRODUCTION_FLAG_FILE):
            self.hold_boot_window()
            wdt = machine.WDT(timeout=WDT_MS)
        print("controller running on %s (%s I2C SDA=%d SCL=%d %d kHz), reset cause: %s, watchdog: %s" % (
            BOARD["name"], "soft" if I2C_SOFT else "hw", I2C_SDA_PIN, I2C_SCL_PIN, I2C_FREQ // 1000,
            self.reset_cause, "on" if wdt else "off (dev mode)"))

        tasks = self.tasks
        while True:
            for task in tasks:
                task.run_if_due()
            if wdt is not None:
                wdt.feed()
            now = time.ticks_ms()
            wait = min(task.ms_until_due(now) for task in tasks)
            if wait > 0:
                time.sleep_ms(wait)  # also lets the WiFi/MQTT threads run



def run():
    Controller().run()
