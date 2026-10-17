"""Saved positions for the destination page, so a destination doesn't have to be typed in
every time.

They live in SAVED_POSITIONS_FILE on the computer running this app, and nowhere else: the
boat only ever gets the one it is sent. The file holds real coordinates, so it is
gitignored. Every browser session sees the same list. Its location can be changed with the
SAVED_POSITIONS_FILE environment variable (the container keeps it on a volume).

Each entry: {"id": ..., "name": "A", "lat_e7": ..., "lon_e7": ...}, coordinates as whole
numbers of degrees * 1e7, as the boat uses them. A new entry is named after the first free
letter (A, B, ... Z, AA, AB, ...) unless it is given a name.
"""

import json
import os
import string
import uuid
from itertools import count, product
from pathlib import Path

SAVED_POSITIONS_FILE = Path(os.environ.get("SAVED_POSITIONS_FILE") or Path(__file__).resolve().parent / "saved_positions.json")


def load():
    try:
        with open(SAVED_POSITIONS_FILE, encoding="utf-8") as f:
            entries = json.load(f)
    except FileNotFoundError:
        return []
    return entries if isinstance(entries, list) else []


def _save(entries):
    # Write to a temporary file and swap it in, so a crash can't leave half a list.
    SAVED_POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SAVED_POSITIONS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    os.replace(tmp, SAVED_POSITIONS_FILE)


def _letters():
    for length in count(1):
        for letters in product(string.ascii_uppercase, repeat=length):
            yield "".join(letters)


def next_name(entries):
    taken = {e["name"] for e in entries}
    return next(name for name in _letters() if name not in taken)


def add(lat_e7, lon_e7, name=""):
    """Saves a position; returns its entry. Raises ValueError for a name already in use."""
    entries = load()
    name = name.strip() or next_name(entries)
    if any(e["name"] == name for e in entries):
        raise ValueError("there is already a position named %s" % name)
    entry = {"id": uuid.uuid4().hex[:8], "name": name, "lat_e7": int(lat_e7), "lon_e7": int(lon_e7)}
    entries.append(entry)
    _save(entries)
    return entry


def rename(entry_id, name):
    """Raises ValueError for an empty name or one already in use."""
    name = name.strip()
    if not name:
        raise ValueError("the name can't be empty")
    entries = load()
    if any(e["name"] == name and e["id"] != entry_id for e in entries):
        raise ValueError("there is already a position named %s" % name)
    for e in entries:
        if e["id"] == entry_id:
            e["name"] = name
    _save(entries)


def delete(entry_id):
    _save([e for e in load() if e["id"] != entry_id])


def find(entries, lat_e7, lon_e7):
    """The saved entry at exactly this position, or None."""
    return next((e for e in entries if e["lat_e7"] == lat_e7 and e["lon_e7"] == lon_e7), None)
