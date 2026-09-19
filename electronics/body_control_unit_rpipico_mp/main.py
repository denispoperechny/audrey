import select
import sys
import time

# Ctrl+C sent over the dupterm UART can't interrupt a running script by itself, but
# reading it from stdin raises KeyboardInterrupt. So the loop reads all pending stdin
# (USB + UART) every pass; a Ctrl+C then ends the script and drops to the REPL, which
# lets mpremote upload over UART. Everything else that was read is returned as text.
poll = select.poll()
poll.register(sys.stdin, select.POLLIN)


def read_input():
    data = ""
    while poll.poll(0):
        data += sys.stdin.read(1)
    return data


n = 0
last = time.ticks_ms()
while True:
    data = read_input()
    if data:
        print("rx:", repr(data))
    if time.ticks_diff(time.ticks_ms(), last) >= 1000:
        last = time.ticks_add(last, 1000)
        print("hi 10 there", n)
        n += 1
    time.sleep_ms(50)
