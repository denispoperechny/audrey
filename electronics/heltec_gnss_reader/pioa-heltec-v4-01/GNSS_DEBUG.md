# Debugging the L76K via raw UART passthrough

`src/main.cpp` is currently a **debug build**: the ESP32 does nothing but
bridge bytes between the USB serial monitor and the L76K's UART. Nothing
is parsed, nothing is sent automatically. This lets you type commands at
the module directly, bypassing all of our own firmware logic, to isolate
whether the module itself responds at all.

The previous "real" application (NMEA parsing, automatic PCAS config) is
saved at `backups/main_nmea_client.cpp.bak` — copy it back over
`src/main.cpp` once the module is confirmed working.

## 1. Flash and connect

```
pio run -t upload
tio /dev/cu.usbmodemXXXX -b 115200
```

(Port name may differ — check with `pio device list`.)

## 2. Baseline: what you should see immediately

As soon as it connects, you should see a continuous, unprompted stream of
NMEA sentences — this is the module's default output, completely raw and
unfiltered now (more sentence types than before, since nothing is being
selectively parsed anymore):

```
$GNGGA,...*hh
$GNGSA,...*hh
$GPGSV,...*hh
$GNRMC,...*hh
$GNVTG,...*hh
```

roughly once per second. **If you see this, the module is alive, powered,
and its TX line to the ESP32 works** — same as before. This part has
never been in question.

## 3. The actual test: does the module hear us?

Type (or paste) the following line into `tio` and press Enter. It slows
the GGA sentence down to once every 3 fixes, leaving everything else
unchanged — same 1Hz rate, same bandwidth, just a different cadence for
one sentence type, so it's easy to tell apart from noise:

```
$PCAS03,3,0,1,0,1,0,0,0,0,0,,,0,0*01
```

**Expected if it works:** `$G*GGA` lines drop from ~once/sec to ~once
every 3 seconds, while `$G*GSA`/`$G*RMC`/etc. keep coming every second.

**Expected if it doesn't:** no change at all — every sentence keeps
appearing at the same rate as before, forever.

There's no acknowledgment reply to wait for — this chip's `$PCAS`
commands aren't acked over NMEA at all, so success is only visible as
this rate change, never as a reply line.

To put it back to normal once you're done:

```
$PCAS03,1,0,1,0,1,0,0,0,0,0,,,0,0*03
```

## 4. Other things worth trying

- **Constellation select** (should never fail silently in an obviously
  bad way — this is already the factory default, so it's a low-stakes
  first thing to type just to see if anything happens):
  ```
  $PCAS04,3*1A
  ```
- **Hot restart** — the most forceful test. If the module hears this, you
  should see a brief gap (roughly a second or so) in the NMEA stream
  before it resumes:
  ```
  $PCAS10,0*1C
  ```

## 5. If nothing ever changes, no matter what you type

That confirms the problem is the physical link on the TX side (ESP32
GPIO38 → module RX), not firmware, not the command protocol, and not
command formatting — a human typing exact, correct commands directly
rules all of that out. At that point it's the connector/cable question
from earlier in the chat: reseating the GNSS module's connector is the
next step, not more code.

## 6. If it *does* work now

That would mean our automatic config in the previous build had a real
bug somewhere (timing, formatting, or something else) — restore
`backups/main_nmea_client.cpp.bak` to `src/main.cpp` and we'll dig into
what was different.

## Note on typing commands into `tio`

Commands above are copy-paste-ready with checksums already computed. If
pressing Enter doesn't seem to send a full line the module accepts, it's
worth trying `tio`'s line-ending mapping options — some terminals send a
bare `\r` on Enter when the module's parser wants `\r\n`.
