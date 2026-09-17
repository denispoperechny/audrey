#include <Arduino.h>
#include <Wire.h>
#include <string.h>

#define I2C_TARGET_ADDRESS 0x31
#define SERIAL_BAUD 115200
#define LED_PIN LED_BUILTIN

// Fixed by hardware on ATmega328 (Wire.begin() cannot remap these)
#define I2C_SDA_PIN A4
#define I2C_SCL_PIN A5

// Must match the struct on the controller MCU exactly, packed to avoid
// AVR (no padding) vs Xtensa (pads uint16_t to a 2-byte boundary) disagreeing
// on sizeof() and byte layout.
struct __attribute__((packed)) BoatCommand {
    int8_t throttle;
    int8_t rudder;
    uint8_t flags;
    uint16_t checksum;
};

volatile BoatCommand latest_cmd;
volatile bool cmd_available = false;
volatile uint32_t rx_event_count = 0;
volatile uint32_t rx_bad_length_count = 0;
volatile int last_numBytes = -1;
uint32_t last_heartbeat_time = 0;
#define HEARTBEAT_INTERVAL_MS 1000

uint16_t computeChecksum(const BoatCommand &cmd) {
    return (uint8_t)cmd.throttle + (uint8_t)cmd.rudder + cmd.flags;
}

void onI2cReceive(int numBytes) {
    rx_event_count++;
    last_numBytes = numBytes;

    if (numBytes != sizeof(BoatCommand)) {
        rx_bad_length_count++;
        while (Wire.available()) {
            Wire.read();
        }
        return;
    }

    BoatCommand cmd;
    Wire.readBytes((uint8_t *)&cmd, sizeof(cmd));
    memcpy((void *)&latest_cmd, &cmd, sizeof(cmd));
    cmd_available = true;
}

void setup() {
    Serial.begin(SERIAL_BAUD);
    Wire.begin(I2C_TARGET_ADDRESS);
    Wire.onReceive(onI2cReceive);
    pinMode(LED_PIN, OUTPUT);

    Serial.println();
    Serial.print("I2C receiver up. address=0x");
    Serial.print(I2C_TARGET_ADDRESS, HEX);
    Serial.print(" SDA=A");
    Serial.print(I2C_SDA_PIN - A0);
    Serial.print(" SCL=A");
    Serial.print(I2C_SCL_PIN - A0);
    Serial.print(" sizeof(BoatCommand)=");
    Serial.println(sizeof(BoatCommand));
}

void loop() {
    if (millis() - last_heartbeat_time >= HEARTBEAT_INTERVAL_MS) {
        last_heartbeat_time = millis();

        noInterrupts();
        uint32_t events = rx_event_count;
        uint32_t bad_length = rx_bad_length_count;
        interrupts();

        Serial.print("[heartbeat] uptime_ms=");
        Serial.print(last_heartbeat_time);
        Serial.print(" rx_events=");
        Serial.print(events);
        Serial.print(" bad_length=");
        Serial.print(bad_length);
        Serial.print(" last_numBytes=");
        Serial.print(last_numBytes);
        Serial.print(" SDA_idle=");
        Serial.print(digitalRead(I2C_SDA_PIN));
        Serial.print(" SCL_idle=");
        Serial.println(digitalRead(I2C_SCL_PIN));
    }

    if (cmd_available) {
        cmd_available = false;
        BoatCommand cmd;
        noInterrupts();
        memcpy(&cmd, (const void *)&latest_cmd, sizeof(cmd));
        interrupts();

        Serial.print("throttle=");
        Serial.print(cmd.throttle);
        Serial.print(" rudder=");
        Serial.print(cmd.rudder);
        Serial.print(" flags=");
        Serial.print(cmd.flags);
        Serial.print(" checksum=");
        Serial.print(cmd.checksum);

        if (cmd.checksum != computeChecksum(cmd)) {
            Serial.print(" [CHECKSUM MISMATCH]");
        } else {
            digitalWrite(LED_PIN, !digitalRead(LED_PIN));
        }

        Serial.println();
    }
}