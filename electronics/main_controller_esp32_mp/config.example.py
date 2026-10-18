# Copy this file to config.py and fill in the real values. config.py is gitignored.
config = {
    "wifi_ssid": "your-wifi-ssid",
    "wifi_password": "your-wifi-password",
    # MQTT broker (Mosquitto on Azure, TLS, non-standard port, username/password).
    # mqtt_ca_file is the broker's self-signed certificate, checked in as mqtt_ca.pem
    # next to this file — not a secret, safe to commit.
    "mqtt_host": "your-broker-hostname",
    "mqtt_port": 10000,
    "mqtt_user": "your-mqtt-username",
    "mqtt_password": "your-mqtt-password",
    "mqtt_ca_file": "mqtt_ca.pem",
    # The rudder pulse (us) that keeps the boat going straight: the same as the transmitter's
    # rudder trim. The autopilot's rudder commands are shifted by it. 1500 = no trim.
    "rudder_center_us": 1500,
}
