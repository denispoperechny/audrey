import os
from machine import UART, Pin

# Mirror the REPL onto UART0 (GP0 = TX, GP1 = RX) in addition to USB
uart = UART(0, baudrate=115200, tx=Pin(0), rx=Pin(1))
os.dupterm(uart)
