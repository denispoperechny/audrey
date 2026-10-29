"""Pictures the camera pushes on its own (no task), kept in 10-minute folders.

    <window>/<taken_at>_<suffix>.jpg      e.g. 20261028T1450Z/20261028T145312.345Z_ab12cd34.jpg

<window> is the start of the 10-minute interval (UTC) the picture was taken in, and the file
name starts with the time it was taken, so names sort in time order. The suffix keeps two
pictures taken in the same millisecond apart.

LocalStreamStore keeps them in a folder on disk, BlobStreamStore in an Azure storage container
(with the camera's name as blob metadata). Both have the same interface: put, list, picture.
"""

import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone

log = logging.getLogger("camera_service.stream")

WINDOW_MINUTES = 10
LIST_WINDOWS = 6  # how far back list() looks without `after` or `window`: an hour
NAME_RE = re.compile(r"^(\d{8}T\d{2}\d0Z)/(\d{8}T\d{6}\.\d{3}Z)_([0-9a-f]{8})\.jpg$")
WINDOW_RE = re.compile(r"^\d{8}T\d{2}\d0Z$")


def window_of(when):
    """The 10-minute folder a UTC datetime falls in, e.g. 20261028T1450Z."""
    start = when.replace(minute=when.minute - when.minute % WINDOW_MINUTES, second=0, microsecond=0)
    return start.strftime("%Y%m%dT%H%MZ")


def name_for(when):
    stamp = when.strftime("%Y%m%dT%H%M%S.") + "%03dZ" % (when.microsecond // 1000)
    return "%s/%s_%s.jpg" % (window_of(when), stamp, uuid.uuid4().hex[:8])


def after_time(when):
    """A name that sorts after every picture taken at or before `when` and before every one
    taken later: list(after=after_time(t)) gives the pictures taken after t."""
    stamp = when.strftime("%Y%m%dT%H%M%S.") + "%03dZ" % (when.microsecond // 1000)
    return "%s/%s_ffffffff.jpg" % (window_of(when), stamp)


def taken_at_of(name):
    """The ISO 8601 time a picture's name says it was taken, or None if it isn't a picture name."""
    m = NAME_RE.match(name)
    if not m:
        return None
    when = datetime.strptime(m.group(2), "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
    return when.isoformat(timespec="milliseconds")


def windows_to_scan(after=None, window=None, now=None):
    """The folders list() looks in, newest first."""
    if window:
        return [window]
    now = now or datetime.now(timezone.utc)
    # A little ahead too: the camera's clock may run up to a few minutes fast.
    newest = now + timedelta(minutes=WINDOW_MINUTES)
    if after:
        oldest = datetime.strptime(NAME_RE.match(after).group(1), "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)
    else:
        oldest = now - timedelta(minutes=WINDOW_MINUTES * (LIST_WINDOWS - 1))
    names, at = [], newest
    while at >= oldest - timedelta(minutes=WINDOW_MINUTES) and len(names) < 200:
        names.append(window_of(at))
        at -= timedelta(minutes=WINDOW_MINUTES)
    return sorted(set(names), reverse=True)


def new_item(when, data, camera=None):
    """A picture's details, named by when it was taken, received now."""
    name = name_for(when)
    return {"name": name, "taken_at": taken_at_of(name), "camera": camera, "bytes": len(data),
            "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}


def _select(items, after, limit):
    items = [i for i in items if after is None or i["name"] > after]
    items.sort(key=lambda i: i["name"], reverse=True)
    return items[:limit]


class LocalStreamStore:
    def __init__(self, folder):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)

    def put(self, when, data, camera=None):
        return self.save(new_item(when, data, camera), data)

    def save(self, item, data):
        path = os.path.join(self.folder, item["name"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
        with open(path + ".json", "w") as f:
            json.dump({"camera": item["camera"], "uploaded_at": item["uploaded_at"]}, f)
        return item

    def list(self, after=None, limit=50, window=None):
        items = []
        for w in windows_to_scan(after, window):
            d = os.path.join(self.folder, w)
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                name = w + "/" + f
                if not NAME_RE.match(name):
                    continue
                try:
                    with open(os.path.join(d, f + ".json")) as m:
                        meta = json.load(m)
                except (OSError, ValueError):
                    meta = {}
                items.append({"name": name, "taken_at": taken_at_of(name), "uploaded_at": meta.get("uploaded_at"),
                              "camera": meta.get("camera"), "bytes": os.path.getsize(os.path.join(d, f))})
        return _select(items, after, limit)

    def picture(self, name):
        if not NAME_RE.match(name):
            return None
        try:
            with open(os.path.join(self.folder, name), "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None


class BlobStreamStore:
    def __init__(self, account_url, container, credential=None):
        from azure.storage.blob import BlobServiceClient

        if credential is None:
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        self._container = BlobServiceClient(account_url, credential=credential).get_container_client(container)

    def put(self, when, data, camera=None):
        return self.save(new_item(when, data, camera), data)

    def save(self, item, data):
        from azure.storage.blob import ContentSettings

        self._container.get_blob_client(item["name"]).upload_blob(
            data, overwrite=False, content_settings=ContentSettings(content_type="image/jpeg"),
            metadata={"taken_at": item["taken_at"], "uploaded_at": item["uploaded_at"], "camera": item["camera"] or ""})
        return item

    def list(self, after=None, limit=50, window=None):
        items = []
        for w in windows_to_scan(after, window):
            for blob in self._container.list_blobs(name_starts_with=w + "/", include=["metadata"]):
                if not NAME_RE.match(blob.name):
                    continue  # (the folder itself, on a hierarchical-namespace account)
                meta = blob.metadata or {}
                items.append({"name": blob.name, "taken_at": taken_at_of(blob.name), "uploaded_at": meta.get("uploaded_at"),
                              "camera": meta.get("camera") or None, "bytes": blob.size})
            if not after and not window and len(items) >= limit:
                break  # newest folders first: enough already
        return _select(items, after, limit)

    def picture(self, name):
        from azure.core.exceptions import ResourceNotFoundError

        if not NAME_RE.match(name):
            return None
        try:
            return self._container.get_blob_client(name).download_blob().readall()
        except ResourceNotFoundError:
            return None


class RecentCache:
    """Keeps the newest few pictures in memory in front of another stream store, so the viewer's
    "anything newer than X?" and fetching a just-arrived picture don't go to storage.

    Two ways in: put() stores a picture and then caches it; accept() caches it at once and
    save() stores it later (the service does that in the background, so the camera gets its
    answer without waiting for storage). With accept(), a picture can be seen before it is
    stored, and one whose save fails stays only in memory: failures are logged and counted.
    list(after=X) is answered from memory when X is at least as new as the oldest cached picture:
    the cache then holds every picture newer than X (it keeps the newest by name, so anything it
    let go of is older). Otherwise, and for list() without `after` or with `window`, the store is
    asked. Only right with one service replica: another replica wouldn't see this one's uploads.
    """

    def __init__(self, store, size=5):
        self.store = store
        self.size = size
        self._items = {}  # name -> (item, jpeg)
        self._lock = threading.Lock()  # save() runs in worker threads
        self.pending = self.saved = self.failed = 0

    def _remember(self, item, data):
        with self._lock:
            self._items[item["name"]] = (item, data)
            for name in sorted(self._items)[:-self.size]:  # keep the newest by name
                del self._items[name]

    def put(self, when, data, camera=None):
        item = self.store.put(when, data, camera)
        self._remember(item, data)
        return item

    def accept(self, when, data, camera=None):
        """Names and caches a picture now; save(item, data) stores it."""
        item = new_item(when, data, camera)
        self._remember(item, data)
        with self._lock:
            self.pending += 1
        return item

    def save(self, item, data):
        try:
            self.store.save(item, data)
            outcome = "saved"
        except Exception:
            log.exception("stream picture %s not stored", item["name"])
            outcome = "failed"
        with self._lock:
            self.pending -= 1
            setattr(self, outcome, getattr(self, outcome) + 1)

    def counts(self):
        with self._lock:
            return {"pending": self.pending, "saved": self.saved, "failed": self.failed}

    def list(self, after=None, limit=50, window=None):
        with self._lock:
            cached = [item for item, _ in self._items.values()]
        if window is None and cached:
            if after is not None and after >= min(i["name"] for i in cached):
                return _select(cached, after, limit)
            if after is None and len(cached) >= limit:  # the newest few: all in memory
                return _select(cached, None, limit)
        return self.store.list(after, limit, window)

    def picture(self, name):
        with self._lock:
            cached = self._items.get(name)
        return cached[1] if cached else self.store.picture(name)
