#include <Arduino.h>
#include <Wire.h>

#define I2C_TARGET_ADDRESS 0x31
#define I2C_SDA_PIN 21
#define I2C_SCL_PIN 22
#define SEND_INTERVAL_MS 1000

#ifdef LED_BUILTIN
#define LED_PIN LED_BUILTIN
#else
#define LED_PIN 2 // most ESP32 WROOM dev boards route the onboard LED to GPIO2
#endif

// Must match the struct on the target MCU exactly, packed to avoid
// Xtensa (pads uint16_t to a 2-byte boundary) vs AVR (no padding) disagreeing
// on sizeof() and byte layout.
struct __attribute__((packed)) BoatCommand {
    int8_t throttle;
    int8_t rudder;
    uint8_t flags;
    uint16_t checksum;
};

uint32_t last_send_time = 0;

uint16_t computeChecksum(const BoatCommand &cmd) {
    return (uint8_t)cmd.throttle + (uint8_t)cmd.rudder + cmd.flags;
}

void setup() {
    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN); // controller mode, no address
    randomSeed(esp_random());
    pinMode(LED_PIN, OUTPUT);
}

void loop() {
    if (millis() - last_send_time >= SEND_INTERVAL_MS) {
        last_send_time = millis();

        BoatCommand cmd;
        cmd.throttle = random(-100, 101);
        cmd.rudder = random(-100, 101);
        cmd.flags = 0;
        cmd.checksum = computeChecksum(cmd);

        Wire.beginTransmission(I2C_TARGET_ADDRESS);
        Wire.write((uint8_t *)&cmd, sizeof(cmd));
        Wire.endTransmission();

        digitalWrite(LED_PIN, !digitalRead(LED_PIN));
    }
}
