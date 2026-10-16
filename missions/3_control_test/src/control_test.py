"""Runs the control test: timed destination / stop commands to the boat over MQTT, RC
takeovers at set moments, and a timed log of everything that happened.

    pip install -r requirements.txt
    python control_test.py           # Ctrl+C aborts (and sends the boat a stop)
    python control_test.py --dry-run --cycle-s 30 --run-s 12 --rc-test-s 5    # rehearsal: nothing is sent

The test, with the defaults (about an hour):

  autonomous section 1    Each section is 2 cycles of 5 min: a destination is sent, the stop
  RC test 1               follows 1 min later, then nothing until the cycle ends.
  autonomous section 2    In every second cycle the console asks for a TAKEOVER during the
  RC test 2               run, while the boat is driving: switch to RC, Enter, do what you
  autonomous section 3    want, switch back, Enter. The moment within the run varies.
  RC test 3               Between sections it asks for an RC TEST with the boat idle: Enter
  autonomous section 4    when it starts, Enter when it's over (~6 min each makes the hour).

  4 sections x 2 cycles x 5 min = 40 min autonomous, 8 destinations, 4 takeovers, 3 RC tests.

When your part comes is printed before the start, again with clock times once it starts,
and after each RC test (they take as long as you take, which moves everything after them).

Commands (see the boat's controller.py): {"cmd": "destination", "lat_e7", "lon_e7"} and,
as the stop, {"cmd": "clear_destination"}, after which the boat holds neutral. After each
one the boat's telemetry is watched for up to CONFIRM_S, to log whether it took it.

Destinations are points OFFSET_M from where the boat is when the test starts (its GPS fix
then, or --lat / --lon), going round north, east, south, west. The boat only drives its
motor towards them while the RC switch is on autonomous; on dry land that spins the prop.

The RC switch is checked against what the Pico reports: a section doesn't start until it
reports autonomous (or you type "go"), and after "started" of a takeover or an RC test the
log says whether it reported RC. Every change of mode it reports is logged by itself, too.

Two files in ../data, with the same name stamp and the same "t" (local time) and "unix"
fields, one JSON object per line:

  test_<date>_<time>.jsonl   the timed log of the test:
      {"t": "...", "unix": ..., "event": "destination_sent", "section": 1, ..., "boat": {...}}
    Events: test_started, section_started / section_finished, destination_sent,
    destination_confirmed / destination_not_confirmed, stop_sent, stop_confirmed /
    stop_not_confirmed, takeover_requested / takeover_started / takeover_finished,
    rc_test_requested / rc_test_started / rc_test_finished, mode_confirmed /
    mode_not_confirmed / mode_check_overridden, pico_mode (the Pico reported a change),
    telemetry_lost / telemetry_back, mqtt_connected / mqtt_disconnected, test_finished /
    test_aborted. "boat" is what its latest telemetry said at that moment.
  mqtt_<date>_<time>.jsonl   every message on the boat's topics (all its telemetry, and the
    commands), in mqtt_logger.py's format. mqtt_logger.py isn't needed alongside this.
"""

import argparse
import json
import math
import signal
import threading
import time
import uuid
from datetime import datetime

import paho.mqtt.client as mqtt

from mqtt_logger import DATA_DIR, TOPICS, Logger, load_mqtt_config

TELEMETRY_TOPIC = "audrey/boat1/telemetry"  # see the boat's mqtt.py
COMMAND_TOPIC = "audrey/boat1/cmd"

SECTIONS = 4  # autonomous sections, with an RC test between each two
CYCLES_PER_SECTION = 2
CYCLE_S = 300  # a destination is sent this often
RUN_S = 60  # the stop follows the destination after this long
OFFSET_M = 50  # how far from the boat the destinations are
DIRECTIONS = (("north", 1, 0), ("east", 0, 1), ("south", -1, 0), ("west", 0, -1))  # name, north, east

TAKEOVER_EVERY = 2  # every this many destinations, an RC takeover during the run (0 = never)
TAKEOVER_AT = (0.25, 0.5, 0.75, 0.35)  # when in the run it's asked for (fraction of RUN_S), in turn
RC_TEST_S = 360  # what an RC test between sections is assumed to take, for the printed times

CONFIRM_S = 8  # how long to wait for the boat's telemetry to show a command took effect
MODE_WAIT_S = 10  # ...and to show the RC switch where it should be
TELEMETRY_LOST_S = 5  # no telemetry for this long is logged as lost
FIX_WAIT_S = 60  # how long to wait at the start for the boat's position
COUNTDOWN_S = 30  # console countdown period while waiting
HEADS_UP_S = 10  # ...and one more line this long before each step

EARTH_RADIUS_M = 6371000
M_PER_E7 = EARTH_RADIUS_M * math.pi / 180 / 1e7  # meters per 1e-7 degree of latitude


def clock(at=None):
    return datetime.fromtimestamp(at if at is not None else time.time()).strftime("%H:%M:%S")


def minutes(seconds):
    seconds = round(seconds)
    return "%d:%02d" % (seconds // 60, seconds % 60)


def offset_position(lat_e7, lon_e7, north_m, east_m):
    """The position north_m / east_m from (lat_e7, lon_e7), in degrees * 1e7 (equirectangular,
    the same approximation the boat's gnc.py uses the other way round)."""
    return (lat_e7 + round(north_m / M_PER_E7),
            lon_e7 + round(east_m / (M_PER_E7 * math.cos(math.radians(lat_e7 / 1e7)))))


def pico_mode(telemetry):
    """"autonomous", "rc", or None when this telemetry message has no Pico status."""
    bcu = (telemetry or {}).get("bcu")
    if not bcu:
        return None
    return "autonomous" if bcu.get("esp32_mode") else "rc"


class Boat:
    """MQTT link: records everything on the boat's topics, keeps its latest telemetry,
    sends it commands."""

    def __init__(self, config, log, recorder, dry_run):
        self.log = log
        self.recorder = recorder  # mqtt_logger.Logger: every message goes to its file
        self.dry_run = dry_run
        self.lock = threading.Lock()
        self.latest = None
        self.received_at = None
        self.mode = None  # last mode the Pico reported
        self.lost_since = None  # set while telemetry is missing

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="audrey-test-" + uuid.uuid4().hex[:8])
        client.username_pw_set(config["user"], config["password"])
        client.tls_set(ca_certs=config["ca_file"])
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=1, max_delay=10)
        client.connect_async(config["host"], config["port"], keepalive=30)
        client.loop_start()
        self.client = client
        threading.Thread(target=self._watch_telemetry, daemon=True).start()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.recorder.write({"event": "connect_refused", "reason": str(reason_code)})
            self.log.event("mqtt_connect_refused", reason=str(reason_code))
            return
        client.subscribe(TOPICS, qos=1)
        self.recorder.write({"event": "connected"})
        self.log.event("mqtt_connected")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.recorder.write({"event": "disconnected", "reason": str(reason_code)})
        self.log.event("mqtt_disconnected", reason=str(reason_code))

    def _on_message(self, client, userdata, message):
        self.recorder.on_message(client, userdata, message)
        if message.topic != TELEMETRY_TOPIC:
            return
        try:
            data = json.loads(message.payload)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        now = time.time()
        mode = pico_mode(data)
        with self.lock:
            self.latest = data
            self.received_at = now
            lost_since, self.lost_since = self.lost_since, None
            changed = mode is not None and mode != self.mode
            if changed:
                self.mode = mode
        if lost_since is not None:
            self.log.event("telemetry_back", gap_s=round(now - lost_since, 1))
        if changed:
            self.log.event("pico_mode", mode=mode)

    def _watch_telemetry(self):
        while True:
            time.sleep(1)
            with self.lock:
                received_at = self.received_at
                lost = (received_at is not None and self.lost_since is None
                        and time.time() - received_at > TELEMETRY_LOST_S)
                if lost:
                    self.lost_since = received_at
            if lost:
                self.log.event("telemetry_lost", last_s_ago=round(time.time() - received_at, 1))

    def telemetry(self):
        """(latest telemetry dict or None, its age in seconds or None)."""
        with self.lock:
            if self.latest is None:
                return None, None
            return self.latest, time.time() - self.received_at

    def summary(self):
        """The few telemetry fields worth having next to every log entry."""
        latest, age = self.telemetry()
        if latest is None:
            return None
        output = latest.get("output") or {}
        bcu = latest.get("bcu") or {}
        gps = latest.get("gps") or {}
        return {
            "telemetry_age_s": round(age, 1),
            "seq": latest.get("seq"),
            "reason": output.get("reason"),
            "throttle": output.get("throttle"),
            "rudder": output.get("rudder"),
            "mode": pico_mode(latest),
            "failsafe": bcu.get("failsafe"),
            "fix_valid": gps.get("fix_valid"),
        }

    def send(self, command):
        """Publishes a command; returns an error text, or None if it went out."""
        if self.dry_run:
            return None
        if not self.client.is_connected():
            return "not connected to the MQTT broker"
        info = self.client.publish(COMMAND_TOPIC, json.dumps(command), qos=1)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return mqtt.error_string(info.rc)
        try:
            info.wait_for_publish(timeout=5)
        except (RuntimeError, ValueError) as e:
            return str(e)
        return None if info.is_published() else "the broker didn't acknowledge it in 5 s"

    def wait_for(self, predicate, timeout_s):
        """Seconds until the boat's telemetry satisfies predicate(latest), or None on timeout.
        Only telemetry received after the call counts."""
        start = time.time()
        while time.time() - start < timeout_s:
            with self.lock:
                fresh = self.received_at is not None and self.received_at >= start
                latest = self.latest
            if fresh and predicate(latest):
                return time.time() - start
            time.sleep(0.1)
        return None

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


class TestLog:
    def __init__(self, path):
        self.path = path
        self.file = open(path, "a", encoding="utf-8")
        self.lock = threading.Lock()  # paho's thread logs events too
        self.boat = None

    def event(self, name, **details):
        now = time.time()
        line = {"t": datetime.fromtimestamp(now).astimezone().isoformat(timespec="milliseconds"),
                "unix": round(now, 3), "event": name}
        line.update(details)
        if self.boat is not None:
            line["boat"] = self.boat.summary()
        with self.lock:
            self.file.write(json.dumps(line, separators=(",", ":")) + "\n")
            self.file.flush()
        shown = ", ".join("%s=%s" % item for item in details.items())
        print("%s  %s%s" % (clock(now), name, "  (" + shown + ")" if shown else ""))

    def close(self):
        with self.lock:
            self.file.close()


def wait_until(deadline, what):
    """Sleeps until time.time() reaches deadline, with a countdown on the console and a
    last line HEADS_UP_S before."""
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        print("%s    %s in %s" % (clock(), what, minutes(remaining)))
        step = COUNTDOWN_S
        if remaining > HEADS_UP_S:
            step = min(step, remaining - HEADS_UP_S)
        time.sleep(min(remaining, step))


def build_plan(args):
    """The steps of the test, in order: runs (a destination and its stop) and RC tests."""
    plan = []
    number = 0
    takeovers = 0
    for section in range(1, args.sections + 1):
        for cycle in range(1, args.cycles + 1):
            name, north, east = DIRECTIONS[number % len(DIRECTIONS)]
            number += 1
            takeover_at = None
            if args.takeover_every and number % args.takeover_every == 0:
                takeover_at = TAKEOVER_AT[takeovers % len(TAKEOVER_AT)] * args.run_s
                takeovers += 1
            plan.append({"kind": "run", "section": section, "cycle": cycle, "cycles": args.cycles, "number": number,
                         "direction": name, "north": north, "east": east,
                         "takeover": takeovers if takeover_at is not None else None, "takeover_at": takeover_at})
        if section < args.sections:
            plan.append({"kind": "rc_test", "number": section})
    return plan


def print_schedule(plan, index, start, args, absolute):
    """When the operator is needed, from plan[index] on, if that step starts at `start`
    (a time.time() value, or 0 with absolute=False for times from the start of the test)."""
    def stamp(at):
        return clock(at) if absolute else "+" + minutes(at)

    at = start
    lines = []
    for step in plan[index:]:
        if step["kind"] == "run":
            if step["takeover_at"] is not None:
                lines.append("  %s  TAKEOVER %d: switch to RC during run %d, %s after its destination" % (
                    stamp(at + step["takeover_at"]), step["takeover"], step["number"], minutes(step["takeover_at"])))
            at += args.cycle_s
        else:
            lines.append("  %s  RC TEST %d, boat idle (assumed to take %s)" % (stamp(at), step["number"], minutes(args.rc_test_s)))
            at += args.rc_test_s
    lines.append("  %s  end of the test" % stamp(at))
    print("Your part%s:" % ("" if absolute else " (time from the start)"))
    print("\n".join(lines))


def start_position(boat, args):
    """(lat_e7, lon_e7, source) the destinations are measured from."""
    if args.lat is not None and args.lon is not None:
        return round(args.lat * 1e7), round(args.lon * 1e7), "command line"
    print("%s  waiting for the boat's position (up to %d s)..." % (clock(), FIX_WAIT_S))
    waited = boat.wait_for(lambda t: bool((t.get("gps") or {}).get("fix_valid")), FIX_WAIT_S)
    if waited is None:
        latest, _ = boat.telemetry()
        raise SystemExit("No GPS fix from the boat in %d s (%s). Wait for a fix, or pass --lat and --lon." % (
            FIX_WAIT_S, "no telemetry at all" if latest is None else "telemetry is arriving"))
    gps = boat.telemetry()[0]["gps"]
    return gps["lat_e7"], gps["lon_e7"], "boat's GPS fix"


def check_mode(boat, log, wanted, context):
    """Logs whether the Pico reports the RC switch in the `wanted` position within MODE_WAIT_S."""
    if boat.dry_run:
        return True
    took = boat.wait_for(lambda t: pico_mode(t) == wanted, MODE_WAIT_S)
    if took is not None:
        log.event("mode_confirmed", mode=wanted, context=context, after_s=round(took, 1))
        return True
    log.event("mode_not_confirmed", wanted=wanted, context=context, reported=boat.mode, waited_s=MODE_WAIT_S)
    print("\a  *** The Pico reports %s, not %s. ***" % (boat.mode or "nothing", wanted))
    return False


def require_autonomous(boat, log, context):
    """Doesn't return until the Pico reports autonomous mode, or the operator says to go on:
    a section run with the switch on RC would test nothing."""
    while not check_mode(boat, log, "autonomous", context):
        answer = input("  Put the switch on autonomous and press Enter to check again (or type go to continue anyway): ")
        if answer.strip().lower() == "go":
            log.event("mode_check_overridden", wanted="autonomous", context=context)
            return


def takeover(boat, log, step):
    number = step["takeover"]
    log.event("takeover_requested", takeover=number, destination=step["number"])
    print("\a")
    print("=" * 76)
    print("TAKEOVER %d. The boat is driving to destination %d." % (number, step["number"]))
    input("  Switch to RC NOW, then press Enter the moment you have control: ")
    started = time.time()
    log.event("takeover_started", takeover=number)
    check_mode(boat, log, "rc", "takeover %d" % number)
    input("  Press Enter the moment you are done and the switch is back on autonomous: ")
    log.event("takeover_finished", takeover=number, duration_s=round(time.time() - started, 1))
    check_mode(boat, log, "autonomous", "after takeover %d" % number)
    print("=" * 76)


def run(boat, log, args, step, origin, after):
    """One cycle: destination, (takeover,) stop, then nothing until the cycle is over."""
    cycle_start = time.time()
    number = step["number"]
    lat_e7, lon_e7 = offset_position(origin[0], origin[1], step["north"] * args.offset_m, step["east"] * args.offset_m)

    error = boat.send({"cmd": "destination", "lat_e7": lat_e7, "lon_e7": lon_e7})
    log.event("destination_sent", section=step["section"], cycle=step["cycle"], number=number,
              direction=step["direction"], offset_m=args.offset_m, lat_e7=lat_e7, lon_e7=lon_e7, error=error)
    if not boat.dry_run:
        took = boat.wait_for(lambda t: t.get("destination") == {"lat_e7": lat_e7, "lon_e7": lon_e7}, CONFIRM_S)
        if took is None:
            log.event("destination_not_confirmed", number=number, waited_s=CONFIRM_S)
        else:
            log.event("destination_confirmed", number=number, after_s=round(took, 1))

    if step["takeover_at"] is not None:
        wait_until(cycle_start + step["takeover_at"], "TAKEOVER %d (switch to RC)" % step["takeover"])
        takeover(boat, log, step)

    wait_until(cycle_start + args.run_s, "stop")  # at once, if the takeover outlasted the run
    error = boat.send({"cmd": "clear_destination"})
    log.event("stop_sent", section=step["section"], cycle=step["cycle"], number=number, error=error)
    if not boat.dry_run:
        took = boat.wait_for(lambda t: "destination" in t and t["destination"] is None, CONFIRM_S)
        if took is None:
            log.event("stop_not_confirmed", number=number, waited_s=CONFIRM_S)
        else:
            log.event("stop_confirmed", number=number, after_s=round(took, 1))

    wait_until(cycle_start + args.cycle_s, after)


def rc_test(boat, log, number):
    log.event("rc_test_requested", rc_test=number)
    print("\a")
    print("=" * 76)
    print("RC TEST %d. The boat has no destination and is holding neutral." % number)
    input("  Switch to RC, then press Enter the moment the RC test STARTS: ")
    started = time.time()
    log.event("rc_test_started", rc_test=number)
    check_mode(boat, log, "rc", "rc test %d" % number)
    input("  Press Enter the moment the RC test is FINISHED and the switch is back on autonomous: ")
    log.event("rc_test_finished", rc_test=number, duration_s=round(time.time() - started, 1))
    print("=" * 76)


def what_follows(plan, index):
    """What the countdown at the end of plan[index] (a run) is counting down to."""
    if index + 1 == len(plan):
        return "end of the test"
    following = plan[index + 1]
    return "RC test %d" % following["number"] if following["kind"] == "rc_test" else "next destination"


def stop(signum, frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description="Run the control test (see the top of this file).")
    parser.add_argument("--sections", type=int, default=SECTIONS, help="autonomous sections (default %(default)s)")
    parser.add_argument("--cycles", type=int, default=CYCLES_PER_SECTION, help="destinations per section (default %(default)s)")
    parser.add_argument("--cycle-s", type=float, default=CYCLE_S, help="seconds between destinations (default %(default)s)")
    parser.add_argument("--run-s", type=float, default=RUN_S, help="seconds from a destination to its stop (default %(default)s)")
    parser.add_argument("--offset-m", type=float, default=OFFSET_M, help="distance of the destinations (default %(default)s)")
    parser.add_argument("--takeover-every", type=int, default=TAKEOVER_EVERY,
                        help="an RC takeover during every Nth run, 0 for none (default %(default)s)")
    parser.add_argument("--rc-test-s", type=float, default=RC_TEST_S,
                        help="what an RC test is assumed to take, for the printed times (default %(default)s)")
    parser.add_argument("--lat", type=float, help="start position, degrees (default: the boat's GPS fix)")
    parser.add_argument("--lon", type=float)
    parser.add_argument("--dry-run", action="store_true", help="send nothing to the boat and skip the RC switch checks")
    parser.add_argument("--yes", action="store_true", help="don't ask before starting")
    args = parser.parse_args()
    if args.run_s >= args.cycle_s:
        parser.error("--run-s has to be shorter than --cycle-s")
    if (args.lat is None) != (args.lon is None):
        parser.error("--lat and --lon go together")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log = TestLog(DATA_DIR / ("test_%s.jsonl" % stamp))
    recorder = Logger(DATA_DIR / ("mqtt_%s.jsonl" % stamp))
    recorder.write({"event": "started", "topics": TOPICS, "by": "control_test.py"})
    boat = Boat(load_mqtt_config(), log, recorder, args.dry_run)
    log.boat = boat
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)  # also when started in the background, where it's ignored

    plan = build_plan(args)
    runs = [step for step in plan if step["kind"] == "run"]
    finished = False
    started = False
    try:
        if boat.wait_for(lambda t: True, 15) is None and not args.dry_run:
            print("%s  no telemetry from the boat yet" % clock())
        origin = start_position(boat, args)

        print()
        print("Control test%s: %d autonomous sections of %d x %s, %d destinations, %d takeovers, %d RC tests." % (
            " (DRY RUN: nothing is sent)" if args.dry_run else "", args.sections, args.cycles, minutes(args.cycle_s),
            len(runs), sum(1 for step in runs if step["takeover_at"] is not None), len(plan) - len(runs)))
        print("  Destination every %s, stop %s after it, %g m from the start." % (
            minutes(args.cycle_s), minutes(args.run_s), args.offset_m))
        print("  Start position (%s): %.7f, %.7f" % (origin[2], origin[0] / 1e7, origin[1] / 1e7))
        print("  Boat now: %s" % (boat.summary(),))
        print("  Logs: %s\n        %s" % (log.path, recorder.path))
        print_schedule(plan, 0, 0, args, absolute=False)
        if not args.yes:
            input("RC switch on autonomous? Press Enter to start (Ctrl+C to cancel): ")

        started = True
        log.event("test_started", dry_run=args.dry_run, sections=args.sections, cycles=args.cycles,
                  cycle_s=args.cycle_s, run_s=args.run_s, offset_m=args.offset_m, takeover_every=args.takeover_every,
                  start_lat_e7=origin[0], start_lon_e7=origin[1], start_from=origin[2])
        for index, step in enumerate(plan):
            if step["kind"] == "rc_test":
                rc_test(boat, log, step["number"])
                continue
            if step["cycle"] == 1:
                require_autonomous(boat, log, "before section %d" % step["section"])
                log.event("section_started", section=step["section"])
                print_schedule(plan, index, time.time(), args, absolute=True)
            run(boat, log, args, step, origin, what_follows(plan, index))
            if step["cycle"] == step["cycles"]:
                log.event("section_finished", section=step["section"])
        finished = True
        log.event("test_finished", destinations=len(runs))
    except KeyboardInterrupt:
        print()
    finally:
        if started and not finished:
            # Whatever happened, don't leave the boat with a destination.
            error = boat.send({"cmd": "clear_destination"})
            log.event("test_aborted", stop_sent=error is None, error=error)
        boat.close()
        recorder.write({"event": "stopped", "messages": recorder.messages})
        recorder.close()
        log.close()
        print("logs: %s\n      %s" % (log.path, recorder.path))


if __name__ == "__main__":
    main()
