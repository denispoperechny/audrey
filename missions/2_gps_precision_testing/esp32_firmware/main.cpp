#include <Arduino.h>

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

#define LED_PIN 35

#define GNSS_CTRL_PIN 34
#define GNSS_RX_PIN 39  // ESP32 RX <- L76K TX
#define GNSS_TX_PIN 38  // ESP32 TX -> L76K RX
#define GNSS_WAKE_PIN 40
#define GNSS_PPS_PIN 41
#define GNSS_RESET_PIN 42

#define GNSS_UART Serial1
#define GNSS_BAUD 9600

const unsigned long BLINK_INTERVAL_MS = 3000;

static unsigned long lastBlinkTime = 0;
static bool ledOn = false;

static String nmeaLine;

// Latest quality/motion info from GSA/RMC, used to qualify GGA fixes.
static double lastHdop = -1.0;  // horizontal dilution of precision (-1 = not seen yet)
static double lastSpeedKnots = 0.0;
static double lastCourseDeg = 0.0;

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

  if (fixQuality == 0 || rawLat.length() == 0 || rawLon.length() == 0) {
    Serial.printf("GNSS: waiting for fix... (satellites in view: %d)\r\n", satellites);
    return;
  }

  double lat = nmeaToDecimalDegrees(rawLat, ns.length() ? ns[0] : 'N');
  double lon = nmeaToDecimalDegrees(rawLon, ew.length() ? ew[0] : 'E');

  // A fix is only as good as its geometry: flag it once satellite count and
  // HDOP look reasonable for navigation (HDOP <= 2.5 is the usual "good"
  // rule of thumb; lastHdop == -1 means no GSA sentence has arrived yet).
  bool goodFix = satellites >= 4 && (lastHdop < 0.0 || lastHdop <= 2.5);

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
  if (count < 9 || fields[2] != "A") {
    return;
  }
  lastSpeedKnots = fields[7].toDouble();
  lastCourseDeg = fields[8].toDouble();
}

// Pumps bytes from the GNSS UART and dispatches complete sentences.
void readGNSS() {
  while (GNSS_UART.available()) {
    char c = (char)GNSS_UART.read();
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

// --- Setup / loop --------------------------------------------------------

void setup() {
  Serial.begin(115200);
  // This board's USB is native CDC: a reset drops and re-enumerates the
  // port, so a host-side monitor is briefly disconnected right when
  // setup() runs. Give it up to 2s to reattach so early boot prints
  // aren't lost into a CDC endpoint nobody's listening on yet. Bounded
  // so boot still proceeds untethered (no monitor ever attached).
  unsigned long serialWaitStart = millis();
  while (!Serial && millis() - serialWaitStart < 2000) {
    delay(10);
  }

  pinMode(LED_PIN, OUTPUT);

  pinMode(GNSS_CTRL_PIN, OUTPUT);
  digitalWrite(GNSS_CTRL_PIN, LOW);  // enable the GNSS 3V3 rail (active-low switch, same convention as Vext_Ctrl)

  pinMode(GNSS_RESET_PIN, OUTPUT);
  digitalWrite(GNSS_RESET_PIN, HIGH);  // release reset

  pinMode(GNSS_WAKE_PIN, INPUT);
  pinMode(GNSS_PPS_PIN, INPUT);

  delay(100);  // let the GNSS module's power rail settle before talking to it
  GNSS_UART.begin(GNSS_BAUD, SERIAL_8N1, GNSS_RX_PIN, GNSS_TX_PIN);
  configureGNSS();

  Serial.println("L76K GNSS demo starting...");
}

void loop() {
  updateBlink();
  readGNSS();
}
