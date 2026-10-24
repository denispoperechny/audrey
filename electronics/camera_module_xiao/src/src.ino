// Audrey camera: XIAO ESP32-S3 Sense (OV3660). Joins WiFi, asks the camera service for a
// picture task every second, takes the picture with the task's parameters and uploads it.
//
//   POST <SERVICE_URL>/camera/next-task?camera=<CAMERA_NAME>
//        -> {"utc_now": "...", "task": null | {"id": "...", "params": {...}, ...}}
//        utc_now sets the board's clock, so taken_at is in UTC
//   POST <SERVICE_URL>/picture-tasks/<id>/picture?taken_at=<UTC>   body: the JPEG
//
// The camera is only switched on for a task (init, settle, shot, upload, off), so it stays
// cool between tasks and every task starts from the sensor's power-on settings.
//
// WiFi and the service address are in secrets.h (not in git; copy secrets.example.h).
// Needs the ArduinoJson library (arduino-cli lib install ArduinoJson). Build and upload:
//   arduino-cli compile -b esp32:esp32:XIAO_ESP32S3:PSRAM=opi,CDCOnBoot=default src
//   arduino-cli upload -b esp32:esp32:XIAO_ESP32S3:PSRAM=opi,CDCOnBoot=default -p <port> src
// Log on the USB serial port at 115200.

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <NetworkClientSecure.h>
#include <WiFi.h>
#include <sys/time.h>

#include "esp_camera.h"
#include "root_ca.h"
#include "secrets.h"

const unsigned long POLL_MS = 1000;
const unsigned long WIFI_TIMEOUT_MS = 20000;  // give up one connection attempt after this, then retry
const int HTTP_TIMEOUT_MS = 20000;
const int UPLOAD_ATTEMPTS = 3;
const int SETTLE_MIN_FRAMES = 2;  // frames thrown away even with settle_ms 0: the first can be stale

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

// A task's picture parameters (the service's PictureParams; its defaults here too).
struct Params {
  framesize_t frame_size = FRAMESIZE_FHD;
  int jpeg_quality = 8;
  bool hmirror = true;  // relative to the sensor's power-on setting: true = flipped from it
  bool vflip = false;   // likewise
  int settle_ms = 1000;
  gainceiling_t gain_ceiling = GAINCEILING_32X;
  bool night_mode = false;
  int ae_level = 2;
};

NetworkClientSecure tls;
HTTPClient http;
unsigned long last_poll = 0;
bool clock_set = false;

// ---- names to camera settings -------------------------------------------------------------

framesize_t frame_size_from(const char *name, framesize_t fallback) {
  static const struct { const char *name; framesize_t size; } sizes[] = {
    {"QVGA", FRAMESIZE_QVGA}, {"VGA", FRAMESIZE_VGA}, {"SVGA", FRAMESIZE_SVGA}, {"XGA", FRAMESIZE_XGA},
    {"HD", FRAMESIZE_HD}, {"SXGA", FRAMESIZE_SXGA}, {"UXGA", FRAMESIZE_UXGA}, {"FHD", FRAMESIZE_FHD},
    {"QXGA", FRAMESIZE_QXGA},
  };
  for (auto &s : sizes)
    if (name && strcmp(name, s.name) == 0) return s.size;
  return fallback;
}

gainceiling_t gain_ceiling_from(const char *name, gainceiling_t fallback) {
  static const struct { const char *name; gainceiling_t gain; } gains[] = {
    {"2X", GAINCEILING_2X}, {"4X", GAINCEILING_4X}, {"8X", GAINCEILING_8X}, {"16X", GAINCEILING_16X},
    {"32X", GAINCEILING_32X}, {"64X", GAINCEILING_64X}, {"128X", GAINCEILING_128X},
  };
  for (auto &g : gains)
    if (name && strcmp(name, g.name) == 0) return g.gain;
  return fallback;
}

Params params_from(JsonObjectConst p) {
  Params out;  // anything missing keeps its default
  out.frame_size = frame_size_from(p["frame_size"] | (const char *)nullptr, out.frame_size);
  out.jpeg_quality = p["jpeg_quality"] | out.jpeg_quality;
  out.hmirror = p["hmirror"] | out.hmirror;
  out.vflip = p["vflip"] | out.vflip;
  out.settle_ms = p["settle_ms"] | out.settle_ms;
  out.gain_ceiling = gain_ceiling_from(p["gain_ceiling"] | (const char *)nullptr, out.gain_ceiling);
  out.night_mode = p["night_mode"] | out.night_mode;
  out.ae_level = p["ae_level"] | out.ae_level;
  return out;
}

// ---- clock ----------------------------------------------------------------------------------

// Days since 1970-01-01 of a civil date (Howard Hinnant's days_from_civil).
long days_from_civil(int y, int m, int d) {
  y -= m <= 2;
  long era = (y >= 0 ? y : y - 399) / 400;
  long yoe = y - era * 400;
  long doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
  long doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  return era * 146097 + doe - 719468;
}

// Sets the clock from the service's utc_now, e.g. "2026-10-23T20:02:00.123+00:00".
void set_clock(const char *utc_now) {
  int y, mo, d, h, mi, s, ms = 0;
  if (!utc_now || sscanf(utc_now, "%d-%d-%dT%d:%d:%d.%d", &y, &mo, &d, &h, &mi, &s, &ms) < 6) return;
  struct timeval tv;
  tv.tv_sec = days_from_civil(y, mo, d) * 86400L + h * 3600L + mi * 60L + s;
  tv.tv_usec = ms * 1000L;
  settimeofday(&tv, nullptr);
  if (!clock_set) Serial.printf("clock set: %s\n", utc_now);
  clock_set = true;
}

// Now as ISO 8601 UTC with milliseconds, e.g. "2026-10-23T20:02:03.456Z".
String utc_iso() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  struct tm t;
  gmtime_r(&tv.tv_sec, &t);
  char text[32];
  snprintf(text, sizeof text, "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ", t.tm_year + 1900, t.tm_mon + 1, t.tm_mday,
           t.tm_hour, t.tm_min, t.tm_sec, (int)(tv.tv_usec / 1000));
  return text;
}

// ---- WiFi -----------------------------------------------------------------------------------

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
  return true;
}

// ---- camera ---------------------------------------------------------------------------------

bool camera_on(const Params &p) {
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
  c.frame_size = p.frame_size;
  c.jpeg_quality = p.jpeg_quality;
  c.fb_count = 2;
  c.fb_location = CAMERA_FB_IN_PSRAM;
  c.grab_mode = CAMERA_GRAB_LATEST;
  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK) {
    Serial.printf("camera: init failed 0x%x (PSRAM found: %d)\n", err, psramFound());
    return false;
  }
  sensor_t *s = esp_camera_sensor_get();
  // The flips are relative to the sensor's power-on state: on this board the picture comes out
  // mirrored left-right, so hmirror true (the default) flips it to look right.
  s->set_hmirror(s, p.hmirror ? !s->status.hmirror : s->status.hmirror);
  s->set_vflip(s, p.vflip ? !s->status.vflip : s->status.vflip);
  s->set_gainceiling(s, p.gain_ceiling);
  s->set_aec2(s, p.night_mode);
  s->set_ae_level(s, p.ae_level);
  return true;
}

// Lets auto exposure and white balance settle, then takes the picture; nullptr if it failed.
camera_fb_t *take_picture(const Params &p) {
  unsigned long start = millis();
  int frames = 0;
  while (millis() - start < (unsigned long)p.settle_ms || frames < SETTLE_MIN_FRAMES) {
    camera_fb_t *fb = esp_camera_fb_get();
    if (fb) esp_camera_fb_return(fb);
    frames++;
  }
  return esp_camera_fb_get();
}

// ---- the service ----------------------------------------------------------------------------

// Asks for a task. Fills the task id and its parameters and returns true if there is one.
bool next_task(String &task_id, Params &params) {
  http.begin(tls, String(SERVICE_URL) + "/camera/next-task?camera=" + CAMERA_NAME);
  http.setTimeout(HTTP_TIMEOUT_MS);
  int code = http.POST((uint8_t *)nullptr, 0);
  if (code != 200) {
    Serial.printf("next-task: HTTP %d %s\n", code, code < 0 ? http.errorToString(code).c_str() : "");
    http.end();
    return false;
  }
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, http.getString());
  http.end();
  if (err) {
    Serial.printf("next-task: bad JSON (%s)\n", err.c_str());
    return false;
  }
  set_clock(doc["utc_now"]);
  JsonObjectConst task = doc["task"];
  if (task.isNull()) return false;
  task_id = task["id"].as<String>();
  params = params_from(task["params"]);
  return task_id.length() > 0;
}

// Uploads the picture; true once the service has it (or refuses it for good, e.g. the task is
// gone, as retrying wouldn't help).
bool upload(const String &task_id, const String &taken_at, camera_fb_t *fb) {
  for (int attempt = 1; attempt <= UPLOAD_ATTEMPTS; attempt++) {
    if (!wifi_up()) {
      delay(2000);
      continue;
    }
    http.begin(tls, String(SERVICE_URL) + "/picture-tasks/" + task_id + "/picture?taken_at=" + taken_at);
    http.setTimeout(HTTP_TIMEOUT_MS);
    http.addHeader("Content-Type", "image/jpeg");
    unsigned long start = millis();
    int code = http.POST(fb->buf, fb->len);
    String body = code > 0 ? http.getString() : http.errorToString(code);
    http.end();
    if (code == 200) {
      Serial.printf("upload: done in %lu ms\n", millis() - start);
      return true;
    }
    Serial.printf("upload: attempt %d, HTTP %d %s\n", attempt, code, body.c_str());
    if (code >= 400 && code < 500) return true;  // refused (no such task, already done, ...): give up
    delay(2000);
  }
  return false;
}

void work(const String &task_id, const Params &p) {
  Serial.printf("task %s: frame size %d, quality %d, hmirror %d, vflip %d, settle %d ms, gain ceiling %d, "
                "night %d, ae %d\n", task_id.c_str(), p.frame_size, p.jpeg_quality, p.hmirror, p.vflip,
                p.settle_ms, p.gain_ceiling, p.night_mode, p.ae_level);
  if (!camera_on(p)) {
    esp_camera_deinit();
    Serial.println("task: no picture (camera failed); the task stays assigned");
    return;
  }
  camera_fb_t *fb = take_picture(p);
  String taken_at = utc_iso();
  if (!fb) {
    Serial.println("task: no picture (capture failed); the task stays assigned");
  } else {
    Serial.printf("picture: %u bytes, %ux%u, taken %s\n", fb->len, fb->width, fb->height, taken_at.c_str());
    if (!upload(task_id, taken_at, fb)) Serial.println("task: upload failed; the task stays assigned");
    esp_camera_fb_return(fb);
  }
  esp_camera_deinit();
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.printf("Audrey camera %s, service %s, PSRAM %u bytes\n", CAMERA_NAME, SERVICE_URL, ESP.getPsramSize());
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // no WiFi power saving: steadier polling and faster uploads
  tls.setCACert(ROOT_CA);
  http.setReuse(true);  // keep the HTTPS connection between polls instead of a new handshake each second
  wifi_up();
}

void loop() {
  if (millis() - last_poll < POLL_MS) {
    delay(10);
    return;
  }
  last_poll = millis();
  if (!wifi_up()) return;
  String task_id;
  Params params;
  if (next_task(task_id, params)) work(task_id, params);
}
