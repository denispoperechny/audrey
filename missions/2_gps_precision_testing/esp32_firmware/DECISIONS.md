# Design decisions — GNSS firmware

Last updated: 2026-09-07. This explains *why* `src/main.cpp` is built the
way it is, so it can be rebuilt, repurposed, or debugged later without
re-deriving everything from scratch. For hands-on GNSS debugging
procedure, see `GNSS_DEBUG.md`.

## What this is

Firmware for a Heltec WiFi LoRa 32 V4 (ESP32-S3) driving its onboard L76K
GNSS module, for real-time position/heading on a 50cm RC boat. Currently
it just reads and prints fixes over USB serial — no navigation logic yet.

## Hardware

- **Board**: Heltec WiFi LoRa 32 V4 (ESP32-S3, native USB-CDC — no
  separate USB-UART bridge chip).
- **GNSS module**: Quectel L76K, chipset **Airoha/CASIC AT6558R**
  (confirmed from the module's own boot banner: `IC=AT6558R-5N-32-1C580901`,
  `MA=CASIC`). This is *not* a MediaTek MT3333-family chip, despite "L76"
  branding — see "The PMTK vs PCAS mistake" below.
- **Connection**: the GNSS module attaches via a separate SH1.25 8-pin
  connector/cable, not a soldered-down chip. Pin mapping (verified against
  Heltec's official V4 datasheet/pinmap, not just `src/gnss_info.txt`):

  | GPIO | Function | Notes |
  |------|----------|-------|
  | 34 | `VGNSS_Ctrl` | Active-low power switch for the GNSS 3V3 rail |
  | 39 | `GNSS_TX` | Module's TX → ESP32 RX |
  | 38 | `GNSS_RX` | ESP32 TX → module's RX (shares IO_MUX with the onboard WS2812 RGB LED, unused here) |
  | 40 | `GNSS_Wakeup` | Read-only in this firmware |
  | 41 | `GNSS_PPS` | Read-only in this firmware, unused |
  | 42 | `GNSS_RST` | Driven high (released) at boot, never asserted |

## The PMTK vs PCAS mistake (and why it matters going forward)

The first version of this firmware configured the module using `$PMTK...`
commands, which is what nearly every "L76" tutorial online uses. **This
silently did nothing** — the module never NACKs an unrecognized command
prefix, it just discards it. Confirmed from Quectel's own *L76K GNSS
Protocol Specification*: this chip speaks `$PCAS...` (CASIC proprietary)
commands instead. If you're ever extending this firmware's GNSS config,
**do not reach for PMTK commands or tutorials that assume MTK chipsets**
— check the command against the L76K spec's PCAS command list first.

Commands actually available at the plain-text PCAS layer (all that's
used here): `PCAS01` (baud), `PCAS02` (fix interval), `PCAS03` (per-
sentence output rate), `PCAS04` (constellation select), `PCAS10`
(restart). Things like SBAS/DGPS, interference cancellation, or a
dynamics/nav-mode setting — all things a PMTK-based module would expose —
**are not available at this layer** on the L76K. They'd require the
separate binary CASIC protocol (different framing, different checksum
algorithm), which this firmware deliberately does not attempt — not
worth the added risk/complexity for the accuracy gain.

**PCAS commands are never acknowledged over NMEA.** There is no ACK/NACK
reply sentence for this chip's text-command layer (unlike PMTK's
`$PMTK001`). The only way to confirm a command took effect is to observe
its behavioral result (e.g. watch the output rate actually change).

## Why `configureGNSS()` asserts values that are already the default

The AT6558R **persists PCAS-set values across resets** on its own (no
explicit "save config" command needed, unlike some other GNSS chipsets).
This was discovered the hard way: an earlier debugging session sent a
temporary "slow GGA down" test command, and it silently survived a full
firmware reflash and several power cycles, because the module remembered
it internally rather than reverting to any default.

Consequence: **never assume the module is in its factory-default state.**
`configureGNSS()` explicitly re-asserts every value it depends on, every
boot, even when the value matches the factory default:

- `PCAS04,3` — GPS + BeiDou constellations.
- `PCAS02,1000` — 1Hz fix rate.
- `PCAS03,1,0,1,0,1,0,0,0,0,0,,,0,0` — GGA/GSA/RMC output every fix,
  everything else (GLL/GSV/VTG/ZDA/ANT) off. This matches what
  `readGNSS()` actually parses; no bandwidth wasted on unparsed sentences.

If a future debugging session sends a one-off test command by hand (see
`GNSS_DEBUG.md`), it can safely leave the module in a weird state —
the next reboot of this firmware puts it back.

## Why baud rate and fix rate are NOT raised above 9600 / 1Hz

Faster real-time fixes would genuinely help boat navigation. The L76K
spec is explicit, though: going below a 1000ms fix interval *requires*
simultaneously raising the baud rate (to 115200) and trimming NMEA
output to a single sentence — three things changing together, one of
which (baud) is the single easiest way to silently lose the UART link
if something's off (mismatched baud = total silence, hard to
distinguish from a dead connection).

Given how expensive debugging this link already turned out to be (see
below), this was deliberately deferred rather than bundled into the same
change as everything else. **Fix rate/baud increase is a good next
step**, but do it as its own isolated, verifiable change — reuse
`GNSS_DEBUG.md`'s passthrough method to confirm each piece (baud change,
then interval change) works before combining them.

## The fix-quality gate

`handleGGA()` doesn't just check `fixQuality != 0` — it also checks
`satellites >= 4` and the latest HDOP (from `handleGSA()`) against 2.5,
the usual "good enough for navigation" rule of thumb. Printed fixes are
tagged `OK` or `LOW-QUALITY` accordingly. This exists because a fix can
be technically valid (quality=1) while still being noisy — worth
distinguishing before any navigation logic trusts a position.

`handleRMC()` separately captures speed-over-ground and course-over-
ground (only when the sentence reports itself valid, i.e. status field
`A`), since a boat needs heading/speed, not just position.

## Known gap: no stale-fix detection

Nothing in this firmware tracks *when* the last good fix arrived. If the
module hangs, the connector works loose, or the antenna loses sky view
mid-run, `readGNSS()` simply stops printing new fixes — there's no signal
anywhere that says "the last position we have is now N seconds old, stop
trusting it." For a passive logger that's a minor annoyance; for
anything that steers or throttles the boat off this data, it's a real
safety gap — a control loop with no staleness check will happily keep
acting on a last-known position that's actually long stale.

Not yet implemented. The fix: timestamp every accepted `GGA`/`RMC` fix
(`millis()` at parse time) and expose a simple staleness check (e.g.
"no good fix in the last 3s") that any future navigation logic can query
before trusting `lastHdop`/position/speed/course. Cheap to add — flagged
here so it isn't forgotten before this firmware drives anything.

## Two USB-CDC quirks worth knowing about

This board's USB is native CDC (part of the ESP32-S3 itself), not a
separate USB-UART bridge chip. Two consequences baked into this
firmware:

1. **A reset re-enumerates the USB port on the host side.** `setup()`
   waits (bounded, 2s) for `Serial` to report a host connection before
   proceeding, so early boot output isn't lost into a CDC endpoint
   nobody's listening on yet. Without this, a serial monitor reconnecting
   after a reset would miss everything printed in the first ~1s of boot.
2. **`Serial.printf(...)` needs an explicit `\r\n`, not just `\n`.**
   Arduino's `Serial.println()` appends `\r\n` automatically, but
   `printf()` doesn't — a bare `\n` produces a "staircase" effect in raw
   serial terminals (line feed without carriage return). Every `printf`
   in this file ends in `\r\n` for this reason.

## Debugging history (why some things look defensive)

Getting this firmware to a working state took a long detour that's worth
recording so it isn't repeated:

1. Config commands appeared to do nothing → assumed wrong checksum, then
   assumed dead TX wiring, then found (correctly) that the protocol
   itself was wrong (PMTK vs PCAS, see above).
2. After fixing the protocol, a test still appeared to show no effect →
   this pointed at the GNSS connector/cable (a real, physically separate
   SH1.25 connector on this board, a plausible failure point) and nearly
   led to reseating/inspecting it.
3. Before touching hardware, `src/main.cpp` was temporarily replaced with
   a raw UART passthrough (`backups/main_pass_through_uart_proxy.cpp.bak`)
   to manually send commands and rule out firmware entirely.
4. Turned out TX had been working the whole time — the very first
   protocol-corrected test *did* succeed, but was judged from only ~2
   lines of output, nowhere near enough to see a 3x rate-change signature.
   Combined with setting-persistence (see above), a stale leftover state
   from that first success looked identical to "still broken" in the next
   session.
5. Confirmed conclusively by watching the fix-rate change live, mid-
   stream, in one continuous capture (send command → watch the exact
   moment the pattern changes) rather than comparing two separate
   before/after sessions.

Lesson baked into this codebase: **when testing a GNSS config change,
capture a live, continuous, multi-sample before/after in one session** —
not two short separate ones — and remember the module may already be in
a non-default state from a previous test.

## Where things live

- `src/main.cpp` — current firmware (NMEA-parsing application).
- `backups/main_nmea_client.cpp.bak` — same application, saved mid-
  debugging (pre-dates the defensive `PCAS02`/`PCAS03` re-assertions
  described above).
- `backups/main_pass_through_uart_proxy.cpp.bak` — the raw UART bridge
  used for manual debugging; see `GNSS_DEBUG.md` for how to use it again.
- `GNSS_DEBUG.md` — step-by-step for talking to the module directly.
- `src/gnss_info.txt` — original pin notes (superseded/confirmed by the
  table above, kept for reference).

There's no git repo here yet, which is why working states are preserved
as plain file backups rather than commits — worth setting up `git init`
if this project keeps growing.

## Ideas for later, deliberately not done now

- Raise fix rate + baud together (see above) for tighter real-time
  control loop response.
- SBAS/DGPS, interference cancellation, or an explicit dynamics/nav mode
  — would require the binary CASIC protocol, not attempted here.
- Position averaging / outlier rejection using the HDOP already being
  captured, once real navigation logic exists.
- Consider `git init` for this project so future changes don't rely on
  manual `.bak` files for safety.
