"""The captain's own mission progress notes: what is done, what is next, what it has learned on
the way (a hazard's position, a detour chosen). They live in a file, not in the conversation, so
summarizing the conversation (agent.py: HISTORY_*) can't lose them; every wake-up message carries
them under the mission. The captain replaces them with its update_mission_progress tool; a new
run (start) clears them.

    <CAPTAIN_STATE_DIR>/progress.md      (default /tmp/audrey-captain, next to control.py's files)
"""

import os

from captain.control import _write, state_dir

MAX_CHARS = 3000
NONE_YET = "(none yet: record them with update_mission_progress as the mission goes)"


class MissionProgress:
    def __init__(self, folder=None):
        self.path = os.path.join(folder or state_dir(), "progress.md")

    def read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""

    def write(self, text):
        text = text.strip()
        if len(text) > MAX_CHARS:
            raise ValueError("progress notes are %d characters; keep them under %d" % (len(text), MAX_CHARS))
        _write(self.path, text + "\n")

    def clear(self):
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass

    def text(self):
        """For the wake-up message."""
        return self.read() or NONE_YET


def progress_tool(progress):
    from langchain_core.tools import tool

    @tool
    def update_mission_progress(notes: str) -> str:
        """Replaces your mission progress notes (they are shown to you on every wake-up, under the
        mission, and survive when older parts of the conversation are summarized). Write the whole
        picture each time, briefly: the waypoints/legs done (with the time), the current target
        and why, hazards found (with positions or azimuths) and decisions that still matter (a
        detour and its reason), anything the operator told you. Update it when something changes:
        a waypoint reached, a hazard located, a plan changed. At most 3000 characters."""
        try:
            progress.write(notes)
        except ValueError as e:
            return "not saved: %s" % e
        return "saved (%d characters)" % len(notes.strip())

    return update_mission_progress
