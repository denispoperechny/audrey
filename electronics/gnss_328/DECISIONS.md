# Design decisions — GNSS reader on a Pro Mini (ATmega328P)

Created 2026-09-29. Replaces `electronics/heltec_gnss_reader`. The Heltec
board's ESP32-S3 I2C target (Arduino core) failed ~35–40% of reads with an
ESP32-S3 controller (stuck `db db db` state, see the Heltec
`DECISIONS.md`). The 328P's TWI is a simple hardware target that stretches
SCL on its own until the reply byte is loaded.

The GNSS side (L76K, PCAS commands, 9600 / 1Hz, GGA/GSA/RMC, fix-quality
gate, recovery) is ported one to one; the reasons are in the Heltec
project's `DECISIONS.md` and `GNSS_DEBUG.md` and still apply.

## Board

Arduino Pro Mini **3.3V / 8 MHz**. The 5V version would pull the controller's
3.3V bus up to 5V and drive the L76K's RX at 5V.

| Pro Mini | Connects to | Notes |
|---|---|---|
| D0 (RX) | L76K TX | through **1 kΩ**, so the USB-serial adapter wins during uploads |
| D1 (TX) | L76K RX | disconnect it (or accept junk sent to the L76K) during uploads |
| D4 | L76K RST | active low, optional (`GNSS_RESET_PIN`, -1 if not wired) |
| A4 | bus SDA | the pads next to A2/A3 on most boards |
| A5 | bus SCL | |
| D13 | onboard LED | off = no GNSS link, blinking = link but no fix, on = valid fix |
| VCC / GND | 3.3V, shared GND | the onboard regulator is not needed if fed 3.3V on VCC |

`GNSS_POWER_PIN` (default -1) can drive an active-low power switch
(P-MOSFET) for the L76K, like `VGNSS_Ctrl` on the Heltec. Without one,
recovery only pulses RST.

`Wire.begin()` turns on the 328P's internal pull-ups (~30–50 kΩ to 3.3V).
They're weak and harmless next to the bus's external pull-ups.

## I2C protocol: unchanged

Same address `0x6D`, same `FixPacket` v3 (30 bytes), same flags, same CRC, so
the controller's `gps.py` works as is. Differences:

- **`resetReason`** holds the ATmega's `MCUSR` bits (1 = power-on,
  2 = reset pin, 4 = brownout, 8 = hardware watchdog), plus `0x80` = restarted
  by the loop watchdog. On the Heltec it was an `esp_reset_reason_t`.
- `gps.py`'s workaround for the S3's leftover byte isn't needed any more but
  is harmless: the 31st byte reads as `0xFF`, and the packet is found at
  offset 0.
- The request handler runs in the TWI interrupt with SCL stretched. The CRC
  uses a nibble table so the stretch stays short (tens of µs). The controller
  must still allow clock stretching.

## No floats

`double` is 32-bit on AVR: about 7 significant digits, or roughly a metre of
error on a latitude parsed as `ddmm.mmmmm`. Coordinates, altitude, HDOP,
SOG and COG are parsed straight into the packet's fixed-point integers.
Checked on the host against double-precision conversion. No `String`
either: 2 KB of RAM (the build uses ~650 bytes).

## No debug output

The only hardware UART belongs to the L76K. SoftwareSerial was ruled out
for either side because it disables interrupts for ~1 ms per byte at 9600,
which delays the I2C handling. The controller prints everything in the packet
(`gps.py` `describe()`); the LED shows the link/fix state.

## Watchdog

`USE_WATCHDOG` (default 1): 2 s, **interrupt-then-reset** mode. The first
timeout runs `WDT_vect`, which jumps to address 0 and restarts the firmware
without going through the bootloader. This avoids a known Pro Mini trap:
after a *hardware* watchdog reset the watchdog stays on with a 16 ms timeout,
and the stock (ATmegaBOOT) bootloader waits longer than that for an upload,
which causes a reset loop. The hardware reset (second timeout) only happens
if the loop hangs with interrupts disabled. With the stock bootloader that
case can still loop until the board is reflashed. Flashing Optiboot (e.g.
MiniCore) removes that risk. `.init3` code turns the watchdog off first
thing after every reset.

## Simulated GNSS (bench testing without an L76K)

`SIMULATE_GNSS` (currently **1**): once a second the firmware builds
GSA/GGA/RMC sentences for a random walk around 50.0 N / 30.0 E (a few
metres), random sats/HDOP/altitude/SOG/COG, and feeds them through the real
parser, so everything past the UART is exercised. HDOP is sometimes above
2.5 (`FIX_GOOD` clears), and the fix drops out for 5 s every minute
(`FIX_VALID`/`MOTION_VALID` clear, `seq` holds). The UART isn't started and
GNSS recovery is off. `LINK_OK` stays set. **Set it to 0 once the L76K is
wired.**

## Status

Built with PlatformIO (`pio run` in this folder; atmelavr, board
`pro8MHzatmega328`) and parsing/CRC host-tested. **Not yet run on hardware.**
Worth checking on the bench:

- read reliability from the XIAO (the whole point of the swap), then again
  with the hardware I2C instead of soft I2C
- GNSS recovery: unplug the L76K's TX mid-run and check that
  `gnssRecoveries` increments and the fix comes back
- the watchdog: a temporary `while (1) {}` in `loop()` should show up as
  `resetReason` `0x80`
