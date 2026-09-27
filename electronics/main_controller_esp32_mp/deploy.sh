#!/bin/sh
# Compiles the controller modules to .mpy and uploads them with main.py, then resets.
#
#   ./deploy.sh [port]      # port defaults to the first /dev/cu.usbserial-*
#
# Why .mpy: compiling source on the board grows the MicroPython heap into the RAM that
# WiFi and the MQTT TLS handshake need (see main.py). Needs mpy-cross matching the
# board's MicroPython (1.29):  pip install "mpy-cross==1.29.*"
# config.py, boot.py and mqtt_ca.pem are left as they are on the board.
set -e
cd "$(dirname "$0")"

MODULES="bno08x compass gps bcu mqtt controller"
PORT="${1:-$(ls /dev/cu.usbserial-* 2>/dev/null | head -1)}"
MPY_CROSS="${MPY_CROSS:-mpy-cross}"

if [ -z "$PORT" ]; then
    echo "no serial port found; pass one: ./deploy.sh /dev/cu.usbserial-XXXX" >&2
    exit 1
fi
"$MPY_CROSS" --version

mkdir -p build
CP_ARGS=""
for m in $MODULES; do
    "$MPY_CROSS" -march=xtensawin -o "build/$m.mpy" "$m.py"
    CP_ARGS="$CP_ARGS cp build/$m.mpy :$m.mpy +"
done

# A .py next to a .mpy of the same name wins on import, so remove stale source copies.
REMOVE="import os
for m in '$MODULES'.split():
    try:
        os.remove(m + '.py')
        print('removed', m + '.py')
    except OSError:
        pass"

# shellcheck disable=SC2086
mpremote connect "$PORT" $CP_ARGS cp main.py :main.py + exec "$REMOVE" + reset
echo "deployed to $PORT; watch with: mpremote connect $PORT repl"
