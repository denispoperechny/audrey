"""GNSS fixes from the Quectel L76K, wired straight to a UART of this board.

Replaces the Heltec V4 board (electronics/heltec_gnss_reader), which read the L76K and
served fixes over I2C (the old reader is kept as gnss_i2c.py). This module does what that
firmware's src/main.cpp did, with the same rules -- see its DECISIONS.md for the why:

  - The L76K (AT6558R chip) takes CASIC "$PCAS" commands, not MediaTek "$PMTK", and never
    acknowledges them. It also keeps PCAS settings across resets, so every value is sent
    on every start: GPS + BeiDou, 1 Hz, only GGA/GSA/RMC. Baud and fix rate stay at the
    proven 9600 / 1 Hz.
  - Sentences are only used with a valid checksum; a '$' always starts a new sentence.
  - A fix is GGA quality 1-5 with in-range coordinates (6-8 are estimated / manual /
    simulated). It is "good" with >= 4 satellites and HDOP (from GSA) <= 2.5, or no HDOP
    seen yet. Speed/course come from RMC, only when it reports itself valid ('A').
  - Nothing is used stale: a fix older than FIX_MAX_AGE_MS (one missed 1 Hz fix plus
    margin) isn't valid, and a GGA without a fix invalidates the last one right away. The
    position fields keep the last known fix, but only fix_valid() says it's current.
    seq and the fix stamp only change when a new fix arrives.
  - The link is up while checksum-valid GGA sentences (fix or not) keep arriving. Silent
    for GNSS_RECOVERY_AFTER_MS: re-initialize the UART, send "9600 baud" at 115200 (in
    case a past session left the module there; the chip keeps that too), go back to 9600
    and re-send the config. At most every GNSS_RECOVERY_INTERVAL_MS. The Heltec also
    power-cycled the module; only TX/RX are wired here, so that part is left out.

Differences from the Heltec firmware, because this runs inside the controller's loop:
  - PCAS commands are queued and sent one per poll() instead of with blocking 100 ms
    delays in between, so a (re)configuration never holds up the loop.
  - Fix and RMC times are stamped when poll() reads them, up to one poll period after the
    module sent them (the Heltec read in a tight loop).
  - Coordinates are parsed into integer degrees * 1e7 (lat_e7 / lon_e7): MicroPython
    floats are 32-bit here, about half a metre of rounding at these magnitudes. lat / lon
    are float conveniences for display.
"""

import time

from machine import UART

GNSS_BAUD = 9600
RESCUE_BAUD = 115200

FIX_MAX_AGE_MS = 2500  # a 1 Hz fix older than this (one missed fix + margin) isn't current
LINK_TIMEOUT_MS = 3000  # no checksum-valid GGA (fix or not) for this long = link down
GNSS_RECOVERY_AFTER_MS = 5000  # ...and for this long = try to recover the module,
GNSS_RECOVERY_INTERVAL_MS = 10000  # at most this often
MAX_SENTENCE_LEN = 120  # NMEA allows 82; longer = corrupt / unterminated, dropped

GOOD_FIX_MIN_SATS = 4
GOOD_FIX_MAX_HDOP = 2.5

CONFIG_COMMANDS = (
    "PCAS04,3",  # GPS + BeiDou
    "PCAS02,1000",  # 1 Hz fix rate
    "PCAS03,1,0,1,0,1,0,0,0,0,0,,,0,0",  # GGA/GSA/RMC every fix; GLL/GSV/VTG/ZDA/ANT off
)
RESCUE_COMMAND = "PCAS01,1"  # 1 = 9600 baud; just noise to a module already at 9600


def pcas_sentence(body):
    """'$<body>*XX\\r\\n' with the NMEA checksum, e.g. body = 'PCAS04,3'."""
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return "$%s*%02X\r\n" % (body, checksum)


def checked_body(line):
    """The part between '$' and '*' of a sentence with a valid checksum, else None."""
    star = line.rfind(b"*")
    if line[:1] != b"$" or star < 1 or star + 2 >= len(line):
        return None
    checksum = 0
    for b in line[1:star]:
        checksum ^= b
    try:
        expected = int(line[star + 1:star + 3], 16)
    except ValueError:
        return None
    return line[1:star] if checksum == expected else None


def coord_e7(raw, hemisphere):
    """NMEA 'ddmm.mmmm' / 'dddmm.mmmm' + hemisphere -> degrees * 1e7, or None."""
    whole, _, fraction = raw.partition(".")
    if len(whole) < 3 or not whole.isdigit() or (fraction and not fraction.isdigit()):
        return None
    degrees, minutes = divmod(int(whole), 100)
    if minutes >= 60:
        return None
    minutes_e7 = minutes * 10_000_000 + int((fraction + "0000000")[:7])
    value = degrees * 10_000_000 + (minutes_e7 + 30) // 60
    return -value if hemisphere in ("S", "W") else value


def to_float(raw, default=0.0):
    try:
        return float(raw)
    except ValueError:
        return default


def to_int(raw, default=0):
    try:
        return int(raw)
    except ValueError:
        return default


def format_e7(value):
    sign = "-" if value < 0 else ""
    value = abs(value)
    return "%s%d.%07d" % (sign, value // 10_000_000, value % 10_000_000)


class GpsLink:
    """Reads the L76K on a UART; call poll(now) often (every 100-200 ms)."""

    def __init__(self, uart_id, rx, tx, now):
        self.uart_id = uart_id
        self.rx = rx
        self.tx = tx
        self.uart = None
        self.baud = None
        self.pending = []  # (baud, PCAS body) still to send, one per poll()
        self.buf = b""

        # Counters.
        self.sentences = 0  # checksum-valid sentences
        self.checksum_errors = 0
        self.overflows = 0  # over-long lines dropped
        self.recoveries = 0
        self.seq = 0  # +1 per new fix (fixes since start)

        # Link / recovery timing. Counted from now, so the module gets a full window first.
        self.gga_seen = False
        self.last_gga = now
        self.last_recovery = now

        # Last known fix; see fix_valid() before using it.
        self.has_position = False
        self.fix_flag = False  # the latest GGA reported a fix
        self.good_flag = False  # ...that passed the sats/HDOP gate
        self.fix_stamp = now
        self.lat_e7 = 0
        self.lon_e7 = 0
        self.lat = 0.0
        self.lon = 0.0
        self.alt_m = 0.0
        self.sats = 0  # satellites used in the last fix
        self.fix_quality = 0
        self.hdop = None  # from GSA; None = not seen yet
        self.gga_sats = 0  # satellites in the latest GGA, fix or not (for the console)
        self.gga_quality = 0

        # Motion, from RMC.
        self.motion_flag = False
        self.motion_stamp = now
        self.sog_kn = 0.0
        self.cog_deg = 0.0

        self._open(GNSS_BAUD)
        self._queue_config()

    # ---- UART / module config --------------------------------------------------------

    def _open(self, baud):
        if self.uart is None:
            self.uart = UART(self.uart_id, baudrate=baud, rx=self.rx, tx=self.tx, rxbuf=2048)
        else:
            self.uart.init(baudrate=baud, rx=self.rx, tx=self.tx, rxbuf=2048)
        self.baud = baud

    def _queue_config(self):
        self.pending.extend((GNSS_BAUD, body) for body in CONFIG_COMMANDS)

    def _send_pending(self):
        """Sends one queued command. The previous one has had a whole poll period to go
        out, so switching the baud rate here doesn't cut it off."""
        if not self.pending:
            return
        baud, body = self.pending.pop(0)
        if baud != self.baud:
            self._open(baud)
            self.buf = b""  # bytes received at the old rate are garbage now
        self.uart.write(pcas_sentence(body))

    def _check_link(self, now):
        silent_ms = time.ticks_diff(now, self.last_gga)
        if silent_ms < GNSS_RECOVERY_AFTER_MS:
            return
        if time.ticks_diff(now, self.last_recovery) < GNSS_RECOVERY_INTERVAL_MS:
            return
        print("gps: no data for %d ms, re-initializing the GNSS module" % silent_ms)
        self.last_recovery = now
        self.recoveries += 1
        self.pending = [(RESCUE_BAUD, RESCUE_COMMAND)]
        self._queue_config()

    # ---- reading ---------------------------------------------------------------------

    def poll(self, now):
        """Reads what the module sent, sends one queued command, checks the link."""
        data = self.uart.read()
        if data:
            self._feed(data, now)
        self._send_pending()
        self._check_link(now)

    def _feed(self, data, now):
        buf = self.buf + data
        while True:
            end = buf.find(b"\n")
            if end < 0:
                break
            line = buf[:end]
            buf = buf[end + 1:]
            start = line.rfind(b"$")  # resync: a '$' always starts a new sentence
            if start < 0:
                continue
            line = line[start:].strip()
            if len(line) > MAX_SENTENCE_LEN:
                self.overflows += 1
                continue
            self._handle_line(line, now)
        if len(buf) > MAX_SENTENCE_LEN:
            # No newline in sight: keep only a sentence that may still be in progress.
            start = buf.rfind(b"$")
            buf = buf[start:] if start >= 0 and len(buf) - start <= MAX_SENTENCE_LEN else b""
            self.overflows += 1
        self.buf = buf

    def _handle_line(self, line, now):
        body = checked_body(line)
        if body is None:
            self.checksum_errors += 1
            return
        self.sentences += 1
        try:
            fields = body.decode().split(",")
        except UnicodeError:
            return
        kind = fields[0][2:5]  # "GNGGA" -> "GGA"
        if kind == "GGA":
            self._handle_gga(fields, now)
        elif kind == "GSA":
            self._handle_gsa(fields)
        elif kind == "RMC":
            self._handle_rmc(fields, now)

    def _handle_gga(self, f, now):
        if len(f) < 10:
            return
        quality = to_int(f[6])
        sats = to_int(f[7])
        self.last_gga = now  # any valid GGA proves the link is alive, fix or not
        self.gga_seen = True
        self.gga_sats = sats
        self.gga_quality = quality

        lat = lon = None
        if 1 <= quality <= 5:
            lat = coord_e7(f[2], f[3])
            lon = coord_e7(f[4], f[5])
        if lat is None or lon is None or abs(lat) > 900_000_000 or abs(lon) > 1_800_000_000:
            # Nothing new was measured: leave seq/stamp/position alone and just stop
            # vouching for the old fix.
            self.fix_flag = False
            self.good_flag = False
            return

        self.seq += 1
        self.has_position = True
        self.fix_flag = True
        self.good_flag = sats >= GOOD_FIX_MIN_SATS and (self.hdop is None or self.hdop <= GOOD_FIX_MAX_HDOP)
        self.fix_stamp = now
        self.lat_e7 = lat
        self.lon_e7 = lon
        self.lat = lat / 1e7
        self.lon = lon / 1e7
        self.alt_m = to_float(f[9])
        self.sats = sats
        self.fix_quality = quality

    def _handle_gsa(self, f):
        if len(f) < 17:
            return
        hdop = to_float(f[16])
        if hdop > 0:
            self.hdop = hdop

    def _handle_rmc(self, f, now):
        if len(f) < 9:
            return
        if f[2] != "A":
            self.motion_flag = False
            return
        self.motion_flag = True
        self.motion_stamp = now
        self.sog_kn = to_float(f[7])  # empty when not moving: 0
        self.cog_deg = to_float(f[8])

    # ---- state -----------------------------------------------------------------------

    def link_ok(self, now):
        """The module is talking: a valid GGA (fix or not) within LINK_TIMEOUT_MS."""
        return self.gga_seen and time.ticks_diff(now, self.last_gga) <= LINK_TIMEOUT_MS

    def fix_age_ms(self, now):
        """ms since the last fix was read, or None if there never was one."""
        return time.ticks_diff(now, self.fix_stamp) if self.has_position else None

    def fix_valid(self, now):
        """Real fix, not older than FIX_MAX_AGE_MS -- check before using the position."""
        return self.fix_flag and self.has_position and time.ticks_diff(now, self.fix_stamp) <= FIX_MAX_AGE_MS

    def fix_good(self, now):
        """fix_valid() and it passes the satellites/HDOP gate."""
        return self.fix_valid(now) and self.good_flag

    def motion_valid(self, now):
        """sog_kn / cog_deg come from a valid RMC not older than FIX_MAX_AGE_MS."""
        return self.motion_flag and time.ticks_diff(now, self.motion_stamp) <= FIX_MAX_AGE_MS

    def describe(self, now):
        """One-line status for the console."""
        if not self.link_ok(now):
            return "gps: no data from GNSS module (recoveries: %d, checksum errors: %d)" % (
                self.recoveries, self.checksum_errors)
        if not self.fix_valid(now):
            return "gps: waiting for fix (quality=%d, sats=%d)" % (self.gga_quality, self.gga_sats)
        hdop = "-" if self.hdop is None else "%.2f" % self.hdop
        line = "gps: lat=%s lon=%s alt=%.1fm sats=%d hdop=%s age=%dms seq=%d" % (
            format_e7(self.lat_e7), format_e7(self.lon_e7), self.alt_m, self.sats, hdop,
            self.fix_age_ms(now), self.seq)
        if self.motion_valid(now):
            line += " sog=%.2fkn cog=%.1f" % (self.sog_kn, self.cog_deg)
        line += " OK" if self.good_flag else " LOW-QUALITY"
        return line

    def status_dict(self, now):
        """For telemetry. lat_e7 / lon_e7 are the exact values (degrees * 1e7)."""
        return {
            "link_ok": self.link_ok(now),
            "fix_valid": self.fix_valid(now),
            "fix_good": self.fix_good(now),
            "motion_valid": self.motion_valid(now),
            "lat_e7": self.lat_e7,
            "lon_e7": self.lon_e7,
            "alt_m": self.alt_m,
            "sats": self.sats,
            "hdop": self.hdop,
            "fix_quality": self.fix_quality,
            "sog_kn": self.sog_kn,
            "cog_deg": self.cog_deg,
            "age_ms": self.fix_age_ms(now),
            "seq": self.seq,
            "sentences": self.sentences,
            "checksum_errors": self.checksum_errors,
            "overflows": self.overflows,
            "recoveries": self.recoveries,
        }
