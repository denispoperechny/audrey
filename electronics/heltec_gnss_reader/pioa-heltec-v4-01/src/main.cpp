#include <Arduino.h>
#include <Wire.h>
#include <driver/gpio.h>
#include <esp_system.h>

// Heltec WiFi LoRa 32 V4 (ESP32-S3) has two onboard LEDs:
//  - GPIO35: plain white LED (digitalWrite) — this is the one this
//    sketch blinks.
//  - GPIO38: addressable WS2812 RGB LED (rgbLedWrite) — not used here
//    (it also happens to double as the L76K GNSS RX line, see below).
//
// GNSS module: Quectel L76K, wired per src/gnss_info.txt (confirmed
// against Heltec's own V4 datasheet/pinmap):
//   GPIO34 - Ctrl_3v3 (power switch for the GNSS rail)
//   GPIO39 - TX (module's TX -> ESP32 RX)
//   GPIO38 - RX (module's RX -> ESP32 TX)
//   GPIO40 - WAKE
//   GPIO41 - PPS
//   GPIO42 - RST
//
// See DECISIONS.md for the reasoning behind this file's choices, and
// GNSS_DEBUG.md for how to talk to the module directly if it ever needs
// debugging again (backups/main_pass_through_uart_proxy.cpp.bak has the
// raw-passthrough build used for that).
//
// I2C peripheral: the latest fix is also served to an external I2C
// controller (this board is the target, address 0x6D) on the header's
// TX/RX pins — see "I2C fix readout" below for the packet layout.
//
// Resiliency (this runs on an autonomous bot): a loop watchdog, GNSS
// link recovery, I2C stuck-bus recovery, non-blocking USB serial, and
// fix validity decided at read time. See "Resiliency" in DECISIONS.md.

#define LED_PIN 35

#define GNSS_CTRL_PIN 34
#define GNSS_RX_PIN 39  // ESP32 RX <- L76K TX
#define GNSS_TX_PIN 38  // ESP32 TX -> L76K RX
#define GNSS_WAKE_PIN 40
#define GNSS_PPS_PIN 41
#define GNSS_RESET_PIN 42

#define GNSS_UART Serial1
#define GNSS_BAUD 9600

// GPIO43/44 are UART0 TX/RX on the header. They're free because Serial
// is USB-CDC on this board (ARDUINO_USB_CDC_ON_BOOT=1). SCL goes on 43
// (the ROM's boot-log TX pin) rather than SDA: boot-log glitches on SCL
// alone can't form an I2C START/STOP, glitches on SDA could.
#define I2C_SCL_PIN 43
#define I2C_SDA_PIN 44
#define I2C_ADDR 0x6D
#define I2C_FREQ 400000

const unsigned long BLINK_INTERVAL_MS = 3000;

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

static unsigned long lastBlinkTime = 0;
static bool ledOn = false;

static String nmeaLine;

// Latest quality/motion info from GSA/RMC, used to qualify GGA fixes.
static double lastHdop = -1.0;  // horizontal dilution of precision (-1 = not seen yet)
static double lastSpeedKnots = 0.0;
static double lastCourseDeg = 0.0;

static unsigned long lastGnssRecoveryMillis = 0;
static unsigned long i2cLastIdleMillis = 0;
static unsigned long i2cLastCheckMillis = 0;
static unsigned long i2cLastRestartMillis = 0;

// --- I2C fix readout -------------------------------------------------------

// What an I2C controller gets from a plain read of sizeof(FixPacket) bytes
// (no register address needed). Little-endian, packed.
//
// FIX_FLAG_FIX_VALID is the one flag to check before using the position.
// The position fields always hold the last known fix (if any) so a
// controller can still see where it last was, but when the module has no
// fix, stops talking, or the fix is older than FIX_MAX_AGE_MS, FIX_VALID
// is clear. Nothing (seq, ageMs base, utcTimeMs) changes without a new
// fix actually arriving from the module.
#define FIX_PACKET_VERSION 2
#define FIX_FLAG_LINK_OK 0x01       // GNSS module is talking (valid GGA within GNSS_LINK_TIMEOUT_MS)
#define FIX_FLAG_FIX_VALID 0x02     // module reports a real fix, and it's <= FIX_MAX_AGE_MS old
#define FIX_FLAG_FIX_GOOD 0x04      // FIX_VALID and passes the sats/HDOP gate ("OK")
#define FIX_FLAG_MOTION_VALID 0x08  // sog/cog from a valid RMC <= FIX_MAX_AGE_MS old
#define FIX_FLAG_HAS_POSITION 0x10  // position fields hold a last known fix (may be stale — see ageMs)

struct __attribute__((packed)) FixPacket {
  uint8_t version;          // FIX_PACKET_VERSION
  uint8_t flags;            // FIX_FLAG_*
  uint32_t seq;             // +1 per new fix; unchanged = same fix as last read (resets on reboot)
  uint32_t ageMs;           // ms since that fix was parsed, at read time (UINT32_MAX = none yet)
  int32_t latE7;            // degrees * 1e7
  int32_t lonE7;            // degrees * 1e7
  int32_t altCm;            // altitude above mean sea level, cm
  uint16_t hdopX100;        // HDOP * 100 (UINT16_MAX = not seen yet)
  uint8_t sats;             // satellites used
  uint8_t fixQuality;       // GGA fix quality (1 = GPS, 2 = DGPS, ...)
  uint16_t sogKnotsX100;    // speed over ground, knots * 100
  uint16_t cogDegX100;      // course over ground, degrees * 100
  uint32_t utcTimeMs;       // fix time, ms since UTC midnight
  uint32_t utcDate;         // ddmmyy as a number, e.g. 260926 (0 = not seen yet)
  uint16_t gnssRecoveries;  // times the GNSS module was power-cycled since boot
  uint16_t i2cRecoveries;   // times the I2C peripheral was restarted since boot
  uint8_t resetReason;      // esp_reset_reason_t of the last boot (1 = power-on, 4 = panic, 6 = task WDT, 9 = brownout, ...)
  uint8_t crc;              // CRC-8/SMBUS (poly 0x07, init 0) over all bytes above
};
static_assert(sizeof(FixPacket) == 44, "FixPacket layout changed; bump FIX_PACKET_VERSION");

// Written from loop(), read from the I2C driver's task — guard with fixMux.
// latestFix.flags holds the last-set FIX_VALID/FIX_GOOD/MOTION_VALID/
// HAS_POSITION; onI2CRequest() applies the age limits and LINK_OK.
static portMUX_TYPE fixMux = portMUX_INITIALIZER_UNLOCKED;
static FixPacket latestFix = {};
static unsigned long latestFixMillis = 0;
static unsigned long lastGgaMillis = 0;
static bool ggaSeen = false;
static unsigned long lastMotionMillis = 0;

// --- LED ---------------------------------------------------------------

void updateBlink() {
  unsigned long now = millis();
  if (now - lastBlinkTime >= BLINK_INTERVAL_MS) {
    lastBlinkTime = now;
    ledOn = !ledOn;
    digitalWrite(LED_PIN, ledOn ? HIGH : LOW);
  }
}

// --- GNSS configuration ----------------------------------------------------

// Sends a "$PCAS...*XX\r\n" command to the module with an auto-computed
// checksum. `body` is everything between '$' and '*', e.g. "PCAS04,3".
// This module (Quectel L76K, AT6558R-based chipset) speaks the CASIC
// "$PCAS..." command family, NOT the MediaTek "$PMTK..." set most L76-ish
// tutorials assume — see Quectel's L76K GNSS Protocol Specification.
// PCAS commands are not acknowledged over NMEA (no reply sentence exists
// for this chip), so correctness can only be verified behaviorally.
void sendPCAS(const char *body) {
  uint8_t checksum = 0;
  for (const char *p = body; *p; p++) {
    checksum ^= (uint8_t)*p;
  }
  GNSS_UART.print('$');
  GNSS_UART.print(body);
  GNSS_UART.printf("*%02X\r\n", checksum);
}

// Applies GNSS receiver settings once at boot. Every value here is sent
// explicitly, even where it matches the factory default, because this
// chip persists PCAS-set values in its own memory across resets — a
// leftover setting from a prior debugging session (e.g. a slower output
// rate) would otherwise silently carry into normal operation. See
// DECISIONS.md for why baud rate and fix interval are NOT raised above
// the 9600 / 1Hz factory defaults.
void configureGNSS() {
  delay(100);  // let the UART line settle before the module sees traffic
  sendPCAS("PCAS04,3");  // GPS + BeiDou
  delay(100);
  sendPCAS("PCAS02,1000");  // 1Hz fix rate
  delay(100);
  // GGA/GSA/RMC every fix; GLL/GSV/VTG/ZDA/ANT off (unused by readGNSS()).
  sendPCAS("PCAS03,1,0,1,0,1,0,0,0,0,0,,,0,0");
}

// Brings back a GNSS link that has gone silent (module hung, brownout,
// connector glitch): power-cycles the module, forces it back to 9600 baud
// in case a past session left it at 115200 (PCAS01 persists too), then
// re-applies config. Blocks ~1s; the I2C task keeps answering meanwhile,
// with LINK_OK and FIX_VALID clear.
void recoverGNSS() {
  GNSS_UART.end();  // don't drive TX into an unpowered module
  digitalWrite(GNSS_CTRL_PIN, HIGH);  // GNSS rail off
  delay(500);
  digitalWrite(GNSS_CTRL_PIN, LOW);  // GNSS rail on
  delay(100);

  GNSS_UART.begin(115200, SERIAL_8N1, GNSS_RX_PIN, GNSS_TX_PIN);
  sendPCAS("PCAS01,1");  // 1 = 9600; just noise to a module already at 9600
  GNSS_UART.flush();
  delay(50);
  GNSS_UART.updateBaudRate(GNSS_BAUD);
  configureGNSS();
  nmeaLine = "";
}

void checkGNSSLink() {
  unsigned long now = millis();
  unsigned long silentMs = now - lastGgaMillis;  // only loop() writes it; no lock needed to read here
  if (silentMs < GNSS_RECOVERY_AFTER_MS || now - lastGnssRecoveryMillis < GNSS_RECOVERY_INTERVAL_MS) {
    return;
  }
  Serial.printf("GNSS: no data for %lums, power-cycling module\r\n", silentMs);
  recoverGNSS();
  lastGnssRecoveryMillis = millis();
  portENTER_CRITICAL(&fixMux);
  if (latestFix.gnssRecoveries < UINT16_MAX) {
    latestFix.gnssRecoveries++;
  }
  portEXIT_CRITICAL(&fixMux);
}

// --- NMEA parsing --------------------------------------------------------

// Verifies the "*XX" checksum at the end of a "$..." NMEA sentence.
bool nmeaChecksumValid(const String &sentence) {
  int star = sentence.lastIndexOf('*');
  if (star < 1 || star + 2 >= (int)sentence.length()) {
    return false;
  }
  uint8_t checksum = 0;
  for (int i = 1; i < star; i++) {  // skip leading '$'
    checksum ^= (uint8_t)sentence[i];
  }
  uint8_t expected = (uint8_t)strtol(sentence.substring(star + 1).c_str(), nullptr, 16);
  return checksum == expected;
}

// Converts NMEA "ddmm.mmmm" / "dddmm.mmmm" + hemisphere into decimal degrees.
double nmeaToDecimalDegrees(const String &raw, char hemisphere) {
  if (raw.length() == 0) {
    return 0.0;
  }
  double value = raw.toDouble();
  int degrees = (int)(value / 100);
  double minutes = value - (degrees * 100);
  double decimal = degrees + minutes / 60.0;
  if (hemisphere == 'S' || hemisphere == 'W') {
    decimal = -decimal;
  }
  return decimal;
}

// Splits an NMEA sentence on ',' and '*', returns number of fields found.
int splitNmeaFields(const String &sentence, String fields[], int maxFields) {
  int count = 0;
  int start = 0;
  for (int i = 0; i <= (int)sentence.length() && count < maxFields; i++) {
    char c = (i == (int)sentence.length()) ? '\0' : sentence[i];
    if (c == ',' || c == '*' || c == '\0') {
      fields[count++] = sentence.substring(start, i);
      start = i + 1;
      if (c == '*' || c == '\0') {
        break;
      }
    }
  }
  return count;
}

// Converts NMEA "hhmmss.sss" into milliseconds since UTC midnight.
uint32_t nmeaTimeToMs(const String &raw) {
  if (raw.length() < 6) {
    return 0;
  }
  uint32_t hh = raw.substring(0, 2).toInt();
  uint32_t mm = raw.substring(2, 4).toInt();
  uint32_t secMs = (uint32_t)lround(raw.substring(4).toDouble() * 1000.0);
  return (hh * 3600 + mm * 60) * 1000 + secMs;
}

// Parses a GGA sentence ($GNGGA/$GPGGA) and prints coordinates if there's a fix.
void handleGGA(const String &sentence) {
  String fields[15];
  int count = splitNmeaFields(sentence, fields, 15);
  if (count < 10) {
    return;
  }

  const String &rawLat = fields[2];
  const String &ns = fields[3];
  const String &rawLon = fields[4];
  const String &ew = fields[5];
  int fixQuality = fields[6].toInt();
  int satellites = fields[7].toInt();
  const String &altitude = fields[9];

  // Qualities 1-5 are real fixes; 6 (dead-reckoning estimate), 7 (manual)
  // and 8 (simulation) are not something to navigate on.
  bool hasFix = fixQuality >= 1 && fixQuality <= 5 && rawLat.length() > 0 && rawLon.length() > 0;
  double lat = 0.0;
  double lon = 0.0;
  if (hasFix) {
    lat = nmeaToDecimalDegrees(rawLat, ns.length() ? ns[0] : 'N');
    lon = nmeaToDecimalDegrees(rawLon, ew.length() ? ew[0] : 'E');
    if (fabs(lat) > 90.0 || fabs(lon) > 180.0) {
      hasFix = false;
    }
  }

  // A fix is only as good as its geometry: flag it once satellite count and
  // HDOP look reasonable for navigation (HDOP <= 2.5 is the usual "good"
  // rule of thumb; lastHdop == -1 means no GSA sentence has arrived yet).
  bool goodFix = hasFix && satellites >= 4 && (lastHdop < 0.0 || lastHdop <= 2.5);

  unsigned long now = millis();
  portENTER_CRITICAL(&fixMux);
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
    latestFix.latE7 = (int32_t)lround(lat * 1e7);
    latestFix.lonE7 = (int32_t)lround(lon * 1e7);
    latestFix.altCm = (int32_t)lround(altitude.toDouble() * 100.0);
    latestFix.hdopX100 = lastHdop < 0.0 ? UINT16_MAX : (uint16_t)min(lround(lastHdop * 100.0), 65534L);
    latestFix.sats = (uint8_t)constrain(satellites, 0, 255);
    latestFix.fixQuality = (uint8_t)fixQuality;
    latestFix.utcTimeMs = nmeaTimeToMs(fields[1]);
    latestFixMillis = now;
  } else {
    // Nothing new was measured: leave seq/timestamps/position alone and
    // just stop vouching for the old fix.
    latestFix.flags &= ~(FIX_FLAG_FIX_VALID | FIX_FLAG_FIX_GOOD);
  }
  portEXIT_CRITICAL(&fixMux);

  if (!hasFix) {
    Serial.printf("GNSS: waiting for fix... (quality: %d, satellites in view: %d)\r\n", fixQuality, satellites);
    return;
  }

  Serial.printf(
      "GNSS fix: lat=%.6f, lon=%.6f, alt=%sm, sats=%d, hdop=%.1f, sog=%.2fkn, cog=%.1f, %s\r\n",
      lat, lon, altitude.c_str(), satellites, lastHdop, lastSpeedKnots, lastCourseDeg,
      goodFix ? "OK" : "LOW-QUALITY");
}

// Parses a GSA sentence ($GNGSA/$GPGSA/$BDGSA/...) and stashes HDOP.
void handleGSA(const String &sentence) {
  String fields[18];
  int count = splitNmeaFields(sentence, fields, 18);
  if (count < 17) {
    return;
  }
  double hdop = fields[16].toDouble();
  if (hdop > 0.0) {
    lastHdop = hdop;
  }
}

// Parses an RMC sentence ($GNRMC/$GPRMC/...) and stashes speed/course
// over ground (only when the sentence reports itself valid).
void handleRMC(const String &sentence) {
  String fields[13];
  int count = splitNmeaFields(sentence, fields, 13);
  if (count < 9) {
    return;
  }
  if (fields[2] != "A") {
    portENTER_CRITICAL(&fixMux);
    latestFix.flags &= ~FIX_FLAG_MOTION_VALID;
    portEXIT_CRITICAL(&fixMux);
    return;
  }
  lastSpeedKnots = fields[7].toDouble();
  lastCourseDeg = fields[8].toDouble();

  unsigned long now = millis();
  portENTER_CRITICAL(&fixMux);
  latestFix.flags |= FIX_FLAG_MOTION_VALID;
  latestFix.sogKnotsX100 = (uint16_t)constrain(lround(lastSpeedKnots * 100.0), 0L, 65535L);
  latestFix.cogDegX100 = (uint16_t)constrain(lround(lastCourseDeg * 100.0), 0L, 35999L);
  latestFix.utcDate = count > 9 ? (uint32_t)fields[9].toInt() : 0;
  lastMotionMillis = now;
  portEXIT_CRITICAL(&fixMux);
}

// Pumps bytes from the GNSS UART and dispatches complete sentences.
void readGNSS() {
  while (GNSS_UART.available()) {
    char c = (char)GNSS_UART.read();
    if (c == '$') {
      nmeaLine = "";  // resync: a '$' always starts a new sentence
    }
    if (c == '\n') {
      nmeaLine.trim();
      if (nmeaLine.startsWith("$") && nmeaChecksumValid(nmeaLine)) {
        if (nmeaLine.indexOf("GGA") == 3) {  // e.g. "$GNGGA" / "$GPGGA"
          handleGGA(nmeaLine);
        } else if (nmeaLine.indexOf("GSA") == 3) {
          handleGSA(nmeaLine);
        } else if (nmeaLine.indexOf("RMC") == 3) {
          handleRMC(nmeaLine);
        }
      }
      nmeaLine = "";
    } else if (c != '\r') {
      nmeaLine += c;
      if (nmeaLine.length() > 120) {  // guard against a corrupt/unterminated line
        nmeaLine = "";
      }
    }
  }
}

// --- I2C peripheral ------------------------------------------------------

// CRC-8/SMBUS: poly 0x07, init 0x00, no reflection.
uint8_t crc8(const uint8_t *data, size_t len) {
  uint8_t crc = 0;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (int bit = 0; bit < 8; bit++) {
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x07) : (uint8_t)(crc << 1);
    }
  }
  return crc;
}

// Runs in the I2C driver's task when the controller reads from us (the
// ESP32-S3 stretches SCL meanwhile), so ages and validity are per read.
void onI2CRequest() {
  FixPacket packet;
  portENTER_CRITICAL(&fixMux);
  packet = latestFix;
  unsigned long fixMillis = latestFixMillis;
  unsigned long ggaMillis = lastGgaMillis;
  bool gga = ggaSeen;
  unsigned long motionMillis = lastMotionMillis;
  portEXIT_CRITICAL(&fixMux);
  unsigned long now = millis();

  packet.version = FIX_PACKET_VERSION;
  bool hasPosition = packet.flags & FIX_FLAG_HAS_POSITION;
  packet.ageMs = hasPosition ? (uint32_t)(now - fixMillis) : UINT32_MAX;
  if (gga && now - ggaMillis <= GNSS_LINK_TIMEOUT_MS) {
    packet.flags |= FIX_FLAG_LINK_OK;
  } else {
    packet.flags &= ~FIX_FLAG_LINK_OK;
  }
  if (!hasPosition || packet.ageMs > FIX_MAX_AGE_MS) {
    packet.flags &= ~(FIX_FLAG_FIX_VALID | FIX_FLAG_FIX_GOOD);
  }
  if (now - motionMillis > FIX_MAX_AGE_MS) {
    packet.flags &= ~FIX_FLAG_MOTION_VALID;
  }
  packet.crc = crc8((const uint8_t *)&packet, sizeof(packet) - 1);
  Wire1.write((const uint8_t *)&packet, sizeof(packet));
}

bool startI2C() {
  return Wire1.begin((uint8_t)I2C_ADDR, I2C_SDA_PIN, I2C_SCL_PIN, I2C_FREQ);
}

// A target can wedge the bus by holding SDA (or SCL, while stretching) low
// when a transfer is cut short, e.g. the controller resets mid-read.
// Normal traffic never holds a line low for I2C_STUCK_MS, so if that's
// seen, restart our I2C peripheral to let go of the lines. If it was the
// controller holding the line, restarting ours is harmless.
void checkI2CBus() {
  unsigned long now = millis();
  bool idle = gpio_get_level((gpio_num_t)I2C_SDA_PIN) && gpio_get_level((gpio_num_t)I2C_SCL_PIN);
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
  Serial.println("I2C: bus line stuck low, restarting peripheral");
  Wire1.end();
  if (!startI2C()) {
    Serial.println("I2C: failed to restart peripheral mode");
  }
  i2cLastRestartMillis = millis();
  i2cLastIdleMillis = i2cLastRestartMillis;
  portENTER_CRITICAL(&fixMux);
  if (latestFix.i2cRecoveries < UINT16_MAX) {
    latestFix.i2cRecoveries++;
  }
  portEXIT_CRITICAL(&fixMux);
}

// --- Setup / loop --------------------------------------------------------

void setup() {
  // Never let debug output stall the loop: with a host attached that isn't
  // reading (e.g. a companion computer powering us over USB), each write
  // could otherwise block for up to ~2s. Unread output is dropped instead.
  Serial.setTxTimeoutMs(0);
  Serial.begin(115200);

  // I2C comes up first, before any waiting, so the controller gets
  // answers (with FIX_VALID clear) as soon as possible after a reset.
  latestFix.hdopX100 = UINT16_MAX;
  latestFix.resetReason = (uint8_t)esp_reset_reason();
  Wire1.onRequest(onI2CRequest);
  bool i2cStarted = startI2C();

  // This board's USB is native CDC: a reset drops and re-enumerates the
  // port, so a host-side monitor is briefly disconnected right when
  // setup() runs. Give it up to 2s to reattach so early boot prints
  // aren't lost into a CDC endpoint nobody's listening on yet. Bounded
  // so boot still proceeds untethered (no monitor ever attached).
  unsigned long serialWaitStart = millis();
  while (!Serial && millis() - serialWaitStart < 2000) {
    delay(10);
  }

  Serial.printf("Boot, reset reason: %d\r\n", latestFix.resetReason);
  if (i2cStarted) {
    Serial.printf("I2C: serving fixes at 0x%02X (SDA=%d, SCL=%d)\r\n", I2C_ADDR, I2C_SDA_PIN, I2C_SCL_PIN);
  } else {
    Serial.println("I2C: failed to start peripheral mode");
  }

  pinMode(LED_PIN, OUTPUT);

  pinMode(GNSS_CTRL_PIN, OUTPUT);
  digitalWrite(GNSS_CTRL_PIN, LOW);  // enable the GNSS 3V3 rail (active-low switch, same convention as Vext_Ctrl)

  pinMode(GNSS_RESET_PIN, OUTPUT);
  digitalWrite(GNSS_RESET_PIN, HIGH);  // release reset

  pinMode(GNSS_WAKE_PIN, INPUT);
  pinMode(GNSS_PPS_PIN, INPUT);

  delay(100);  // let the GNSS module's power rail settle before talking to it
  nmeaLine.reserve(128);  // one allocation up front instead of regrowing per sentence
  GNSS_UART.begin(GNSS_BAUD, SERIAL_8N1, GNSS_RX_PIN, GNSS_TX_PIN);
  configureGNSS();
  lastGnssRecoveryMillis = millis();  // give the module a full window before first recovery

  Serial.println("L76K GNSS demo starting...");

  // Reboot if loop() ever stops returning for 5s (CONFIG_ESP_TASK_WDT_TIMEOUT_S).
  // Longest intentional block is recoverGNSS() at ~1s.
  enableLoopWDT();
}

void loop() {
  updateBlink();
  readGNSS();
  checkGNSSLink();
  checkI2CBus();
}
