---
name: passing-under-bridges
description: How to pass safely under a bridge - stopping at a safe distance, finding the widest clear span with visual_observe, moving sideways to line up with it, then crossing perpendicular to the bridge on one straight leg aimed about 30 m beyond it (no waypoints under the bridge, where GPS may be jammed), and what to do if the way isn't clear. Use when the route or the camera shows a bridge ahead.
---

# Passing under bridges

Going under bridges is allowed. Do it deliberately: find the best span, line up with it, and go
through straight and perpendicular to the bridge in one leg.

## Why it is done this way

- **GPS can be jammed or poor under a bridge.** The deck blocks the sky. A fix taken there can
  jump by many metres, or be lost. So never put a waypoint under the bridge, and don't navigate
  from a position reported under it.
- **The autopilot steers to the destination's bearing.** If the destination is far beyond the
  bridge, a GPS jump of a few metres barely changes that bearing, so the boat keeps going straight.
  With the destination right at or just past the bridge, the same jump could swing it sharply.
  Hence the destination about 30 m beyond the bridge.
- **The autopilot holds still when it has no GPS fix** (its telemetry then says "hold: no gps
  fix"). Under a bridge the boat may coast and stop until the fix comes back, then carry on by
  itself.
- **Perpendicular is the shortest way under**, with the most room either side of the span, and
  the heading doesn't change while the GPS is unreliable.

## 1. Approach and stop

- When a bridge is ahead (on the route, or seen by the camera), end the current leg in open water
  about 30-40 m before it, and stop there. Don't run up to it.
- Note the bridge's direction: which way its deck runs, as an azimuth. From the boat, use
  `visual_observe` looking at it, and ask which way the bridge runs relative to the bow. Then
  convert with `azimuth_from_relative`. The pass azimuth is the bridge's direction ± 90°: the one
  that points to the other side.

## 2. Find the span

- **Use the camera on the way in to identify the biggest span** to pass through.
- Look along the pass azimuth with `visual_observe` and ask:
  - which span is the widest and clear;
  - where its middle is relative to the bow (degrees, + right / - left);
  - roughly how far away it is;
  - whether anything is in it: boats, buoys, debris, ropes, low beams, construction.
- Look again from a different spot if the answer is unsure: the piers can hide each other at an
  angle.
- Work out the middle of the span as a position: `azimuth_from_relative` (the direction to it),
  then `gps_from_course` (from where you are, that far along that direction).

## 3. Line up

- The boat has to approach on the line through the middle of the span, perpendicular to the
  bridge. **You may need to move sideways, along the bridge, first** to get onto that line.
- Line-up point: on that line, 20-30 m before the bridge. It's the span's middle, `gps_from_course`
  back along the reverse of the pass azimuth. Go there with `navigate_to`, in a leg or two through
  open water, keeping clear of the piers.
- Once there, look along the pass azimuth once more with `visual_observe`. Check that the span is
  straight ahead (its middle within a few degrees of the bow) and still clear. If it isn't
  centred, correct the line-up point and go again. **Use the camera on the way in so the route is
  planned well**, not just once.

## 4. Go through

- Target: on the pass line, **about 30 m beyond the bridge**. Use `gps_from_course` from the
  span's middle along the pass azimuth, then `navigate_to` it. That is one straight leg from the
  line-up point, through the span, and out the other side. **This leg may be longer than 50 m**:
  that's fine here.
- **No waypoints under the bridge**, and no turns there. Don't send new commands while the boat is
  under it.
- On the wake-ups during the pass, expect the telemetry to look odd for a while: a jumping
  position, no fix, the autopilot holding. Don't react to it unless the boat stays stuck for
  30 s or more after it should be through. Wait for a good fix on the far side before planning
  the next leg.

## 5. Afterwards

- Once the autopilot reports "arrived" beyond the bridge with a good fix, check the position makes
  sense: on the far side, near the target. Then carry on with the navigation skill.

## If it isn't clear

- Something in the span, or no span looks wide enough: stay stopped at a safe distance and look
  again after a few wake-ups. Things move.
- Try the next widest span if it's clear, with the same steps.
- If the boat ends up off the line close to the bridge, or something appears in the span during
  the pass: `stop()` before the bridge if there's still room, back off to open water, and start
  again from step 2. Don't try to correct sideways right at the bridge.
- No safe span after a few tries, or the bridge can't be judged from the pictures: don't go
  through. Stop in open water and report it.
