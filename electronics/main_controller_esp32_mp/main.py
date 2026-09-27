"""Boot entry point: runs the controller (controller.py).

Kept tiny on purpose. Everything else is uploaded precompiled as .mpy (see deploy.sh):
compiling source on the board grows the MicroPython heap into the RAM that WiFi and the
MQTT TLS handshake need, and it doesn't give it back (compiling the controller alone
cost ~60 KB and made TLS fail with ENOMEM).

This is also the last line of defense: anything that escapes the controller -- a bug in
the loop itself, or a missing/broken controller.mpy -- is printed, the Pico is commanded
neutral, and the chip resets after FATAL_RESET_DELAY_MS (Ctrl+C in that window stops it).
"""

import sys
import time

import machine

FATAL_RESET_DELAY_MS = 5000


def fatal(e):
    print("FATAL: %s: %s -- resetting in %d ms (Ctrl+C to stop)" % (type(e).__name__, e, FATAL_RESET_DELAY_MS))
    sys.print_exception(e)
    try:
        from bcu import BodyControlUnit
        from controller import make_i2c

        BodyControlUnit(make_i2c()).send(0, 0)
    except Exception:
        pass
    time.sleep_ms(FATAL_RESET_DELAY_MS)
    machine.reset()


try:
    import controller

    controller.run()
except KeyboardInterrupt:
    print("stopped by Ctrl+C")
except Exception as e:
    fatal(e)
