---
name: using-the-camera
description: How to look around with the boat's camera (visual_observe) - what the camera can and can't see, what a look costs, choosing the direction and writing a good question, turning the answer's directions and distances into navigation, and what to do when a look fails. Use before any visual_observe call, and when a camera answer needs interpreting.
---

# Using the camera

The boat has one camera, fixed, looking straight ahead over the bow. You see through it with
`visual_observe`: it points the camera by turning the whole boat, takes a picture, and a vision
model answers your question about it.

## What the camera sees

- **About 86° across**: 43° either side of where it points. One look covers that sector; anything
  outside it isn't in the picture.
- **Red marks on every picture**: boxes labelled -25°, 0° and +25°, at those angles from the
  direction it pointed (+ right, - left). Answers use them for directions: "a buoy at about +10°".
- **Distance only by judgement.** One camera can't measure range: the model estimates it from
  sizes and perspective, so treat distances as rough (easily 30-50 % off). Keep generous margins.
- **Limits**: a dark, misty or rainy picture, glare from a low sun or the water, and spray or drops
  on the lens all make it see less. Small, low or dark things (logs, ropes, swimmers) are easy to
  miss at a distance. If an answer says it can't make something out, believe it.

## The lookout's notes (every wake-up)

Another AI, the lookout, looks at the camera's latest picture every 10 seconds while the boat
goes, and each wake-up brings you its notes since the previous one, each with:

- its age and the boat's heading when the picture was taken;
- a short summary (it keeps track of its own earlier notes, so it says what is new, has moved or
  has gone);
- hazards, each with its direction from the bow (+ right, - left), its azimuth (already worked out
  from that heading), and a rough distance;
- how far ahead looks clear, and its confidence (0-1).

How to use them:

- They only see **what was ahead of the bow** at that moment (86° across). Nothing about the
  sides or behind: look yourself (`visual_observe`) when you need those.
- A hazard's **azimuth** stays right after the boat turns; its "from the bow" doesn't. Use the
  azimuth, with `relative_from_azimuth` against the current heading to see where it is now.
- **Distances are rough**, and objects far off or low in the water are easy to miss: keep margins.
- **Low confidence**, "no observation" (no fresh picture, a model error), or notes that contradict
  each other: don't trust the way ahead blindly. Look yourself, or slow down to shorter legs.
- Several notes in a row reporting the same hazard getting closer on the leg: act (stop or
  re-route) before it is close.
- The notes are seconds old; with the boat moving at about 0.8 m/s, that is a few metres.

## What a look costs

`visual_observe(original_gps, azimuth, request_note)`:

1. The boat turns to `azimuth` (up to 10 s).
2. It runs on that course for at most 4 s while the camera takes the picture.
3. It goes back to `original_gps` and holds still there.
4. The tool returns the answer once the boat is back.

- **It stops the navigation**: afterwards the boat holds still until you send the next leg again
  (`navigate_to` or `start_navigation`).
- **It takes about 10-40 s** (up to about 75 s); that wake-up lasts until it returns, and the next one
  comes a couple of seconds after.
- **The boat moves**: the turn plus up to 4 s on the azimuth, a few metres. Only look where there
  is room for that: not right next to a shore, a pier or a moored boat, and never under a bridge.
- Pass the boat's current `position` from the state as `original_gps`.

So look when it changes a decision, not on every wake-up.

## When to look

- **With no shoreline information, much more often**: the camera is then the only way to know
  where the land is (see the navigation skill). Look along every leg into water you haven't seen.

- Before the first leg, along the planned route.
- Before a leg into water you haven't seen, or after a detour, along the new direction.
- When the lookout's notes or an earlier answer reported something unclear on or near the route.
- When the boat isn't making progress or behaves oddly: look along the leg, at what's ahead.
- Before passing under a bridge (see `passing-under-bridges`), and when choosing a side for a
  detour: look both ways.
- Usually not: on a leg already checked, through open water, with nothing new reported.

## Choosing where to look

- **Along the leg** (its azimuth): is the way clear?
- **At something**: the azimuth to it, e.g. from `course_between` to a known position, or
  `azimuth_from_relative` on an earlier answer's relative direction.
- **Around**: one look covers 86°, so four looks about 90° apart (ahead, right, behind, left) cover
  everything. That's four turns: only do it when you really need the whole picture (lost, stuck,
  or something reported but not located).

## Writing the question (`request_note`)

Ask for what the decision needs, specifically, and in the terms you can use:

- "Is the water ahead clear for at least 50 m? If not, what is in the way, at what angle from the
  bow and roughly how far?"
- "Where is the nearest shore: its direction (degrees from the bow) and rough distance, and which
  way does it run?"
- "Is anything floating, moored or moving within 30 m of the line straight ahead?"
- "Which span of the bridge is widest and clear, at what angle from the bow, and roughly how far?"
- "I'm planning to go this way for about 50 m. Are there any serious obstructions? Based on what
  you see, which course would you recommend, in degrees from the bow?"
- "Where do the shore and the open water lie in this direction: which angles from the bow are
  water, and which are land or reeds, and how far is the nearest land?"

Good habits:

- Ask for **directions as degrees from the bow** (the red marks) and **distances in metres**.
- Ask it to **say what it can't make out**.
- Keep it to one or two questions. A long list gets vague answers.
- Mention what you're looking for ("a red buoy", "the dock with a white boathouse") if you know it.

## Using the answer

- **A recommended course is advice, not an order.** The model sees one picture; you also have the
  mission, the route, the lookout's notes and any shoreline information. Convert the course to an
  azimuth, check it against those, and decide yourself.
- The answer's directions are relative to the azimuth you looked along. Turn them into azimuths
  with `azimuth_from_relative(heading = the azimuth you looked at, relative = the answer's degrees)`.
- To turn "something at +15°, about 40 m" into a position: `gps_from_course(original_gps, that
  azimuth, 40)`. Keep it as a rough position with a wide margin, not a point to aim at.
- Clear water "for 50 m ahead" means the leg along that azimuth is fine for about that far: keep
  legs within what was seen.
- Unsure or contradictory answers: look again, from a different spot or closer (with room to
  spare), before betting the route on them.
- The picture was taken just before the answer (seconds old); things that move (boats, birds,
  swimmers) may have moved since.

## When a look fails

The tool returns an error, and the boat is back at `original_gps` and holding still:

- **"didn't turn to ... within 10 s"**: wind or current may be holding the boat, or the turn was
  large. Try once more. If it fails again, look in a nearer direction or carry on without it,
  carefully.
- **"no camera picture taken during the 4 s"**: the camera may be off or its pictures delayed.
  Try once more. If it keeps failing, treat the boat as blind: shorter legs only through water
  already seen, or stop and report.
- **"the vision model didn't answer"**: try once more. If it keeps failing, stop and report.
- **"didn't get back to original_gps"**: check the boat state before doing anything else; the
  boat may be stuck or drifting.

After any failure, remember the navigation is stopped: send the next leg yourself when you're
ready to go on.
