from machine import Pin, I2C
import _thread
import binascii
import machine
import math
import network
import ntptime
import ssl
import struct
import time

try:
    import ujson as json
except ImportError:
    import json

from umqtt.simple import MQTTClient

from config import config

I2C_TARGET_ADDRESS = 0x31
I2C_SDA_PIN = 21
I2C_SCL_PIN = 22
SEND_INTERVAL_MS = 50  # 20 Hz; the Pico treats a command older than 200 ms as lost
REPORT_INTERVAL_MS = 1000
WAVE_PERIOD_MS = 10000  # one full sin/cos cycle
WAVE_AMPLITUDE = 100
LED_PIN = 2  # most ESP32 WROOM dev boards route the onboard LED to GPIO2

# WiFi runs in its own thread: connecting blocks for seconds, which would starve the
# 20 Hz command loop and trip the Pico's failsafe.
WIFI_CHECK_INTERVAL_MS = 1000
WIFI_CONNECT_TIMEOUT_MS = 15000

# MQTT also runs in its own thread, separate from the WiFi thread: the TLS handshake
# alone blocks for several hundred ms, which would starve the I2C loop just as badly.
MQTT_CLIENT_ID = b"audrey-esp32-" + binascii.hexlify(machine.unique_id())
MQTT_TELEMETRY_TOPIC = b"audrey/boat1/telemetry"
MQTT_COMMAND_TOPIC = b"audrey/boat1/cmd"
MQTT_KEEPALIVE_S = 30
MQTT_PUBLISH_INTERVAL_MS = 1000  # one telemetry message per second
MQTT_POLL_INTERVAL_MS = 200  # how often check_msg() looks for an incoming command
MQTT_RECONNECT_DELAY_MS = 2000

# Pico register map (register-addressed I2C target, little-endian).
REG_CMD = 0x00
CMD_FORMAT = "<bbBBB"  # throttle, rudder, flags, seq, checksum
REG_STATUS = 0x10
STATUS_LEN = 16
# heartbeat, flags, battery mV, rc mode/thr/rud us, out thr/rud us, overruns, cmd errors
STATUS_FORMAT = "<BBHHHHHHBB"

FLAG_ESP32_MODE = 0x01
FLAG_CMD_FRESH = 0x02
FLAG_FAILSAFE = 0x40

# Latest outgoing I2C command + Pico status, written by the main loop and read by the
# MQTT thread once a second for telemetry. A plain dict assignment is atomic enough
# under MicroPython's GIL; no lock needed for these simple field updates.
link_state = {
    "throttle": 0,
    "rudder": 0,
    "seq": 0,
    "write_status": "",
    "pico": None,  # dict of the last-decoded Pico status block, or None
}

# Latest incoming MQTT command, written by the MQTT callback. Nothing consumes this yet
# to drive the actual servo outputs -- see the note in mqtt_on_message().
command_state = {"count": 0, "raw": b"", "stamp": 0}


def compute_checksum(throttle, rudder, flags, seq):
    return ((throttle & 0xFF) + (rudder & 0xFF) + flags + seq) & 0xFF


def read_status(i2c):
    """Return the Pico status tuple, or None if the block's checksum doesn't match."""
    block = i2c.readfrom_mem(I2C_TARGET_ADDRESS, REG_STATUS, STATUS_LEN + 1)
    if sum(block[:STATUS_LEN]) & 0xFF != block[STATUS_LEN]:
        return None
    return struct.unpack(STATUS_FORMAT, block[:STATUS_LEN])


def wifi_connect(wlan):
    """Return True once connected, False if it didn't connect in time."""
    if wlan.isconnected():
        return True
    wlan.connect(config["wifi_ssid"], config["wifi_password"])
    start = time.ticks_ms()
    while not wlan.isconnected():
        if time.ticks_diff(time.ticks_ms(), start) > WIFI_CONNECT_TIMEOUT_MS:
            return False
        time.sleep_ms(100)
    return True


def wifi_loop():
    """Keep the station connected, reconnecting if the link drops."""
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    was_connected = False
    while True:
        try:
            connected = wifi_connect(wlan)
        except Exception as e:  # keep the thread alive across any WiFi error
            connected = False
            print("wifi error: %s: %s" % (type(e).__name__, e))
        if connected != was_connected:
            was_connected = connected
            print("wifi %s" % ("connected " + wlan.ifconfig()[0] if connected else "disconnected"))
            if connected:
                # The board's clock starts at year 2000 on every boot, which is before
                # our TLS cert's validity start; sync it or certificate checks fail.
                try:
                    ntptime.settime()
                    print("time synced:", time.gmtime())
                except Exception as e:
                    print("ntp sync failed: %s: %s" % (type(e).__name__, e))
        time.sleep_ms(WIFI_CHECK_INTERVAL_MS)


def mqtt_ssl_params():
    """Best-effort TLS setup: verify against our CA file if the firmware supports it,
    otherwise fall back to an encrypted-but-unverified connection rather than failing."""
    try:
        with open(config["mqtt_ca_file"], "rb") as f:
            ca = f.read()
    except OSError:
        print("mqtt: CA file %s not found on device; connecting without cert verification" % config["mqtt_ca_file"])
        return {}
    try:
        return {"cert_reqs": ssl.CERT_REQUIRED, "cadata": ca, "server_hostname": config["mqtt_host"]}
    except AttributeError:
        print("mqtt: this build's ssl module can't verify a CA; connecting without cert verification")
        return {}


def mqtt_on_message(topic, msg):
    """Callback for an incoming command message. Only records + prints it for now --
    nothing wires this into the actual throttle/rudder output yet. Driving the real
    servos from a cloud message is a deliberate follow-up step, not an accidental one."""
    command_state["count"] += 1
    command_state["raw"] = msg
    command_state["stamp"] = time.ticks_ms()
    print("mqtt rx #%d %s: %r" % (command_state["count"], topic, msg))


def mqtt_connect():
    client = MQTTClient(
        MQTT_CLIENT_ID,
        config["mqtt_host"],
        port=config["mqtt_port"],
        user=config["mqtt_user"],
        password=config["mqtt_password"],
        keepalive=MQTT_KEEPALIVE_S,
        ssl=True,
        ssl_params=mqtt_ssl_params(),
    )
    client.set_callback(mqtt_on_message)
    client.connect()
    client.subscribe(MQTT_COMMAND_TOPIC)
    return client


def publish_telemetry(client):
    payload = json.dumps(
        {
            "throttle": link_state["throttle"],
            "rudder": link_state["rudder"],
            "seq": link_state["seq"],
            "i2c": link_state["write_status"],
            "pico": link_state["pico"],
        }
    )
    client.publish(MQTT_TELEMETRY_TOPIC, payload)


def mqtt_loop():
    """Connect, subscribe, and alternate between polling for a command and publishing
    telemetry once a second. Any failure drops the client and reconnects after a delay,
    so a broker restart or a WiFi blip doesn't kill the thread."""
    wlan = network.WLAN(network.STA_IF)
    client = None
    last_publish = time.ticks_ms()
    while True:
        try:
            if client is None:
                if not wlan.isconnected():
                    time.sleep_ms(500)
                    continue
                client = mqtt_connect()
                print("mqtt connected to %s:%d as %s" % (config["mqtt_host"], config["mqtt_port"], MQTT_CLIENT_ID))

            client.check_msg()  # non-blocking; runs mqtt_on_message() if a command arrived

            now = time.ticks_ms()
            if time.ticks_diff(now, last_publish) >= MQTT_PUBLISH_INTERVAL_MS:
                last_publish = time.ticks_add(last_publish, MQTT_PUBLISH_INTERVAL_MS)
                publish_telemetry(client)

            time.sleep_ms(MQTT_POLL_INTERVAL_MS)
        except Exception as e:
            print("mqtt error: %s: %s" % (type(e).__name__, e))
            if client is not None:
                try:
                    client.disconnect()
                except Exception:
                    pass
                client = None
            time.sleep_ms(MQTT_RECONNECT_DELAY_MS)


i2c = I2C(0, sda=Pin(I2C_SDA_PIN), scl=Pin(I2C_SCL_PIN), freq=100000)  # controller mode
led = Pin(LED_PIN, Pin.OUT)

_thread.start_new_thread(wifi_loop, ())
_thread.start_new_thread(mqtt_loop, ())

seq = 0
now = time.ticks_ms()
last_send_time = now
last_report_time = now

while True:
    now = time.ticks_ms()
    if time.ticks_diff(now, last_send_time) < SEND_INTERVAL_MS:
        time.sleep_ms(1)  # yield so the other threads get to run
        continue
    last_send_time = time.ticks_add(last_send_time, SEND_INTERVAL_MS)

    phase = 2 * math.pi * (now % WAVE_PERIOD_MS) / WAVE_PERIOD_MS
    throttle = round(WAVE_AMPLITUDE * math.cos(phase))
    rudder = round(WAVE_AMPLITUDE * math.sin(phase))
    flags = 0
    seq = (seq + 1) & 0xFF  # the Pico drops a frame whose seq didn't change
    checksum = compute_checksum(throttle, rudder, flags, seq)

    payload = struct.pack(CMD_FORMAT, throttle, rudder, flags, seq, checksum)
    try:
        i2c.writeto_mem(I2C_TARGET_ADDRESS, REG_CMD, payload)
        write_status = "ok"
    except OSError as e:
        write_status = "write failed (%s)" % e

    link_state["throttle"] = throttle
    link_state["rudder"] = rudder
    link_state["seq"] = seq
    link_state["write_status"] = write_status

    if time.ticks_diff(now, last_report_time) >= REPORT_INTERVAL_MS:
        last_report_time = time.ticks_add(last_report_time, REPORT_INTERVAL_MS)
        led.value(not led.value())

        line = "thr=%d rud=%d seq=%d: %s" % (throttle, rudder, seq, write_status)
        try:
            status = read_status(i2c)
        except OSError as e:
            line += " | status read failed (%s)" % e
            link_state["pico"] = None
        else:
            if status is None:
                line += " | status checksum mismatch"
                link_state["pico"] = None
            else:
                heartbeat, sflags, battery_mv, _, _, _, out_thr, out_rud, overruns, cmd_errors = status
                link_state["pico"] = {
                    "heartbeat": heartbeat,
                    "esp32_mode": bool(sflags & FLAG_ESP32_MODE),
                    "cmd_fresh": bool(sflags & FLAG_CMD_FRESH),
                    "failsafe": bool(sflags & FLAG_FAILSAFE),
                    "battery_mv": battery_mv,
                    "out_throttle_us": out_thr,
                    "out_rudder_us": out_rud,
                    "overruns": overruns,
                    "cmd_errors": cmd_errors,
                }
                line += " | pico: hb=%d batt=%dmV %s%s%s out=%d/%dus overruns=%d cmd_errors=%d" % (
                    heartbeat,
                    battery_mv,
                    "ESP32" if sflags & FLAG_ESP32_MODE else "RC",
                    " cmd_fresh" if sflags & FLAG_CMD_FRESH else "",
                    " FAILSAFE" if sflags & FLAG_FAILSAFE else "",
                    out_thr,
                    out_rud,
                    overruns,
                    cmd_errors,
                )
        # print(line)
