"""GNSS fix readout from the Heltec V4 board over I2C.

The Heltec board (electronics/heltec_gnss_reader) reads its L76K and serves the latest fix
as an I2C target at 0x6D. A plain 44-byte read returns a FixPacket v2 (layout in that
project's src/main.cpp), little-endian, CRC-8/SMBUS in the last byte. Only FIX_VALID says
the position is current; the position fields keep the last known fix after it's lost. The
Heltec board stretches SCL while it builds the packet, so the bus needs an I2C timeout that
allows for it. A board that hasn't answered with a good packet for lost_ms is treated the
same as "no fix".
"""

import struct
import time

GPS_I2C_ADDR = 0x6D
GPS_PACKET_FORMAT = "<BBIIiiiHBBHHIIHHBB"  # FixPacket v2
GPS_PACKET_LEN = 44
GPS_PACKET_VERSION = 2
GPS_LOST_MS = 1000  # no good packet for this long = treat as no fix

FLAG_LINK_OK = 0x01  # GNSS module is talking to the Heltec board
FLAG_FIX_VALID = 0x02  # real fix, <= 2.5 s old -- the one to check before using position
FLAG_FIX_GOOD = 0x04  # FIX_VALID and passes the sats/HDOP gate
FLAG_MOTION_VALID = 0x08  # sog/cog are current
FLAG_HAS_POSITION = 0x10  # position fields hold a last known fix (may be stale)


def crc8(data, length):
    """CRC-8/SMBUS: poly 0x07, init 0x00, no reflection."""
    crc = 0
    for i in range(length):
        crc ^= data[i]
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


class GpsLink:
    """Polls the Heltec board and keeps the last good fix packet."""

    def __init__(self, i2c, addr=GPS_I2C_ADDR, lost_ms=GPS_LOST_MS):
        self.i2c = i2c
        self.addr = addr
        self.lost_ms = lost_ms
        self.buf = bytearray(GPS_PACKET_LEN)
        self.stamp = 0
        self.seen = False

        # Failed reads, by cause: the bus (NACK or timeout) vs. the data that came back.
        self.i2c_errors = 0
        self.crc_errors = 0
        self.version_errors = 0

        # Fields of the last good packet; see FixPacket in the Heltec project.
        self.flags = 0
        self.seq = 0
        self.age_ms = 0  # fix age at the moment of the read
        self.lat = 0.0
        self.lon = 0.0
        self.alt_m = 0.0
        self.hdop = None  # None = the board hasn't seen HDOP yet
        self.sats = 0
        self.fix_quality = 0
        self.sog_kn = 0.0
        self.cog_deg = 0.0
        self.utc_ms = 0
        self.utc_date = 0
        self.gnss_recoveries = 0
        self.i2c_recoveries = 0
        self.reset_reason = 0

    def poll(self, now):
        """One read attempt. Returns True if a good packet arrived."""
        buf = self.buf
        try:
            self.i2c.readfrom_into(self.addr, buf)
        except OSError:
            self.i2c_errors += 1  # NACK (board resetting or not wired) or bus timeout
            return False
        if crc8(buf, GPS_PACKET_LEN - 1) != buf[-1]:
            self.crc_errors += 1
            return False
        if buf[0] != GPS_PACKET_VERSION:
            self.version_errors += 1
            return False

        (_version, self.flags, self.seq, self.age_ms, lat_e7, lon_e7, alt_cm, hdop_x100,
         self.sats, self.fix_quality, sog_x100, cog_x100, self.utc_ms, self.utc_date,
         self.gnss_recoveries, self.i2c_recoveries, self.reset_reason,
         _crc) = struct.unpack(GPS_PACKET_FORMAT, buf)
        self.lat = lat_e7 / 1e7
        self.lon = lon_e7 / 1e7
        self.alt_m = alt_cm / 100
        self.hdop = None if hdop_x100 == 0xFFFF else hdop_x100 / 100
        self.sog_kn = sog_x100 / 100
        self.cog_deg = cog_x100 / 100
        self.stamp = now
        self.seen = True
        return True

    def answering(self, now):
        return self.seen and time.ticks_diff(now, self.stamp) <= self.lost_ms

    def fix_valid(self, now):
        """Board answering and the position is current -- check before using lat/lon."""
        return self.answering(now) and bool(self.flags & FLAG_FIX_VALID)

    def fix_good(self, now):
        return self.fix_valid(now) and bool(self.flags & FLAG_FIX_GOOD)

    def motion_valid(self, now):
        return self.answering(now) and bool(self.flags & FLAG_MOTION_VALID)

    def describe(self, now):
        """One-line status for the console."""
        if not self.answering(now):
            return "gps: board not answering (i2c errors: %d, crc: %d, version: %d)" % (
                self.i2c_errors, self.crc_errors, self.version_errors)
        if not self.flags & FLAG_LINK_OK:
            return "gps: no link to GNSS module"
        if not self.flags & FLAG_FIX_VALID:
            return "gps: no fix (sats=%d)" % self.sats
        hdop = "-" if self.hdop is None else "%.2f" % self.hdop
        line = "gps: lat=%.7f lon=%.7f alt=%.1fm sats=%d hdop=%s age=%dms seq=%d" % (
            self.lat, self.lon, self.alt_m, self.sats, hdop, self.age_ms, self.seq)
        if self.flags & FLAG_MOTION_VALID:
            line += " sog=%.2fkn cog=%.1f" % (self.sog_kn, self.cog_deg)
        line += " OK" if self.flags & FLAG_FIX_GOOD else " LOW-QUALITY"
        return line
