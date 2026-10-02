// GNSS reader on an Arduino Pro Mini 3.3V / 8 MHz (ATmega328P): reads a
// Quectel L76K over the hardware UART and serves the latest fix to the main
// controller as an I2C target at 0x6D.
//
// Drop-in replacement for electronics/heltec_gnss_reader: same FixPacket v3
// (30 bytes), same address, same flag semantics, so the controller's gps.py
// works unchanged. Only resetReason means something board-specific (see
// RESET_* below). See DECISIONS.md for wiring and the reasons behind the
// choices here.
//
// Wiring (Pro Mini - L76K / bus):
//   D0 (RX)  <- L76K TX, through a 1 kOhm resistor (lets the USB-serial
//               adapter win during uploads)
//   D1 (TX)  -> L76K RX
//   D4       -> L76K RST (active low, optional)
//   A4 (SDA) -- bus SDA, A5 (SCL) -- bus SCL (the pads next to A2/A3 on
//               most Pro Minis)
//   D13         onboard LED: off = no GNSS link, blinking = link but no
//               fix, on = valid fix
//   VCC 3.3V, GND shared with the controller and the L76K.
//
// There is no debug output: the only hardware UART belongs to the L76K. The
// controller's gps.py prints everything that's in the packet.

#include <Arduino.h>
#include <Wire.h>
#include <avr/wdt.h>
#include <util/atomic.h>

#define LED_PIN 13

#define GNSS_RESET_PIN 4    // L76K RST, active low; -1 if not wired
#define GNSS_POWER_PIN -1   // optional active-low power switch (e.g. P-MOSFET) for the L76K; -1 if none
#define GNSS_UART Serial
#define GNSS_BAUD 9600

#define I2C_ADDR 0x6D

// 1 = restart the board if loop() stops returning for ~2 s. See "Watchdog"
// in DECISIONS.md: a hang with interrupts disabled falls through to a
// hardware watchdog reset, which the stock Pro Mini bootloader turns into a
// reset loop; Optiboot (e.g. MiniCore) handles it.
#define USE_WATCHDOG 1

// 1 = no L76K needed: generate a fake 1Hz fix (a random walk around
// SIM_LAT_E7/SIM_LON_E7) as NMEA sentences and feed them through the same
// parser. For testing the I2C link on the bench. The fix drops out for
// SIM_DROPOUT_MS every SIM_DROPOUT_EVERY_MS so the controller sees
// FIX_VALID go away and come back.
#define SIMULATE_GNSS 1
const int32_t SIM_LAT_E7 = 500000000L;  // 50.0 N
const int32_t SIM_LON_E7 = 300000000L;  // 30.0 E
const unsigned long SIM_DROPOUT_EVERY_MS = 60000;
const unsigned long SIM_DROPOUT_MS = 5000;

// A 1Hz fix older than this (one missed fix plus margin) is not current.
const unsigned long FIX_MAX_AGE_MS = 2500;
// No checksum-valid GGA (fix or not) for this long = GNSS link down.
const unsigned long GNSS_LINK_TIMEOUT_MS = 3000;
// ...and for this long = try to recover the module, at most this often.
const unsigned long GNSS_RECOVERY_AFTER_MS = 5000;
const unsigned long GNSS_RECOVERY_INTERVAL_MS = 10000;
// Normal I2C traffic never holds a line low this long.
const unsigned long I2C_STUCK_MS = 100;
const unsigned long I2C_RESTART_INTERVAL_MS = 1000;

const unsigned long LED_BLINK_MS = 500;

// --- I2C fix readout -------------------------------------------------------

// What an I2C controller gets from a plain read of sizeof(FixPacket) bytes
// (no register address needed). Little-endian, packed. Layout identical to
// the Heltec firmware's v3.
//
// FIX_FLAG_FIX_VALID is the one flag to check before using the position.
// The position fields always hold the last known fix (if any), but when the
// module has no fix, stops talking, or the fix is older than FIX_MAX_AGE_MS,
// FIX_VALID is clear. seq and the ageMs base only change when a new fix
// actually arrives from the module.
//
// Reading more bytes than the packet is fine: the extra bytes come back as
// 0xFF (gps.py reads 31 and finds the packet at offset 0).
#define FIX_PACKET_VERSION 3
#define FIX_FLAG_LINK_OK 0x01       // GNSS module is talking (valid GGA within GNSS_LINK_TIMEOUT_MS)
#define FIX_FLAG_FIX_VALID 0x02     // module reports a real fix, and it's <= FIX_MAX_AGE_MS old
#define FIX_FLAG_FIX_GOOD 0x04      // FIX_VALID and passes the sats/HDOP gate ("OK")
#define FIX_FLAG_MOTION_VALID 0x08  // sog/cog from a valid RMC <= FIX_MAX_AGE_MS old
#define FIX_FLAG_HAS_POSITION 0x10  // position fields hold a last known fix (may be stale — see ageMs)

// resetReason: the ATmega's MCUSR bits of the last boot, plus RESET_SOFT.
#define RESET_POWER_ON 0x01   // PORF
#define RESET_EXTERNAL 0x02   // EXTRF (reset pin, e.g. an upload)
#define RESET_BROWNOUT 0x04   // BORF
#define RESET_WATCHDOG 0x08   // WDRF (hardware watchdog reset)
#define RESET_SOFT 0x80       // restarted by the loop watchdog's interrupt (loop() hung)

struct __attribute__((packed)) FixPacket {
  uint8_t version;          // FIX_PACKET_VERSION
  uint8_t flags;            // FIX_FLAG_*
  uint16_t seq;             // +1 per new fix (wraps); unchanged = same fix as last read (resets on reboot)
  uint16_t ageMs;           // ms since that fix was parsed, at read time, capped at 65534 (65535 = none yet)
  int32_t latE7;            // degrees * 1e7
  int32_t lonE7;            // degrees * 1e7
  int32_t altCm;            // altitude above mean sea level, cm
  uint16_t hdopX100;        // HDOP * 100 (UINT16_MAX = not seen yet)
  uint8_t sats;             // satellites used
  uint8_t fixQuality;       // GGA fix quality (1 = GPS, 2 = DGPS, ...)
  uint16_t sogKnotsX100;    // speed over ground, knots * 100
  uint16_t cogDegX100;      // course over ground, degrees * 100
  uint8_t gnssRecoveries;   // times the GNSS module was reset since boot (capped at 255)
  uint8_t i2cRecoveries;    // times the I2C peripheral was restarted since boot (capped at 255)
  uint8_t resetReason;      // RESET_* bits of the last boot
  uint8_t crc;              // CRC-8/SMBUS (poly 0x07, init 0) over all bytes above
};
static_assert(sizeof(FixPacket) == 30, "FixPacket layout changed; bump FIX_PACKET_VERSION");
static_assert(sizeof(FixPacket) <= BUFFER_LENGTH, "FixPacket must fit Wire's TX buffer");

// Written from loop() inside ATOMIC_BLOCKs, read from the TWI interrupt
// (onI2CRequest). latestFix.flags holds the last-set FIX_VALID/FIX_GOOD/
// MOTION_VALID/HAS_POSITION; applyReadTimeLimits() applies the age limits
// and LINK_OK.
static FixPacket latestFix = {};
static unsigned long latestFixMillis = 0;
static unsigned long lastGgaMillis = 0;
static bool ggaSeen = false;
static unsigned long lastMotionMillis = 0;

// Latest HDOP from GSA, used to qualify GGA fixes (-1 = not seen yet).
static int32_t lastHdopX100 = -1;

static unsigned long lastGnssRecoveryMillis = 0;
static unsigned long i2cLastIdleMillis = 0;
static unsigned long i2cLastCheckMillis = 0;
static unsigned long i2cLastRestartMillis = 0;

// --- Reset reason ------------------------------------------------------------

#define SOFT_RESTART_MAGIC 0x5A3C

// .noinit survives the loop watchdog's jump to 0 (and nothing else reliably).
static uint16_t softRestartMarker __attribute__((section(".noinit")));
static uint8_t resetFlags __attribute__((section(".noinit")));

// Runs before main(), before anything is initialized. Captures why the
// board reset and turns the watchdog off: after a watchdog reset the
// hardware leaves it running with its shortest timeout (16 ms). The stock
// bootloader leaves MCUSR alone; Optiboot clears it and passes it in r2.
void captureResetReason() __attribute__((naked, used, section(".init3")));
void captureResetReason() {
  uint8_t fromBootloader;
  asm volatile("mov %0, r2" : "=r"(fromBootloader));
  uint8_t flags = MCUSR;
  MCUSR = 0;
  wdt_disable();
  if (flags == 0) {
    flags = fromBootloader;
  }
  flags &= RESET_POWER_ON | RESET_EXTERNAL | RESET_BROWNOUT | RESET_WATCHDOG;
  if (softRestartMarker == SOFT_RESTART_MAGIC) {
    flags |= RESET_SOFT;
  }
  softRestartMarker = 0;
  resetFlags = flags;
}

// --- Watchdog ------------------------------------------------------------------

// Interrupt-then-reset mode: the first timeout runs this interrupt, which
// restarts the firmware by jumping to 0 without involving the bootloader.
// If interrupts are disabled so it can't run, the second timeout does a
// hardware reset.
ISR(WDT_vect) {
  softRestartMarker = SOFT_RESTART_MAGIC;
  TWCR = 0;    // stop the TWI and UART from raising interrupts before
  UCSR0B = 0;  // their drivers are set up again
  asm volatile("jmp 0");
}

void startWatchdog() {
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    wdt_reset();
    WDTCSR = _BV(WDCE) | _BV(WDE);
    WDTCSR = _BV(WDIE) | _BV(WDE) | _BV(WDP2) | _BV(WDP1) | _BV(WDP0);  // 2 s
  }
}

void feedWatchdog() {
#if USE_WATCHDOG
  wdt_reset();
#endif
}

// --- GNSS configuration ----------------------------------------------------

// Sends a "$PCAS...*XX\r\n" command with an auto-computed checksum. `body`
// is everything between '$' and '*', e.g. "PCAS04,3". The L76K (AT6558R)
// speaks CASIC $PCAS commands, not MediaTek $PMTK, and never acknowledges
// them — see the Heltec project's DECISIONS.md.
void sendPCAS(const char *body) {
  uint8_t checksum = 0;
  for (const char *p = body; *p; p++) {
    checksum ^= (uint8_t)*p;
  }
  static const char HEX_DIGITS[] = "0123456789ABCDEF";
  GNSS_UART.write('$');
  GNSS_UART.print(body);
  GNSS_UART.write('*');
  GNSS_UART.write(HEX_DIGITS[checksum >> 4]);
  GNSS_UART.write(HEX_DIGITS[checksum & 0x0F]);
  GNSS_UART.write("\r\n");
}

// Applies GNSS receiver settings. Every value is sent explicitly, even
// where it matches the factory default, because the chip persists
// PCAS-set values across resets.
void configureGNSS() {
  delay(100);  // let the UART line settle before the module sees traffic
  sendPCAS("PCAS04,3");  // GPS + BeiDou
  delay(100);
  sendPCAS("PCAS02,1000");  // 1Hz fix rate
  delay(100);
  // GGA/GSA/RMC every fix; GLL/GSV/VTG/ZDA/ANT off (unused by readGNSS()).
  sendPCAS("PCAS03,1,0,1,0,1,0,0,0,0,0,,,0,0");
  GNSS_UART.flush();
}

// Brings back a GNSS link that has gone silent: power-cycles the module if
// a power switch is wired, pulses its reset line, forces it back to 9600
// baud in case a past session left it at 115200 (PCAS01 persists too),
// then re-applies config. Blocks ~0.9 s; the I2C interrupt keeps answering
// meanwhile, with LINK_OK and FIX_VALID clear.
void recoverGNSS() {
  GNSS_UART.end();  // don't drive TX into an unpowered module
#if GNSS_POWER_PIN >= 0
  digitalWrite(GNSS_POWER_PIN, HIGH);  // off
  delay(500);
  feedWatchdog();
  digitalWrite(GNSS_POWER_PIN, LOW);  // on
#endif
#if GNSS_RESET_PIN >= 0
  digitalWrite(GNSS_RESET_PIN, LOW);
  delay(100);
  digitalWrite(GNSS_RESET_PIN, HIGH);
#endif
  delay(100);

  // 115200 is 3.5% off at 8 MHz; good enough for a one-off rescue attempt.
  GNSS_UART.begin(115200);
  sendPCAS("PCAS01,1");  // 1 = 9600; just noise to a module already at 9600
  GNSS_UART.flush();
  delay(50);
  GNSS_UART.end();
  GNSS_UART.begin(GNSS_BAUD);
  configureGNSS();
}

void checkGNSSLink() {
  unsigned long now = millis();
  unsigned long silentMs = now - lastGgaMillis;  // only loop() writes it
  if (silentMs < GNSS_RECOVERY_AFTER_MS || now - lastGnssRecoveryMillis < GNSS_RECOVERY_INTERVAL_MS) {
    return;
  }
  recoverGNSS();
  lastGnssRecoveryMillis = millis();
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    if (latestFix.gnssRecoveries < UINT8_MAX) {
      latestFix.gnssRecoveries++;
    }
  }
}

// --- NMEA parsing --------------------------------------------------------
//
// No floats: double is 32 bits on AVR, about a metre of rounding error on a
// latitude. Everything is parsed straight into fixed-point integers.

// Parses a decimal like "-12.345" into value * 10^decimals (rounded).
// False for empty or malformed fields.
bool parseFixed(const char *s, uint8_t decimals, int32_t *out) {
  bool negative = *s == '-';
  if (negative) {
    s++;
  }
  int32_t value = 0;
  uint8_t digits = 0;
  uint8_t fraction = 0;
  bool seenPoint = false;
  for (; *s; s++) {
    if (*s == '.' && !seenPoint) {
      seenPoint = true;
      continue;
    }
    if (*s < '0' || *s > '9') {
      return false;
    }
    uint8_t d = *s - '0';
    if (seenPoint && fraction >= decimals) {
      if (fraction == decimals && d >= 5) {
        value++;
      }
      fraction = decimals + 1;  // round on the first extra digit only
      continue;
    }
    if (++digits > 9) {
      return false;  // wouldn't fit; no field we use gets near this
    }
    value = value * 10 + d;
    if (seenPoint) {
      fraction++;
    }
  }
  if (digits == 0) {
    return false;
  }
  for (; fraction < decimals; fraction++) {
    value *= 10;
  }
  *out = negative ? -value : value;
  return true;
}

// Converts NMEA "ddmm.mmmm" / "dddmm.mmmm" + hemisphere into degrees * 1e7.
bool parseCoordE7(const char *s, char hemisphere, int32_t *out) {
  int32_t whole = 0;  // ddmm / dddmm
  uint8_t digits = 0;
  for (; *s >= '0' && *s <= '9'; s++) {
    if (++digits > 5) {
      return false;
    }
    whole = whole * 10 + (*s - '0');
  }
  if (digits < 3) {
    return false;
  }
  int32_t degrees = whole / 100;
  int32_t minutes = whole % 100;
  if (degrees > 180 || minutes >= 60) {
    return false;
  }
  int32_t minutesE7 = minutes * 10000000L;
  if (*s == '.') {
    s++;
    int32_t scale = 1000000L;  // digits beyond the 7th are below 1e-7 minutes
    for (; *s >= '0' && *s <= '9'; s++) {
      minutesE7 += (*s - '0') * scale;
      scale /= 10;
    }
  }
  if (*s != '\0') {
    return false;
  }
  int32_t value = degrees * 10000000L + (minutesE7 + 30) / 60;
  *out = (hemisphere == 'S' || hemisphere == 'W') ? -value : value;
  return true;
}

int32_t parseIntOr(const char *s, int32_t fallback) {
  int32_t value;
  return parseFixed(s, 0, &value) ? value : fallback;
}

// Splits an NMEA sentence (already cut at '*') on ',' in place; returns the
// number of fields found (the last one keeps any fields beyond maxFields).
uint8_t splitNmeaFields(char *sentence, char *fields[], uint8_t maxFields) {
  uint8_t count = 0;
  fields[count++] = sentence;
  for (char *p = sentence; *p && count < maxFields; p++) {
    if (*p == ',') {
      *p = '\0';
      fields[count++] = p + 1;
    }
  }
  return count;
}

// GGA ($GNGGA/$GPGGA): position, fix quality, satellites, altitude.
void handleGGA(char *fields[], uint8_t count) {
  if (count < 10) {
    return;
  }
  int32_t fixQuality = parseIntOr(fields[6], 0);
  int32_t satellites = parseIntOr(fields[7], 0);

  // Qualities 1-5 are real fixes; 6 (dead-reckoning estimate), 7 (manual)
  // and 8 (simulation) are not something to navigate on.
  int32_t lat = 0;
  int32_t lon = 0;
  bool hasFix = fixQuality >= 1 && fixQuality <= 5 &&
                parseCoordE7(fields[2], fields[3][0], &lat) &&
                parseCoordE7(fields[4], fields[5][0], &lon) &&
                labs(lat) <= 900000000L && labs(lon) <= 1800000000L;
  int32_t altCm = 0;
  parseFixed(fields[9], 2, &altCm);

  // A fix is only as good as its geometry: HDOP <= 2.5 is the usual "good"
  // rule of thumb (not seen yet = don't hold it against the fix).
  bool goodFix = hasFix && satellites >= 4 && lastHdopX100 <= 250;

  unsigned long now = millis();
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    lastGgaMillis = now;  // any valid GGA proves the link is alive, fix or not
    ggaSeen = true;
    if (hasFix) {
      latestFix.seq++;
      latestFix.flags |= FIX_FLAG_HAS_POSITION | FIX_FLAG_FIX_VALID;
      if (goodFix) {
        latestFix.flags |= FIX_FLAG_FIX_GOOD;
      } else {
        latestFix.flags &= ~FIX_FLAG_FIX_GOOD;
      }
      latestFix.latE7 = lat;
      latestFix.lonE7 = lon;
      latestFix.altCm = altCm;
      latestFix.hdopX100 = lastHdopX100 < 0 ? UINT16_MAX : (uint16_t)min(lastHdopX100, 65534L);
      latestFix.sats = (uint8_t)constrain(satellites, 0L, 255L);
      latestFix.fixQuality = (uint8_t)fixQuality;
      latestFixMillis = now;
    } else {
      // Nothing new was measured: leave seq/timestamps/position alone and
      // just stop vouching for the old fix.
      latestFix.flags &= ~(FIX_FLAG_FIX_VALID | FIX_FLAG_FIX_GOOD);
    }
  }
}

// GSA ($GNGSA/$GPGSA/$BDGSA/...): stashes HDOP.
void handleGSA(char *fields[], uint8_t count) {
  if (count < 17) {
    return;
  }
  int32_t hdop;
  if (parseFixed(fields[16], 2, &hdop) && hdop > 0) {
    lastHdopX100 = hdop;
  }
}

// RMC ($GNRMC/$GPRMC/...): speed/course over ground, only when the sentence
// reports itself valid.
void handleRMC(char *fields[], uint8_t count) {
  if (count < 9) {
    return;
  }
  if (strcmp(fields[2], "A") != 0) {
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
      latestFix.flags &= ~FIX_FLAG_MOTION_VALID;
    }
    return;
  }
  int32_t sog = 0;
  int32_t cog = 0;
  parseFixed(fields[7], 2, &sog);  // empty when not moving: stays 0
  parseFixed(fields[8], 2, &cog);

  unsigned long now = millis();
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    latestFix.flags |= FIX_FLAG_MOTION_VALID;
    latestFix.sogKnotsX100 = (uint16_t)constrain(sog, 0L, 65535L);
    latestFix.cogDegX100 = (uint16_t)constrain(cog, 0L, 35999L);
    lastMotionMillis = now;
  }
}

int8_t hexValue(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}

// Checks the "*XX" checksum of a "$..." sentence, cuts it off at '*' and
// dispatches it.
void handleSentence(char *line) {
  char *star = strrchr(line, '*');
  if (line[0] != '$' || star == nullptr || star - line < 6) {
    return;
  }
  int8_t hi = hexValue(star[1]);
  int8_t lo = hexValue(star[2]);
  if (hi < 0 || lo < 0) {
    return;
  }
  uint8_t checksum = 0;
  for (char *p = line + 1; p < star; p++) {
    checksum ^= (uint8_t)*p;
  }
  if (checksum != (uint8_t)((hi << 4) | lo)) {
    return;
  }
  *star = '\0';

  char *fields[18];
  const char *type = line + 3;  // e.g. "$GNGGA" -> "GGA"
  if (strncmp(type, "GGA", 3) == 0) {
    handleGGA(fields, splitNmeaFields(line, fields, 15));
  } else if (strncmp(type, "GSA", 3) == 0) {
    handleGSA(fields, splitNmeaFields(line, fields, 18));
  } else if (strncmp(type, "RMC", 3) == 0) {
    handleRMC(fields, splitNmeaFields(line, fields, 13));
  }
}

// Pumps bytes from the GNSS UART and dispatches complete sentences.
void readGNSS() {
  static char line[100];  // NMEA sentences are at most 82 characters
  static uint8_t length = 0;
  static bool overflow = false;
  while (GNSS_UART.available()) {
    char c = (char)GNSS_UART.read();
    if (c == '$') {
      length = 0;  // resync: a '$' always starts a new sentence
      overflow = false;
    }
    if (c == '\n') {
      if (!overflow) {
        line[length] = '\0';
        handleSentence(line);
      }
      length = 0;
      overflow = false;
    } else if (c != '\r') {
      if (length < sizeof(line) - 1) {
        line[length++] = c;
      } else {
        overflow = true;  // corrupt/unterminated line; drop it
      }
    }
  }
}

// --- Simulated GNSS ------------------------------------------------------

#if SIMULATE_GNSS

// Adds "*XX" to `body` ("GNGGA,..." without '$') and dispatches it as if the
// module had sent it.
void feedSentence(const char *body) {
  char line[100];
  uint8_t checksum = 0;
  for (const char *p = body; *p; p++) {
    checksum ^= (uint8_t)*p;
  }
  snprintf(line, sizeof(line), "$%s*%02X", body, checksum);
  handleSentence(line);
}

// Formats degrees * 1e7 as NMEA "ddmm.mmmmm" (degreeDigits = 2) or
// "dddmm.mmmmm" (3).
void formatCoord(char *out, size_t size, int32_t valueE7, uint8_t degreeDigits) {
  int32_t v = labs(valueE7);
  int32_t degrees = v / 10000000L;
  int32_t minutesE5 = (v % 10000000L) * 6 / 10;  // * 60 / 100, without overflow
  snprintf(out, size, degreeDigits == 3 ? "%03ld%02ld.%05ld" : "%02ld%02ld.%05ld",
           (long)degrees, (long)(minutesE5 / 100000L), (long)(minutesE5 % 100000L));
}

void simulateGNSS() {
  static unsigned long lastMillis = 0;
  static int32_t latE7 = SIM_LAT_E7;
  static int32_t lonE7 = SIM_LON_E7;
  unsigned long now = millis();
  if (now - lastMillis < 1000) {
    return;
  }
  lastMillis = now;

  latE7 += random(-50, 51);  // up to ~0.5 m per second
  lonE7 += random(-50, 51);
  int sats = random(5, 13);
  int hdopX100 = random(70, 301);  // sometimes above 2.5: FIX_GOOD clears
  long altCm = 12000 + random(-200, 201);
  int sogX100 = random(0, 401);
  long cogX100 = random(0, 36000);
  bool dropout = now % SIM_DROPOUT_EVERY_MS < SIM_DROPOUT_MS;

  char body[90];
  char lat[16];
  char lon[16];
  formatCoord(lat, sizeof(lat), latE7, 2);
  formatCoord(lon, sizeof(lon), lonE7, 3);

  snprintf(body, sizeof(body), "GNGSA,A,3,,,,,,,,,,,,,1.50,%d.%02d,1.20", hdopX100 / 100, hdopX100 % 100);
  feedSentence(body);
  if (dropout) {
    feedSentence("GNGGA,120000.000,,,,,0,00,,,M,,M,,");
    feedSentence("GNRMC,120000.000,V,,,,,,,010126,,,N");
    return;
  }
  snprintf(body, sizeof(body), "GNGGA,120000.000,%s,%c,%s,%c,1,%02d,%d.%02d,%ld.%02ld,M,0.0,M,,",
           lat, latE7 < 0 ? 'S' : 'N', lon, lonE7 < 0 ? 'W' : 'E', sats,
           hdopX100 / 100, hdopX100 % 100, altCm / 100, altCm % 100);
  feedSentence(body);
  snprintf(body, sizeof(body), "GNRMC,120000.000,A,%s,%c,%s,%c,%d.%02d,%ld.%02ld,010126,,,A",
           lat, latE7 < 0 ? 'S' : 'N', lon, lonE7 < 0 ? 'W' : 'E',
           sogX100 / 100, sogX100 % 100, cogX100 / 100, cogX100 % 100);
  feedSentence(body);
}

#endif

// --- I2C peripheral ------------------------------------------------------

// CRC-8/SMBUS (poly 0x07, init 0x00, no reflection), a nibble at a time:
// it runs inside the TWI interrupt while SCL is stretched, so it's kept
// short. crc8Nibble[n] is the CRC register after shifting n << 4 through
// four rounds.
static uint8_t crc8Nibble[16];

void initCrc8() {
  for (uint8_t n = 0; n < 16; n++) {
    uint8_t crc = n << 4;
    for (uint8_t bit = 0; bit < 4; bit++) {
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x07) : (uint8_t)(crc << 1);
    }
    crc8Nibble[n] = crc;
  }
}

uint8_t crc8(const uint8_t *data, uint8_t len) {
  uint8_t crc = 0;
  for (uint8_t i = 0; i < len; i++) {
    crc ^= data[i];
    crc = (uint8_t)(crc << 4) ^ crc8Nibble[crc >> 4];
    crc = (uint8_t)(crc << 4) ^ crc8Nibble[crc >> 4];
  }
  return crc;
}

// Copies the shared state into `packet` with ages and validity as of `now`.
// Call with interrupts disabled (or from the TWI interrupt).
void snapshotFix(FixPacket &packet, unsigned long now) {
  packet = latestFix;
  packet.version = FIX_PACKET_VERSION;
  bool hasPosition = packet.flags & FIX_FLAG_HAS_POSITION;
  unsigned long ageMs = now - latestFixMillis;
  packet.ageMs = !hasPosition ? UINT16_MAX : (ageMs > 65534UL ? 65534U : (uint16_t)ageMs);
  if (ggaSeen && now - lastGgaMillis <= GNSS_LINK_TIMEOUT_MS) {
    packet.flags |= FIX_FLAG_LINK_OK;
  } else {
    packet.flags &= ~FIX_FLAG_LINK_OK;
  }
  if (!hasPosition || ageMs > FIX_MAX_AGE_MS) {
    packet.flags &= ~(FIX_FLAG_FIX_VALID | FIX_FLAG_FIX_GOOD);
  }
  if (now - lastMotionMillis > FIX_MAX_AGE_MS) {
    packet.flags &= ~FIX_FLAG_MOTION_VALID;
  }
}

// Runs in the TWI interrupt when the controller reads from us (the TWI
// hardware stretches SCL meanwhile), so ages and validity are per read.
void onI2CRequest() {
  FixPacket packet;
  snapshotFix(packet, millis());
  packet.crc = crc8((const uint8_t *)&packet, sizeof(packet) - 1);
  Wire.write((const uint8_t *)&packet, sizeof(packet));
}

void startI2C() {
  Wire.begin((uint8_t)I2C_ADDR);  // also enables the internal pull-ups (to 3.3V on this board)
}

// A target can wedge the bus by holding SDA (or SCL, while stretching) low
// when a transfer is cut short, e.g. the controller resets mid-read.
// Normal traffic never holds a line low for I2C_STUCK_MS, so if that's
// seen, restart our TWI to let go of the lines. If it was the controller
// holding the line, restarting ours is harmless.
void checkI2CBus() {
  unsigned long now = millis();
  const uint8_t lines = _BV(PC4) | _BV(PC5);  // A4 = SDA, A5 = SCL
  bool idle = (PINC & lines) == lines;
  // If loop() was busy (e.g. GNSS recovery), we can't claim a line stayed
  // low the whole time, so start the measurement over.
  bool continuous = now - i2cLastCheckMillis <= 10;
  i2cLastCheckMillis = now;
  if (idle || !continuous) {
    i2cLastIdleMillis = now;
    return;
  }
  if (now - i2cLastIdleMillis < I2C_STUCK_MS || now - i2cLastRestartMillis < I2C_RESTART_INTERVAL_MS) {
    return;
  }
  Wire.end();
  startI2C();
  i2cLastRestartMillis = millis();
  i2cLastIdleMillis = i2cLastRestartMillis;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    if (latestFix.i2cRecoveries < UINT8_MAX) {
      latestFix.i2cRecoveries++;
    }
  }
}

// --- LED ---------------------------------------------------------------

void updateLed() {
  static unsigned long lastToggleMillis = 0;
  static bool blinkOn = false;
  unsigned long now = millis();
  FixPacket packet;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    snapshotFix(packet, now);
  }
  if (now - lastToggleMillis >= LED_BLINK_MS) {
    lastToggleMillis = now;
    blinkOn = !blinkOn;
  }
  bool on;
  if (packet.flags & FIX_FLAG_FIX_VALID) {
    on = true;
  } else if (packet.flags & FIX_FLAG_LINK_OK) {
    on = blinkOn;
  } else {
    on = false;
  }
  digitalWrite(LED_PIN, on ? HIGH : LOW);
}

// --- Setup / loop --------------------------------------------------------

void setup() {
  // I2C comes up first, before any waiting, so the controller gets answers
  // (with FIX_VALID clear) as soon as possible after a reset.
  initCrc8();
  latestFix.hdopX100 = UINT16_MAX;
  latestFix.resetReason = resetFlags;
  Wire.onRequest(onI2CRequest);
  startI2C();

  pinMode(LED_PIN, OUTPUT);
#if GNSS_POWER_PIN >= 0
  pinMode(GNSS_POWER_PIN, OUTPUT);
  digitalWrite(GNSS_POWER_PIN, LOW);  // on
#endif
#if GNSS_RESET_PIN >= 0
  pinMode(GNSS_RESET_PIN, OUTPUT);
  digitalWrite(GNSS_RESET_PIN, HIGH);  // release reset
#endif

#if SIMULATE_GNSS
  randomSeed(analogRead(A0));  // floating pin: different walk per boot
#else
  delay(100);  // let the GNSS module's power rail settle before talking to it
  GNSS_UART.begin(GNSS_BAUD);
  configureGNSS();
  lastGnssRecoveryMillis = millis();  // give the module a full window before first recovery
#endif

#if USE_WATCHDOG
  startWatchdog();
#endif
}

void loop() {
  feedWatchdog();
#if SIMULATE_GNSS
  simulateGNSS();
#else
  readGNSS();
  checkGNSSLink();
#endif
  checkI2CBus();
  updateLed();
}
