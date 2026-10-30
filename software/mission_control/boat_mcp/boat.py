"""Commands to the boat, behind the MCP tools in server.py.

The boat takes its commands over MQTT (electronics/main_controller_esp32_mp/controller.py):

    {"cmd": "destination", "lat_e7": <int>, "lon_e7": <int>}    go there (degrees * 1e7)
    {"cmd": "clear_destination"}                                 forget it: the boat stops (neutral)

and its telemetry, read here through telemetry_service.TelemetryProvider, echoes the destination
it is using. So a command is "accepted" once a telemetry message received after it was sent
shows the new destination (or none, for stop).
"""

import json
import math
import os
import threading
import time
import uuid
from pathlib import Path


import tracing
from geo import course_between, destination_point  # noqa: F401 (destination_point is used by observe.py too)

COMMAND_TOPIC = "audrey/boat1/cmd"  # MQTT_COMMAND_TOPIC in the boat's mqtt.py
TELEMETRY_TOPIC = "audrey/boat1/telemetry"
GPS_MAX_AGE_S = 5.0  # an older fix isn't a position to navigate from
MIN_DISTANCE_M = 5.0  # the boat counts as arrived within 4 m (gnc.ARRIVAL_RADIUS_M)
MAX_DISTANCE_M = float(os.environ.get("BOAT_MAX_DISTANCE_M") or 300)
CONFIRM_S = 4.0  # how long to wait for telemetry to show the command (telemetry comes once a second)
APP_DIR = Path(__file__).resolve().parent.parent  # software/mission_control


class BoatError(Exception):
    """A command that can't be sent: no fresh position, a distance out of range, no broker."""


class Boat:
    """start_navigation / stop, given a way to publish a command and the telemetry."""

    def __init__(self, publish, telemetry, clock=time.time, sleep=time.sleep, confirm_s=CONFIRM_S, poll_s=0.2):
        self.publish = publish  # publish(command dict): sends it to the boat, raises BoatError if it can't
        self.telemetry = telemetry  # a telemetry_service.TelemetryProvider
        self.clock, self.sleep = clock, sleep
        self.confirm_s, self.poll_s = confirm_s, poll_s

    def position(self):
        """(lat, lon, fix age in seconds) from the newest telemetry with a valid fix."""
        newest = self.telemetry.latest(TELEMETRY_TOPIC, n=1)
        if not newest:
            raise BoatError("no telemetry from the boat: is it on and connected?")
        message = newest[0]
        gps = (message.get("data") or {}).get("gps") or {}
        age = self.clock() - message.get("unix", 0) + (gps.get("age_ms") or 0) / 1000
        if not gps.get("fix_valid") or gps.get("lat_e7") is None or gps.get("lon_e7") is None:
            raise BoatError("the boat has no GPS fix")
        if age > GPS_MAX_AGE_S:
            raise BoatError("the boat's last GPS fix is %.0f s old (more than %.0f s)" % (age, GPS_MAX_AGE_S))
        return gps["lat_e7"] / 1e7, gps["lon_e7"] / 1e7, age

    def _confirmed(self, sent_at, matches):
        """Waits up to confirm_s for telemetry received after sent_at that satisfies matches(data)."""
        deadline = sent_at + self.confirm_s
        while True:
            for message in self.telemetry.since(sent_at, TELEMETRY_TOPIC):
                if matches(message.get("data") or {}):
                    tracing.event("boat-confirmed", after_s=round(message["unix"] - sent_at, 2))
                    return True
            if self.clock() >= deadline:
                tracing.event("boat-confirmed", after_s=None, note="not within %g s" % self.confirm_s)
                return False
            self.sleep(self.poll_s)

    def start_navigation(self, azimuth_deg, distance_m):
        if not MIN_DISTANCE_M <= distance_m <= MAX_DISTANCE_M:
            raise BoatError("distance has to be %g to %g m, not %g" % (MIN_DISTANCE_M, MAX_DISTANCE_M, distance_m))
        azimuth_deg = azimuth_deg % 360
        lat, lon, fix_age = self.position()
        dest_lat, dest_lon = destination_point(lat, lon, azimuth_deg, distance_m)
        sent_at = self.clock()
        command = self.send_destination(dest_lat, dest_lon)
        wanted = {"lat_e7": command["lat_e7"], "lon_e7": command["lon_e7"]}
        accepted = self._confirmed(sent_at, lambda data: data.get("destination") == wanted)
        return {"accepted": accepted, "azimuth_deg": round(azimuth_deg, 1), "distance_m": round(distance_m, 1),
                "start": {"lat": round(lat, 7), "lon": round(lon, 7), "fix_age_s": round(fix_age, 1)},
                "destination": {"lat": round(dest_lat, 7), "lon": round(dest_lon, 7)},
                "note": "the boat is navigating to it" if accepted else
                        "sent, but the boat's telemetry hasn't shown it within %g s" % self.confirm_s}

    def navigate_to(self, lat, lon):
        """Sends the boat straight to (lat, lon), within MIN_DISTANCE_M..MAX_DISTANCE_M of where it
        is, and confirms it from the telemetry as start_navigation does."""
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise BoatError("not a position: %r, %r" % (lat, lon))
        here_lat, here_lon, fix_age = self.position()
        azimuth, distance = course_between(here_lat, here_lon, lat, lon)
        if not MIN_DISTANCE_M <= distance <= MAX_DISTANCE_M:
            raise BoatError("that point is %.0f m away; it has to be %g to %g m" % (distance, MIN_DISTANCE_M, MAX_DISTANCE_M))
        sent_at = self.clock()
        command = self.send_destination(lat, lon)
        wanted = {"lat_e7": command["lat_e7"], "lon_e7": command["lon_e7"]}
        accepted = self._confirmed(sent_at, lambda data: data.get("destination") == wanted)
        return {"accepted": accepted, "azimuth_deg": round(azimuth, 1), "distance_m": round(distance, 1),
                "start": {"lat": round(here_lat, 7), "lon": round(here_lon, 7), "fix_age_s": round(fix_age, 1)},
                "destination": {"lat": round(lat, 7), "lon": round(lon, 7)},
                "note": "the boat is navigating to it" if accepted else
                        "sent, but the boat's telemetry hasn't shown it within %g s" % self.confirm_s}

    def send_destination(self, lat, lon):
        """Sends the boat to (lat, lon) without waiting for it to confirm. Returns the command."""
        command = {"cmd": "destination", "lat_e7": round(lat * 1e7), "lon_e7": round(lon * 1e7)}
        self._send(command)
        return command

    def _send(self, command):
        try:
            self.publish(command)
        except Exception as e:
            tracing.event("boat-command", command=command, error=str(e))
            raise
        tracing.event("boat-command", command=command)

    def stop(self):
        sent_at = self.clock()
        self._send({"cmd": "clear_destination"})
        accepted = self._confirmed(sent_at, lambda data: "destination" in data and data["destination"] is None)
        return {"accepted": accepted,
                "note": "the boat has no destination and holds neutral" if accepted else
                        "sent, but the boat's telemetry hasn't shown it within %g s" % self.confirm_s}


class MqttPublisher:
    """Publishes commands to the boat over the broker (the MQTT_* settings, as for the telemetry
    reader). Connects in the background and reconnects by itself."""

    def __init__(self):
        import paho.mqtt.client as mqtt

        env = os.environ
        missing = [n for n in ("MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PASSWORD", "MQTT_CA_FILE") if not env.get(n)]
        if missing:  # a tool error, not SystemExit: that would take the MCP server down
            raise BoatError("the MQTT broker isn't configured (%s missing)" % ", ".join(missing))
        ca_file = Path(env["MQTT_CA_FILE"])
        self.connected = threading.Event()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="audrey-boat-mcp-" + uuid.uuid4().hex[:8])
        self.client.username_pw_set(env["MQTT_USER"], env["MQTT_PASSWORD"])
        self.client.tls_set(ca_certs=str(ca_file if ca_file.is_absolute() else APP_DIR / ca_file))
        self.client.on_connect = lambda client, userdata, flags, rc, props: self.connected.set()
        self.client.on_disconnect = lambda client, userdata, flags, rc, props: self.connected.clear()
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.connect_async(env["MQTT_HOST"], int(env["MQTT_PORT"]), keepalive=30)
        self.client.loop_start()

    def __call__(self, command):
        if not self.connected.wait(5):
            raise BoatError("not connected to the MQTT broker")
        info = self.client.publish(COMMAND_TOPIC, json.dumps(command), qos=1)
        info.wait_for_publish(5)
        if not info.is_published():
            raise BoatError("the broker didn't take the command")

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()
