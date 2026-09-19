# Uploading over UART (GP0/GP1) with mpremote

`boot.py` mirrors the REPL onto UART0 (GP0 = TX, GP1 = RX, 115200) with `os.dupterm`, so
the same two pins carry the REPL, app output and code uploads.

## Setup
- USB-serial adapter: adapter RX -> GP0, adapter TX -> GP1, GND -> GND. Logic level must be
  3.3 V. The Pico needs its own power.
- `brew install mpremote`
- Ports: `ls /dev/cu.usb*` (CH340 = `/dev/cu.usbserial-210`, Pico USB = `/dev/cu.usbmodem2101`)

## Commands
    mpremote connect /dev/cu.usbserial-210 cp main.py :main.py   # upload
    mpremote connect /dev/cu.usbserial-210 reset                 # restart the app
    mpremote connect /dev/cu.usbserial-210 repl                  # output / REPL

An upload stops the running app; run `reset` afterwards to start it again.

## How it works
Ctrl+C over the dupterm UART does not interrupt a running script, so `mpremote` can't get
into raw REPL. `main.py` polls `sys.stdin` (USB + UART) every loop pass and reads what is
pending. Reading a Ctrl+C raises `KeyboardInterrupt`, which ends the script and drops to
the REPL. Other bytes come back as text from `read_input()` (currently echoed as `rx: ...`).

## Rules
- Keep calling `read_input()` regularly in any real loop. Long blocking calls delay the exit.
  Don't catch `KeyboardInterrupt` around it. UART data must be text: a `0x03` byte is Ctrl+C.
- A hung or crashed app can't be interrupted. Recover over the Pico's USB or power cycle.
- Don't upload `boot.py` over UART unless it's known-good; a broken one removes UART access.
- Only one program can hold a serial port. Disconnect MicroPico (or disable it for the
  workspace) before using mpremote.
- Reading the port with repeated open/close shows a few stale lines on open and misses the
  rest. Keep a terminal open (`mpremote repl`) for a continuous view.
- Don't use a second `UART(0, ...)` in the app; it made the interrupt unreliable.
