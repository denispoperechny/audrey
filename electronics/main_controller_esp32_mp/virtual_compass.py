"""Virtual compass: the heading the navigation steers by, built from the gyro, the magnetic
compass and the GPS track (the plan in index.md, "Post-Mission 4 Compass correction").

Mission 4 showed the magnetic heading off by up to about 70 degrees, depending on the
direction, while the chip's gyro (the game rotation vector, no magnetometer) follows turns
accurately but has no idea where north is and drifts slowly. So:

  - The gyro is the main input. The virtual heading is the gyro yaw plus an offset, so every
    turn the gyro measures goes straight into the heading.
  - The starting offset comes from the magnetic compass: the first heading is its reading.
  - The magnetic compass then pulls the offset towards itself by MAG_GAIN_PER_S (0.5 %) of the
    difference per second: the only long-term reference while the boat stands still.
  - The GPS track pulls it by GPS_GAIN_PER_S (5 %) per second, but only while the boat is
    moving forward in a straight line at MIN_SPEED_M_S or more, and the gyro agrees. With
    fixes n-3, n-2, n-1, n (the latest, about a second apart), once a second:
        course   the direction n-2 -> n
        speed    distance n-2 -> n, over the time between them
        straight the directions n-1 -> n and n-3 -> n are both within
                 STRAIGHT_TOLERANCE_DEG of the course
        gyro     the GPS course changed by about as much as the gyro turned: from
                 n-3 -> n-1 to n-2 -> n (a second later) against the gyro's turn over the same
                 second, within GYRO_AGREEMENT_DEG
    The straight test over four fixes and the gyro check are there because a GPS position
    that drifts (a boat held still in someone's hands, poor sky view) can look like a metre a
    second in a fairly straight line over two fixes; it rarely stays lined up over three legs,
    and its "course" swings about while the gyro says the boat didn't turn.
    "Forward" is the caller's to judge (the controller uses the throttle output); a boat
    going backwards travels opposite to its heading.
    The course describes the boat between fixes n-2 and n, so it is compared with the heading
    at the middle of that stretch (fix n-1), not now: the gyro yaw then plus the offset now.
    The gyro has carried any turn since then into the heading already, so only the offset is
    corrected, and a correction made in between isn't counted twice.
  - A jump in the gyro yaw faster than MAX_GYRO_RATE_DEG_S can't be a turn: the chip has reset
    its yaw (its zero moved). The virtual compass then starts over from the magnetic heading.

Headings are in the compass's frame: degrees clockwise from magnetic north. The GPS course
is true north, so the declination (gnc.MAGNETIC_DECLINATION_DEG) is taken off it first.

Pure Python apart from the clock, so it runs on the desktop too: see test_virtual_compass.py.
"""

import math

import gnc

try:
    from time import ticks_diff
except ImportError:  # desktop: plain millisecond counters
    def ticks_diff(a, b):
        return a - b

MAG_GAIN_PER_S = 0.005
GPS_GAIN_PER_S = 0.05
MIN_SPEED_M_S = 0.5
STRAIGHT_TOLERANCE_DEG = 10
GYRO_AGREEMENT_DEG = 20  # GPS course change vs gyro turn; 10 rejected too many good fixes (GPS jitter)
MAX_STEP_S = 2.0  # a longer gap (no gyro, a lost fix) counts as this much, so a correction can't jump
MAX_FIX_SPAN_MS = 4500  # fixes n-3 .. n further apart than this (a fix was lost) aren't used
HISTORY_MS = 5000  # how much of the recent past is kept, to look up the heading and gyro at a fix's time
HISTORY_MATCH_MS = 300  # a history entry further than this from the time asked for doesn't count
MAX_GYRO_RATE_DEG_S = 360  # the boat can't turn this fast; a faster change means the gyro yaw jumped
MIN_GYRO_STEP_S = 0.05  # (shortest interval the rate is judged over, so tiny gaps don't inflate it)


def wrap180(a):
    return (a + 180) % 360 - 180


class VirtualCompass:
    def __init__(self):
        # Counters for telemetry, kept across resets:
        self.gps_corrections = 0
        self.gyro_jumps = 0  # times the gyro yaw jumped and the compass started over
        self.reset()

    def reset(self):
        """Forget the heading and everything tied to the gyro's zero: needed whenever that zero
        changes (the compass chip was re-initialised). The counters are kept."""
        self.offset = None  # virtual heading = gyro yaw + offset
        self.heading = None  # degrees, 0..360; None until the first gyro and magnetic readings
        self.stamp = None  # ticks of the gyro reading the heading was last updated from
        self._last_mag = None  # ticks of the last magnetic correction
        self._last_gps = None  # ticks of the last GPS correction
        self._history = []  # (ticks, heading, gyro yaw), oldest first
        self._fixes = []  # (seq, ticks, lat_e7, lon_e7) of the last four GPS fixes, oldest first
        # What the GPS side last made of the track, for telemetry:
        self.gps_speed = None  # m/s, n-2 -> n
        self.gps_course = None  # degrees magnetic, n-2 -> n
        self.straight = False
        self.gps_turn = None  # how far the GPS course turned in the last second, degrees
        self.gyro_turn = None  # how far the gyro turned over the same second, degrees
        self.gyro_agrees = False
        self.gps_used = False  # the last fix corrected the heading
        self.last_gps_error = None  # GPS course - virtual heading at that time, degrees
        self.last_mag_error = None  # magnetic heading - virtual heading, degrees
        self._last_gyro = None  # (ticks, yaw) of the previous gyro reading, to spot a jump

    def update(self, now, gyro_yaw, gyro_ms, mag_heading, mag_ms, fix, forward):
        """Call often (every compass read).

        gyro_yaw, gyro_ms      game rotation vector yaw (degrees, increasing clockwise, any
                               zero) and when it was read; gyro_yaw None if not available
        mag_heading, mag_ms    magnetic heading (degrees) and when it was read, or None
        fix                    (seq, ticks, lat_e7, lon_e7) of the latest valid GPS fix, or None
        forward                the boat is being driven forward
        """
        if gyro_yaw is None:
            return
        if self._last_gyro is not None and gyro_ms != self._last_gyro[0]:
            seconds = max(MIN_GYRO_STEP_S, abs(ticks_diff(gyro_ms, self._last_gyro[0])) / 1000)
            if abs(wrap180(gyro_yaw - self._last_gyro[1])) / seconds > MAX_GYRO_RATE_DEG_S:
                # Not a turn: the gyro's zero moved. Everything tied to the old zero is void.
                self.reset()
                self.gyro_jumps += 1
        self._last_gyro = (gyro_ms, gyro_yaw)
        if self.offset is None:
            if mag_heading is None:
                return  # nothing to start from yet
            self.offset = (mag_heading - gyro_yaw) % 360
            self._last_mag = mag_ms
        heading = (gyro_yaw + self.offset) % 360

        # Magnetic: a slow pull, once per new magnetic reading.
        if mag_heading is not None and mag_ms != self._last_mag:
            dt = self._step_s(mag_ms, self._last_mag)
            self.last_mag_error = wrap180(mag_heading - heading)
            self.offset = (self.offset + MAG_GAIN_PER_S * dt * self.last_mag_error) % 360
            self._last_mag = mag_ms
            heading = (gyro_yaw + self.offset) % 360

        # GPS: once per new fix.
        if fix is not None and (not self._fixes or fix[0] != self._fixes[-1][0]):
            self._fixes = (self._fixes + [fix])[-4:]
            self.gps_used = False
            span = ticks_diff(fix[1], self._fixes[0][1]) if len(self._fixes) == 4 else None
            if span is not None and span > MAX_FIX_SPAN_MS:
                # A gap in the fixes: the boat may have turned in it, so they say nothing about
                # the course. Wait for four consecutive fixes again.
                self.straight = False
                self.gyro_agrees = False
            elif span is not None:
                self._gps_track()
                gyro_then = self._value_at(self._fixes[2][1], 2)
                if (forward and self.straight and self.gyro_agrees and gyro_then is not None
                        and self.gps_speed >= MIN_SPEED_M_S):
                    then = (gyro_then + self.offset) % 360  # the heading at fix n-1, with today's offset
                    dt = self._step_s(fix[1], self._last_gps) if self._last_gps is not None else 1.0
                    self.last_gps_error = wrap180(self.gps_course - then)
                    self.offset = (self.offset + GPS_GAIN_PER_S * dt * self.last_gps_error) % 360
                    self._last_gps = fix[1]
                    self.gps_used = True
                    self.gps_corrections += 1
                    heading = (gyro_yaw + self.offset) % 360

        self.heading = heading
        self.stamp = gyro_ms
        self._history.append((gyro_ms, heading, gyro_yaw))
        while self._history and ticks_diff(gyro_ms, self._history[0][0]) > HISTORY_MS:
            self._history.pop(0)

    def _gps_track(self):
        """Speed, course, straightness and the gyro check from the last four fixes."""
        f0, f1, f2, f3 = self._fixes  # n-3, n-2, n-1, n

        def direction(a, b):  # degrees magnetic from fix a to fix b
            return (gnc.course_deg(*gnc.offset_m(a[2], a[3], b[2], b[3])) - gnc.MAGNETIC_DECLINATION_DEG) % 360

        east, north = gnc.offset_m(f1[2], f1[3], f3[2], f3[3])
        seconds = ticks_diff(f3[1], f1[1]) / 1000
        self.gps_speed = math.sqrt(east * east + north * north) / seconds if seconds > 0 else 0.0
        self.gps_course = (gnc.course_deg(east, north) - gnc.MAGNETIC_DECLINATION_DEG) % 360
        self.straight = (abs(wrap180(direction(f2, f3) - self.gps_course)) <= STRAIGHT_TOLERANCE_DEG
                         and abs(wrap180(direction(f0, f3) - self.gps_course)) <= STRAIGHT_TOLERANCE_DEG)

        # The course n-3 -> n-1 describes the boat around fix n-2, the course n-2 -> n around
        # fix n-1: compare how far it turned in between with the gyro over the same second.
        self.gps_turn = wrap180(self.gps_course - direction(f0, f2))
        gyro_then, gyro_now = self._value_at(f1[1], 2), self._value_at(f2[1], 2)
        if gyro_then is None or gyro_now is None:
            self.gyro_turn = None
            self.gyro_agrees = False
        else:
            self.gyro_turn = wrap180(gyro_now - gyro_then)
            self.gyro_agrees = abs(wrap180(self.gps_turn - self.gyro_turn)) <= GYRO_AGREEMENT_DEG

    def _value_at(self, ticks, index):
        """From the recent history, the entry closest in time to `ticks`: its heading (index 1)
        or gyro yaw (index 2). None if there is none within HISTORY_MATCH_MS."""
        best = None
        for entry in self._history:
            if best is None or abs(ticks_diff(entry[0], ticks)) < abs(ticks_diff(best[0], ticks)):
                best = entry
        if best is None or abs(ticks_diff(best[0], ticks)) > HISTORY_MATCH_MS:
            return None
        return best[index]

    @staticmethod
    def _step_s(now_ms, last_ms):
        if last_ms is None:
            return 0.0
        return max(0.0, min(MAX_STEP_S, ticks_diff(now_ms, last_ms) / 1000))

    def status_dict(self):
        """For telemetry."""
        return {
            "heading": None if self.heading is None else round(self.heading, 1),
            "offset": None if self.offset is None else round(self.offset, 1),
            "gps_course": None if self.gps_course is None else round(self.gps_course, 1),
            "gps_speed": None if self.gps_speed is None else round(self.gps_speed, 2),
            "straight": self.straight,
            "gps_turn": None if self.gps_turn is None else round(self.gps_turn, 1),
            "gyro_turn": None if self.gyro_turn is None else round(self.gyro_turn, 1),
            "gyro_agrees": self.gyro_agrees,
            "gps_used": self.gps_used,
            "gps_corrections": self.gps_corrections,
            "gyro_jumps": self.gyro_jumps,
            "last_gps_error": None if self.last_gps_error is None else round(self.last_gps_error, 1),
            "last_mag_error": None if self.last_mag_error is None else round(self.last_mag_error, 1),
        }
