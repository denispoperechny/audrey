# Main controller — memory findings and options

Status as of 2026-09-27. Picks up where the controller rebuild left off.

## Update 2026-09-29 — moved to XIAO ESP32-S3 (branch feature/s3_adaptation)

The memory problem is solved by hardware: a Seeed XIAO ESP32-S3 with 8 MB PSRAM
(MicroPython 1.29 `ESP32_GENERIC_S3-SPIRAM_OCT`). MQTT over TLS runs with ~8 MB free.
Everything below the next heading is the original WROOM investigation.

State at the end of the day:
- **XIAO:** runs the controller (board auto-detected in `controller.py`). I2C on D4/D5
  as **software I2C at 50 kHz**: on the bench wiring, the S3's hardware I2C failed at
  every speed with compass + Heltec on the bus. Console/uploads over UART via a CH340 on
  D6/D7 (`./deploy.sh /dev/cu.usbserial-XXX`); MicroPython's UART REPL is on by default.
- **Compass:** works alongside the Heltec at 50 kHz soft I2C; occasional stale seconds.
- **GPS (Heltec):** packet v3 (30 bytes; read 31, accept offset 0/1), Heltec I2C moved
  to GPIO3/4. The Heltec's ESP32-S3 I2C *target* (Arduino core) still fails ~35–40% of
  raw reads (stuck `db db db` state); the controller polls at 5 Hz with a 2 s lost
  window, which rides it out (fix valid every second in the last check). Details and
  what was tried: `electronics/heltec_gnss_reader/pioa-heltec-v4-01/DECISIONS.md`.
- **The Heltec currently runs an experimental pre-load firmware** (performs the same as
  v3). Its source was reverted to the committed v3; reflash it in bootloader mode
  (hold PRG, press RST, release PRG; `pio run -t upload`) to match the repo.
- **Pico:** not connected during these tests.

Next steps to consider:
1. Get rid of the Heltec I2C target flakiness: MicroPython `machine.I2CTarget` on the
   Heltec, or wire the L76K GPS directly to the XIAO's UART (UART1 on D0/D1), or to the
   Pico (a proven I2C target).
2. Fix the bus-recovery rule in `controller.py`: only real bus faults (timeouts / a line
   held low) should trigger it, not a device that doesn't answer.
3. Reconnect the Pico and test all three devices; then revisit hardware I2C with
   shorter wires / 4.7 kΩ pull-ups.
4. Commit this branch's changes.

## TL;DR

The new controller runs on the ESP32 WROOM and survives failures, but **MQTT can't
connect: the TLS handshake fails with `ENOMEM`**. The chip runs out of *system* RAM.
The cause is the MicroPython heap growing into it, mostly because of the BNO08x
compass driver. A decision is needed between the options below (recommended: **B**,
UART-RVC compass).

## Where things stand

| Part | State |
|---|---|
| Control loop (`controller.py`) | Running; tasks isolated, never stalled during testing |
| Compass (BNO08x, I2C 0x4B) | Working; calibration accuracy stays 0 (see open issues) |
| GPS (Heltec board, I2C 0x6D) | Working, fixes arrive at 1 Hz |
| Pico / body control unit (I2C 0x31) | Not answering during the last session (probably unplugged); bus recovery backs off as designed |
| WiFi | Connects |
| MQTT (TLS) | **Fails: `OSError: [Errno 12] ENOMEM`** |

Nothing from this session is committed yet:
- New: `controller.py`, `bcu.py`, `mqtt.py`, `deploy.sh`, this file
- Changed: `main.py` (now a small starter), `compass.py` (freshness tracking),
  `bno08x.py` (buffer size), `.gitignore` (`build/`, `__pycache__/`)
- `main_backup.py`: the old MQTT/Pico controller, kept for reference

## Code structure (new)

| File | Role |
|---|---|
| `main.py` | Tiny starter: runs `controller.run()`; last-resort handler (print, neutral, reset) |
| `controller.py` | Scheduler + `Controller`: control 20 Hz, compass 10 Hz, GPS 2 Hz, Pico status / MQTT / report 1 Hz |
| `compass.py` | `Compass`: heading, calibration auto-save, freshness (`fresh(now, max_age_ms)`) |
| `gps.py` | `GpsLink`: reads the Heltec FixPacket, CRC/version check, freshness |
| `bcu.py` | `BodyControlUnit`: command frames to the Pico, status block back |
| `mqtt.py` | `MqttLink`: WiFi + MQTT threads, bounded command and telemetry queues |
| `deploy.sh` | Compiles modules to `.mpy`, uploads with `main.py`, removes stale `.py`, resets |

Resiliency built in:
- every task in its own try/except; errors counted and rate-limited in the log
- no catch-up bursts
- stale data means neutral
- compass re-init after 3 s without new headings, at most every 10 s
- I2C bus recovery with backoff (5 s → 60 s)
- MQTT optional
- production-mode watchdog (8 s) behind a 3 s boot window
- `main.py` resets on anything that escapes

`decide()` always returns neutral for now: navigation (`gnc.py`) isn't wired in, and
MQTT commands are parsed and recorded but don't drive anything yet.

Deploy: `./deploy.sh [port]` (needs `pip install "mpy-cross==1.29.*"`, matching the
board's MicroPython 1.29). Console: `mpremote connect /dev/cu.usbserial-XXXX repl`.

## How the memory works on this chip

The WROOM has no PSRAM. Two pools matter:

- **MicroPython heap:** starts at ~56 KB. When it runs short, it grows by taking a
  large block of system RAM, roughly doubling at once, and it **never gives it back**.
- **System RAM** (ESP-IDF heap): WiFi and the **TLS handshake** allocate here. TLS
  needs roughly **35–45 KB, largely contiguous**.

Every KB the heap grabs is gone for TLS. The report line now shows both pools:
`mem: heap 52/117 KB free, sys 11 KB free (largest 8 KB)`. The same values are in
telemetry under `memory`.

## What was measured

All numbers are from the board (MicroPython 1.29, ESP32_GENERIC).

| Step | System RAM free | Note |
|---|---|---|
| Fresh boot | ~84–92 KB (largest block 69 KB) | |
| Import `bno08x.py` **as source** | 91 → 31 KB | compiling a 59 KB source file on the board |
| Compile `main.py` (old ~17 KB version) as source | 84 → 25 KB | same effect |
| Import all modules as **`.mpy`** | 84 → 84 KB | precompiling removes the compile cost |
| `BNO08X()` init with heap still small | 84 → 25 KB | heap doubled 56 → 112 KB to fit the driver's 4 KB buffer (heap had 11.6 KB free, largest piece ~2.3 KB) |
| Steady state, full controller | **11 KB free, largest 8–10 KB** | heap ~118 KB total, ~52 KB of it unused |
| TLS connected first, then compass init | 56 → 5 KB | TLS connected, but compass init took the rest; the first publish died |

Other points:
- Plain I2C reads, even 300 bytes, cost no system RAM. The driver's own allocations
  (bytecode ~28 KB, buffers, parsing) push the heap into a growth step.
- In one isolated run (heap already grown), the whole driver init cost nothing. So it's
  the *growth step* that hurts, and it depends on heap state at that moment.

## Already done (helped, not enough)

1. **Precompiled everything to `.mpy`** (`deploy.sh`), and moved the controller out of
   `main.py`. This saves ~60 KB per large source file.
2. **Started WiFi before anything memory-hungry,** and imported `compass` lazily.
3. **`bno08x.py`: `DATA_BUFFER_SIZE` 4096 → 512.** The largest packet seen is the
   ~276-byte startup advertisement; Adafruit's original driver uses 512.
4. **Tried TLS-first ordering.** It connects, but the connection doesn't survive compass
   init.

## Options

### A. Custom MicroPython firmware with our modules frozen in
Build MicroPython for ESP32_GENERIC with a manifest that freezes `bno08x`, `compass`,
`gps`, `bcu`, `mqtt` and `controller`. Frozen bytecode runs from flash and takes
almost no heap.
- **Pros:** the standard fix for no-PSRAM ESP32s. Should free ~50+ KB, and TLS should
  fit comfortably. Keeps the current hardware and wiring. Helps with future code growth
  too.
- **Cons:** needs a firmware build (ESP-IDF, easiest via Docker) and a flash with
  `esptool`. Every code change means a rebuild and reflash, unless you keep developing
  modules as `.mpy` and freeze only the stable ones.

### B. BNO085 in UART-RVC mode (recommended)
Set the breakout's mode pins (PS0 high, PS1 low per the BNO08x datasheet; check your
breakout's labeling) and connect its TX to a free ESP32 UART RX pin. In this mode it
streams heading/pitch/roll at 100 Hz as fixed 19-byte frames. The parser is ~50
lines, and the 59 KB driver goes away.
- **Pros:** removes the heaviest, most fragile code. Takes the compass off the I2C bus
  the Pico shares (no clock-stretching risk). No protocol state, so recovery is just
  "keep reading". Likely enough on its own for TLS to fit.
- **Cons:** wiring change. RVC gives heading/pitch/roll and acceleration only, no
  calibration status/save commands (its own calibration keeps running). Needs a free
  UART: UART2 (GPIO16/17) is free since the GPS moved to I2C via the Heltec board.
- **Check before committing:** in RVC mode, whether the heading uses the magnetometer
  (absolute) or is gyro-only.

### C. MQTT without TLS
- **Pros:** a config change; frees ~40 KB.
- **Cons:** credentials and telemetry sent unencrypted over the network. Not
  recommended beyond a quick test.

### D. ESP32 with PSRAM
For example WROVER, or an ESP32-S3 N8R2 (the Heltec V4 is an S3). Use the SPIRAM
firmware build.
- **Pros:** megabytes of RAM; the problem disappears.
- **Cons:** hardware swap. Pins differ, especially on the S3.

## Suggested next steps

1. Pick an option. With B:
   - rewire the BNO085 for UART-RVC
   - write `compass_rvc.py` with the same interface as `Compass`:
     `update(now)`, `heading`, `fresh()`, `describe()`
   - switch `controller.try_init_compass()` over to it
   - redeploy and watch the `mem:` line: TLS needs system free ≥ ~45 KB with a large
     largest block
2. Reconnect the Pico and confirm that `bcu:` shows `ok` and that status reads work.
3. Commit the controller work (see file list above).
4. Consider option A later anyway, as the codebase grows.

## Open issues noticed along the way

- **Compass calibration accuracy never rises above 0.** The driver only updates it
  from magnetometer reports, which aren't enabled. Enabling `BNO_REPORT_MAGNETOMETER`
  would fix it (moot with option B).
- **Heading direction and zero** haven't been verified against a real compass
  (`HEADING_SIGN` / `HEADING_OFFSET_DEG` in `compass.py`).
- **Intermittent GPS read failures** seen earlier. `GpsLink` now counts them by cause
  (i2c / crc / version); check the counters once things run longer.
- **Telemetry** is ~1.4 KB of JSON per second. Fine for now; trim it if memory stays
  tight.
