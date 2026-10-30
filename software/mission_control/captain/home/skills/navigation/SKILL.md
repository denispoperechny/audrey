---
name: navigation
description: How to get the boat to the mission's destination - planning short legs (normally up to 50 m) through open water, choosing azimuth and distance for start_navigation, checking progress on each wake-up, reasoning about detours, and when to stop or look around with visual_observe. Use when starting a mission, when the boat arrives at a leg's end, or when progress isn't as expected.
---

# Navigation

Getting Audrey to the mission's destination as a series of short, straight legs, checked on
every wake-up.

## What the boat does by itself

- `start_navigation(azimuth, distance)` gives the boat one leg: a point `distance` metres from
  where it is now, along `azimuth` (degrees clockwise from true north). Its autopilot steers there
  on its own between your wake-ups and stops by itself within 4 m of the point: the boat state's
  `autopilot` then reads `arrived`, and the boat holds still until it gets the next leg.
- It goes at about 0.8 m/s, so a 50 m leg takes a little over a minute. You are woken every 15-20 s
  meanwhile: judge progress over time, not wake-up by wake-up.
- Its GPS is good to a couple of metres, and it now holds a straight course well (to within a
  metre or two of the direct line on a 50 m leg). Over short distances the GPS position wanders
  by a metre or two, so don't read meaning into small changes.
- `navigate_to(lat, lon)` gives it a leg that ends at a known position instead (a detour
  waypoint, the final destination): exact, where converting it to azimuth and distance first would
  be off by however far the boat has moved since. Same limits: 5-300 m from where it is.
- `stop()` makes it drop the leg and hold still where it is.

## Calculation tools: never do the arithmetic yourself

Positions, azimuths and distances are easy to get subtly wrong by hand (the 0/360° wrap, metres
of longitude shrinking with latitude, left and right of the bow). Always use the tools:

- `course_between(from, to)`: azimuth and distance from one position to another.
- `gps_from_course(position, azimuth, distance)`: the position that far in that direction.
- `split_into_legs(from, to, max_leg_m=50)`: the straight way in legs of at most 50 m, with each
  leg's azimuth, distance and end position. It only does geometry: it knows nothing of shores or
  obstacles, so check the line is open water before following it.
- `azimuth_from_relative(heading, relative)` and `relative_from_azimuth(heading, azimuth)`: between
  directions relative to the bow (+ right, - left; as camera answers give them) and azimuths.

## Knowing where the shores are

- **If the mission gives shoreline information** (a water outline, a navigation area, no-go
  areas, or the boat state reports the nearest shore), it is your map: plan legs that stay inside
  the water with a margin, and use the camera to confirm it and to catch what the map can't show
  (boats, buoys, debris).
- **If there is no shoreline information, the camera is your only source.** Then:
  - treat water you haven't seen as unknown, not as open;
  - look (`visual_observe`) along every leg into water you haven't seen before sending it, and ask
    specifically about shores and banks on and near that line;
  - keep legs shorter (20-30 m) whenever land has been seen within about 50 m, and keep a wider
    margin from it than you would with a map (camera distances are rough);
  - keep a running picture from the lookout's notes and your own looks: where land has been seen,
    as azimuths and rough positions (`gps_from_course`). Plan around it.

## Planning legs

- **Keep legs short: normally within 50 m.** A short leg is easy to correct, and the boat never
  goes far before you look again. The tool allows 5-300 m; go beyond 50 m only across wide, clear,
  open water you have reason to trust, and never under 5 m (the boat would count as arrived at
  once).
- **Aim for the final destination one leg at a time.** Work the route out with `split_into_legs`
  (or `course_between` for a single leg), then send each leg as it comes: `navigate_to` the leg's
  end position, which stays right however the boat drifted. Near the end, `navigate_to` the
  destination itself.
- **Prefer wide, open water.** Keep well away from shores, banks, piers, moored boats and anything
  else you have seen or been told about, even if it makes the way longer. A leg should end in open
  water, not at an edge.
- **Detours are fine when the shape of the lake or obstacles call for them** - going round a point
  of land, out to the middle and back in, round a bridge pier or a buoy. When you take one, reason
  it through in a line or two: why the direct line won't do, which way round and why, and the
  intermediate point(s) the legs will aim for: work them out with `gps_from_course` (e.g. "30 m
  out from here at 200°") and go to each with `navigate_to`. Then follow the detour until the
  reason has passed, rather than drifting back to the direct line too early.
- When the direction to the destination is uncertain (the mission names a place, not a
  position), or you don't know what lies ahead, look first (`visual_observe` along the planned
  azimuth) before committing to the leg.

## On each wake-up

Read the boat state first, in this order:

1. **Can you navigate at all?** If `telemetry_age_s` is more than a few seconds, there is no
   telemetry, `position` says "no GPS fix", or `control` is remote control (RC): send no
   navigation commands. Say what you see and wait. (With RC, a person is driving.)
2. **What is the autopilot doing?**
   - `navigating`: a leg is under way. Usually do nothing. Check that `destination.distance_m` is
     going down over the last 10-20 s, and that `relative_bearing_deg` stays small (the bow
     points at the leg's end).
   - `arrived` (or no destination): the leg is done. If the final destination is reached, stop
     and report; otherwise plan and send the next leg.
3. **Is something wrong?** Act when:
   - the distance to the leg's end hasn't gone down for about 20-30 s while navigating,
     or `speed_m_s` stays near 0 (stuck, aground, against something, or the autopilot isn't
     driving): `stop()`, then look (`visual_observe`) before deciding what to do;
   - the distance grows, or the relative bearing stays large (beyond about 30°) for more than
     about 20 s: the boat is being set off course; re-plan the leg from where it is now;
   - a camera observation or a look shows an obstacle on the leg: stop or re-route (see below).
4. **Otherwise do nothing.** Most wake-ups need no action: the autopilot is already steering.
   Don't resend the same leg; a new `start_navigation` replaces the current one.

## Camera observations and looking around

For how to look well (directions, questions, reading the answers, failures), see the
`using-the-camera` skill.


- Normally your wake-ups will include notes from the camera, which watches what is ahead as the
  boat goes. Use them: anything reported close ahead or on the leg is a reason to stop or
  re-route.
- **Look for yourself with `visual_observe` whenever it would help**: before the first leg; before
  a leg towards water you haven't seen; when something was reported but isn't clear; when the boat
  isn't making progress; before a detour, to pick the side.
- What it costs: it **stops the navigation**. The boat turns to the azimuth you give, runs on it
  for at most 4 seconds while the camera takes the picture, and comes back to `original_gps`
  (pass the boat's current `position` from the state), where it holds still. It takes about 10-40
  seconds. Afterwards, always send the next leg again with `start_navigation`.
- Ask a specific question in `request_note`: "is there open water for 50 m ahead?", "how far is
  the shore and which way does it run?", "is anything floating or moored on this line?", "which
  span of the bridge is widest and clear?". The answer gives directions relative to the bow
  (-25° left, 0° ahead, +25° right) or as azimuths; turn a relative direction into an azimuth
  with `azimuth_from_relative` (the heading being the azimuth you looked along).

## Safety

- Safety comes before speed: when in doubt, `stop()` and look.
- Stop and wait (no new legs) when: the telemetry is stale or missing, there's no GPS fix, the
  boat is on remote control, something is reported close ahead, or the boat behaves unexpectedly
  (no progress, heading away from the leg) and you don't yet know why.
- Battery (`battery_v`, two cells): about 8.4 V is full, 7.8 V is about half. Below about 7.4 V
  head for the destination or the nearest safe point on short legs; below about 7.0 V stop and
  report it.
- Never plan a leg that ends on or right next to a shore, pier, bridge, or anything seen in the
  water.
- A bridge ahead: follow the `passing-under-bridges` skill (it allows one longer leg to go through).

## Saying what you do

When you act, say why in one line: "Leg 3 of 5: 40 m at 125°, keeping 30 m off the east shore",
"Detour: the direct line crosses the reed bed; going round its north side via the middle of the
lake", "Stopped: no progress for 30 s; looking ahead". When you don't act, a few words are
enough.
