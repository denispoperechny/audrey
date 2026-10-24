"""Picture tasks and pictures on the local disk: tasks in a SQLite file, pictures as files.
For running without Azure, and for the tests; store_blob.BlobTaskStore keeps them in an
Azure storage container instead, with the same interface.

A task moves through:   pending -> assigned -> done
  pending   created by a requestor, waiting for a camera
  assigned  handed to a camera that asked for work (see claim_next)
  done      the camera uploaded the picture (see complete); the file itself is kept on disk
"""

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

PENDING = "pending"
ASSIGNED = "assigned"
DONE = "done"
STATUSES = (PENDING, ASSIGNED, DONE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS picture_tasks (
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    params      TEXT NOT NULL,      -- JSON: the picture parameters
    requested_by TEXT,
    created_at  TEXT NOT NULL,      -- ISO 8601, UTC
    assigned_at TEXT,
    camera      TEXT,               -- which camera took it, as it named itself
    taken_at    TEXT,               -- when the camera took the picture, as it reported (UTC)
    uploaded_at TEXT,               -- when the picture arrived here (UTC)
    picture_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS picture_tasks_status ON picture_tasks (status, created_at);
"""
COLUMNS_ADDED_LATER = (("taken_at", "TEXT"), ("uploaded_at", "TEXT"), ("picture_bytes", "INTEGER"))


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class LocalTaskStore:
    def __init__(self, path, pictures_dir):
        self._pictures = pictures_dir
        os.makedirs(pictures_dir, exist_ok=True)
        self.where = "%s and %s" % (path, pictures_dir)
        # One connection shared by the app's threads; the lock keeps each operation whole.
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        have = {row["name"] for row in self._db.execute("PRAGMA table_info(picture_tasks)")}
        for name, kind in COLUMNS_ADDED_LATER:  # a database made by an earlier version
            if name not in have:
                self._db.execute("ALTER TABLE picture_tasks ADD COLUMN %s %s" % (name, kind))
        self._lock = threading.Lock()

    @staticmethod
    def _task(row):
        if row is None:
            return None
        task = dict(row)
        task["params"] = json.loads(task["params"])
        return task

    def create(self, params, requested_by=None):
        task_id = str(uuid.uuid4())  # a GUID, e.g. 3f2b8c1e-...
        with self._lock:
            self._db.execute(
                "INSERT INTO picture_tasks (id, status, params, requested_by, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, PENDING, json.dumps(params), requested_by, now_utc()))
            return self._get(task_id)

    def _get(self, task_id):
        return self._task(self._db.execute("SELECT * FROM picture_tasks WHERE id = ?", (task_id,)).fetchone())

    def get(self, task_id):
        with self._lock:
            return self._get(task_id)

    def list(self, status=None, limit=100):
        with self._lock:
            if status:
                rows = self._db.execute(
                    "SELECT * FROM picture_tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?", (status, limit))
            else:
                rows = self._db.execute("SELECT * FROM picture_tasks ORDER BY created_at DESC LIMIT ?", (limit,))
            return [self._task(r) for r in rows.fetchall()]

    def claim_next(self, camera=None):
        """The oldest pending task, marked as assigned to this camera; None if there is none.
        Two cameras asking at once never get the same task."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT id FROM picture_tasks WHERE status = ? ORDER BY created_at, rowid LIMIT 1", (PENDING,)).fetchone()
                if row is None:
                    self._db.execute("COMMIT")
                    return None
                self._db.execute("UPDATE picture_tasks SET status = ?, assigned_at = ?, camera = ? WHERE id = ?",
                                 (ASSIGNED, now_utc(), camera, row["id"]))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            return self._get(row["id"])

    def _picture_path(self, task_id):
        return os.path.join(self._pictures, "%s.jpg" % task_id)

    def complete(self, task_id, taken_at, data):
        """Stores the picture and marks the assigned task done. Returns (task, None), or
        (None, reason) if it can't: "not found", or "not assigned" (pending, or already done).
        Both happen under the lock, so two uploads for one task can't both succeed."""
        with self._lock:
            task = self._get(task_id)
            if task is None:
                return None, "not found"
            if task["status"] != ASSIGNED:
                return None, "not assigned (it is %s)" % task["status"]
            path = self._picture_path(task_id)
            with open(path + ".tmp", "wb") as f:
                f.write(data)
            os.replace(path + ".tmp", path)
            self._db.execute("UPDATE picture_tasks SET status = ?, taken_at = ?, uploaded_at = ?, picture_bytes = ? "
                             "WHERE id = ?", (DONE, taken_at, now_utc(), len(data), task_id))
            return self._get(task_id), None

    def picture(self, task_id):
        try:
            with open(self._picture_path(task_id), "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None
