// Audrey streaming camera: XIAO ESP32-S3 Sense (OV3660). Takes a picture, sends it straight to
// the camera service with the moment it was taken, and again: at most one picture a second
// (from the start of one shot to the start of the next; a slow upload delays the next one).
//
//   POST <SERVICE_URL>/stream/pictures?camera=<CAMERA_NAME>&taken_at=<UTC>   body: the JPEG
//
// Plain HTTP (the service accepts it), so no TLS on the board. The picture settings are the
// service's defaults (GET /api/picture-defaults) except the size, HD (1280 x 720) instead of FHD,
// and the JPEG quality, 12 instead of 8, for smaller pictures and quicker uploads.
// The camera stays on and keeps capturing; each send takes the newest frame, and taken_at is
// when that frame was captured (the camera driver's frame time, turned into UTC).
//
// UTC comes from internet time servers (SNTP: time.google.com, time.cloudflare.com,
// pool.ntp.org), first once WiFi is up and then every hour by itself. Nothing is sent until
// the clock has been set, so every picture has a real time.
//
// WiFi and the service address are in secrets.h (not in git; copy secrets.example.h).
// Build and upload:
//   arduino-cli compile -b esp32:esp32:XIAO_ESP32S3:PSRAM=opi,CDCOnBoot=default src
//   arduino-cli upload -b esp32:esp32:XIAO_ESP32S3:PSRAM=opi,CDCOnBoot=default -p <port> src
// Log on the USB serial port at 115200.

#include <HTTPClient.h>
#include <WiFi.h>
#include <esp_timer.h>
#include <sys/time.h>
#include <time.h>

#include "esp_camera.h"
#include "secrets.h"

const unsigned long MIN_INTERVAL_MS = 1000;  // between the starts of two shots
const unsigned long WIFI_TIMEOUT_MS = 20000;  // give up one connection attempt after this, then retry
const int HTTP_TIMEOUT_MS = 10000;
const time_t CLOCK_VALID_AFTER = 1767225600;  // 2026-01-01: before that, the clock hasn't been set
const int SETTLE_MS = 1000;  // auto exposure and white balance, once at start (service default)
const int CAPTURE_FAILURES_BEFORE_RESTART = 5;  // then the camera is re-initialised
const int STATS_EVERY = 20;  // frames between summary lines on the serial log
// A frame older than this when it comes out of the camera's buffer is dropped (not sent) and the
// next one taken: after an upload stalls, the buffer can hold a frame from seconds before.
const int64_t MAX_FRAME_AGE_US = 300000;
const int FRESH_FRAME_TRIES = 3;  // frames dropped at most before sending whatever comes

// The service's default picture settings, except HD and JPEG quality 12.
const framesize_t FRAME_SIZE = FRAMESIZE_HD;  // 1280 x 720
const int JPEG_QUALITY = 12;  // lower = better quality, bigger file; 12 (service default 8) roughly halves the
                              // size, so uploads need fewer round trips and are less often held up by a lost packet
const bool HMIRROR = true;  // relative to the sensor's power-on setting: on this board it needs flipping
const bool VFLIP = false;
const gainceiling_t GAIN_CEILING = GAINCEILING_32X;
const bool NIGHT_MODE = false;
const int AE_LEVEL = 2;

// Pins of the XIAO ESP32-S3 Sense camera board (Espressif's CAMERA_MODEL_XIAO_ESP32S3)
#define PWDN_GPIO_NUM -1
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 10
#define SIOD_GPIO_NUM 40
#define SIOC_GPIO_NUM 39
#define Y9_GPIO_NUM 48
#define Y8_GPIO_NUM 11
#define Y7_GPIO_NUM 12
#define Y6_GPIO_NUM 14
#define Y5_GPIO_NUM 16
#define Y4_GPIO_NUM 18
#define Y3_GPIO_NUM 17
#define Y2_GPIO_NUM 15
#define VSYNC_GPIO_NUM 38
#define HREF_GPIO_NUM 47
#define PCLK_GPIO_NUM 13

WiFiClient tcp;
HTTPClient http;
bool camera_ok = false;
bool clock_reported = false;
int capture_failures = 0;

// Statistics since the last summary line.
int sent = 0, failed = 0, stale_dropped = 0;
unsigned long upload_ms_total = 0;
size_t bytes_total = 0;

// ---- WiFi and clock -------------------------------------------------------------------------

bool wifi_up() {
  if (WiFi.status() == WL_CONNECTED) return true;
  Serial.printf("WiFi: connecting to %s\n", WIFI_SSID);
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_TIMEOUT_MS) delay(250);
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi: not connected, will retry");
    return false;
  }
  Serial.printf("WiFi: connected, IP %s, RSSI %d dBm\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  // (Re)start SNTP now that there is a network: UTC, no daylight saving. It keeps itself in
  // sync from here on (every hour).
  configTime(0, 0, "time.google.com", "time.cloudflare.com", "pool.ntp.org");
  return true;
}

bool clock_set() {
  time_t now = time(nullptr);
  bool ok = now > CLOCK_VALID_AFTER;
  if (ok && !clock_reported) {
    Serial.printf("clock: set from the time servers, %s", ctime(&now));
    clock_reported = true;
  }
  return ok;
}

// A UTC time as ISO 8601 with milliseconds, e.g. "2026-10-28T23:45:12.345Z".
String iso_utc(const struct timeval &tv) {
  struct tm t;
  gmtime_r(&tv.tv_sec, &t);
  char text[32];
  snprintf(text, sizeof text, "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ", t.tm_year + 1900, t.tm_mon + 1, t.tm_mday,
           t.tm_hour, t.tm_min, t.tm_sec, (int)(tv.tv_usec / 1000));
  return text;
}

// How long ago a frame was captured, in microseconds. The driver stamps frames with the board's
// microsecond timer (time since boot).
int64_t frame_age_us(const camera_fb_t *fb) {
  return esp_timer_get_time() - ((int64_t)fb->timestamp.tv_sec * 1000000 + fb->timestamp.tv_usec);
}

// When a frame was captured, in UTC: the wall clock now minus how long ago that was.
struct timeval captured_at(const camera_fb_t *fb) {
  struct timeval now;
  gettimeofday(&now, nullptr);
  int64_t age_us = frame_age_us(fb);
  if (age_us < 0 || age_us > 10000000) age_us = 0;  // not a timer stamp after all: use now
  int64_t at_us = (int64_t)now.tv_sec * 1000000 + now.tv_usec - age_us;
  struct timeval at;
  at.tv_sec = at_us / 1000000;
  at.tv_usec = at_us % 1000000;
  return at;
}

// ---- camera ---------------------------------------------------------------------------------

bool camera_on() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM; c.pin_d1 = Y3_GPIO_NUM; c.pin_d2 = Y4_GPIO_NUM; c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM; c.pin_d5 = Y7_GPIO_NUM; c.pin_d6 = Y8_GPIO_NUM; c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM; c.pin_pclk = PCLK_GPIO_NUM; c.pin_vsync = VSYNC_GPIO_NUM; c.pin_href = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM; c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM; c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;
  c.frame_size = FRAME_SIZE;
  c.jpeg_quality = JPEG_QUALITY;
  c.fb_count = 2;
  c.fb_location = CAMERA_FB_IN_PSRAM;
  c.grab_mode = CAMERA_GRAB_LATEST;  // each fb_get returns the newest frame
  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK) {
    Serial.printf("camera: init failed 0x%x (PSRAM found: %d)\n", err, psramFound());
    return false;
  }
  sensor_t *s = esp_camera_sensor_get();
  s->set_hmirror(s, HMIRROR ? !s->status.hmirror : s->status.hmirror);
  s->set_vflip(s, VFLIP ? !s->status.vflip : s->status.vflip);
  s->set_gainceiling(s, GAIN_CEILING);
  s->set_aec2(s, NIGHT_MODE);
  s->set_ae_level(s, AE_LEVEL);
  unsigned long start = millis();
  while (millis() - start < (unsigned long)SETTLE_MS) {  // let exposure and white balance settle
    camera_fb_t *fb = esp_camera_fb_get();
    if (fb) esp_camera_fb_return(fb);
  }
  Serial.printf("camera: ready, HD, quality %d, sensor PID 0x%x\n", JPEG_QUALITY, s->id.PID);
  return true;
}

// ---- sending --------------------------------------------------------------------------------

void send_frame() {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    Serial.println("camera: capture failed");
    if (++capture_failures >= CAPTURE_FAILURES_BEFORE_RESTART) {
      Serial.println("camera: restarting");
      esp_camera_deinit();
      camera_ok = camera_on();
      capture_failures = 0;
    }
    return;
  }
  capture_failures = 0;
  for (int tries = 0; tries < FRESH_FRAME_TRIES && frame_age_us(fb) > MAX_FRAME_AGE_US; tries++) {
    esp_camera_fb_return(fb);  // stale: give it back unsent and take the next one
    stale_dropped++;
    fb = esp_camera_fb_get();
    if (!fb) {
      Serial.println("camera: capture failed");
      return;
    }
  }
  String taken_at = iso_utc(captured_at(fb));
  http.begin(tcp, String(SERVICE_URL) + "/stream/pictures?camera=" + CAMERA_NAME + "&taken_at=" + taken_at);
  http.setTimeout(HTTP_TIMEOUT_MS);
  http.addHeader("Content-Type", "image/jpeg");
  unsigned long start = millis();
  int code = http.POST(fb->buf, fb->len);
  unsigned long took = millis() - start;
  size_t bytes = fb->len;
  esp_camera_fb_return(fb);
  if (code == 201) {
    sent++;
    upload_ms_total += took;
    bytes_total += bytes;
  } else {
    failed++;
    String why = code > 0 ? http.getString() : http.errorToString(code);
    Serial.printf("send: HTTP %d %s (%u bytes, taken %s)\n", code, why.c_str(), bytes, taken_at.c_str());
  }
  http.end();
  if (sent + failed >= STATS_EVERY) {
    Serial.printf("stream: %d sent, %d failed, %d stale frames dropped; average %lu ms and %u KB per picture; "
                  "last taken %s; RSSI %d dBm\n", sent, failed, stale_dropped, sent ? upload_ms_total / sent : 0,
                  sent ? (unsigned)(bytes_total / sent / 1024) : 0, taken_at.c_str(), WiFi.RSSI());
    sent = failed = stale_dropped = 0;
    upload_ms_total = 0;
    bytes_total = 0;
  }
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.printf("Audrey streaming camera %s, service %s, PSRAM %u bytes\n", CAMERA_NAME, SERVICE_URL, ESP.getPsramSize());
  setenv("TZ", "UTC0", 1);
  tzset();
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // no WiFi power saving: steadier uploads
  http.setReuse(true);  // keep the connection open between pictures
  camera_ok = camera_on();
  wifi_up();
}

void loop() {
  if (!camera_ok) {
    delay(2000);
    camera_ok = camera_on();
    return;
  }
  if (!wifi_up()) return;
  if (!clock_set()) {
    static unsigned long last_note = 0;
    if (millis() - last_note > 2000) {
      Serial.println("clock: waiting for the time servers");
      last_note = millis();
    }
    delay(100);
    return;
  }
  static unsigned long last_shot = 0;
  static bool shot_before = false;
  if (shot_before && millis() - last_shot < MIN_INTERVAL_MS) {
    delay(5);
    return;
  }
  last_shot = millis();
  shot_before = true;
  send_frame();
}
