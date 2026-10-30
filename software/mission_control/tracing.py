"""Traces: what mission control's processes did and why, for reviewing a run afterwards (next to
the MQTT logs and the camera's pictures, by time).

    import tracing
    tracing.event("wake-up", n=3, message=..., tokens={...})

Each event is one JSON line:

    {"unix": 1793..., "t": "2026-10-30T15:42:07.123+00:00", "proc": "captain", "run_id": "99e2b8c3",
     "kind": "wake-up", ...the fields given...}

`run_id` is the mission run (captain/control.py) at that moment, or null between runs. Lines go to
one file per process per 10-minute window (UTC, aligned to the clock), laid out as the MQTT logs:

    <TRACE_DIR>/YYYY/MM/DD/<proc>_YYYYMMDDTHHMMZ.jsonl

and the trace_uploader process (python -m tracing) copies them to the storage account under
mission-control/ with the same names, every TRACE_UPLOAD_S.

Cheap for the caller: event() only puts the dict on a queue; a background thread turns it into
JSON and appends it to the file, about once a second. Strings longer than MAX_TEXT are cut. If
the queue is full (the disk stuck), events are dropped and counted, never waited for. Nothing
here raises into the caller.

Settings: TRACE_DIR (default /tmp/audrey-traces; "off" turns tracing off), TRACE_UPLOAD_S (30),
TRACE_KEEP_HOURS (24: local files older than that are deleted once uploaded), and the storage
account: TRACE_STORAGE_ACCOUNT_URL (none: the files stay local), TRACE_STORAGE_CONTAINER (logs).
"""

import atexit
import json
import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import settings  # noqa: F401  (reads .env)

DEFAULT_DIR = "/tmp/audrey-traces"
WINDOW_S = 600
MAX_QUEUE = 10000
MAX_TEXT = 20000  # characters per string field
WRITE_EVERY_S = 1.0
RUN_ID_CACHE_S = 1.0
BLOB_PREFIX = "mission-control/"

log = logging.getLogger("tracing")


def trace_dir():
    return os.environ.get("TRACE_DIR") or DEFAULT_DIR


def process_name():
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    return (spec.name.split(".")[0] if spec and spec.name else Path(sys.argv[0]).stem) or "process"


def file_name(proc, unix, window_s=WINDOW_S):
    start = datetime.fromtimestamp(unix - unix % window_s, timezone.utc)
    return "%s/%s_%s.jsonl" % (start.strftime("%Y/%m/%d"), proc, start.strftime("%Y%m%dT%H%MZ"))


def _cut(value):
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "...[%d more]" % (len(value) - MAX_TEXT)
    if isinstance(value, dict):
        return {k: _cut(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_cut(v) for v in value]
    return value


class Tracer:
    def __init__(self, folder, proc=None, window_s=WINDOW_S, run_id=None, max_queue=MAX_QUEUE):
        self.folder, self.proc, self.window_s = Path(folder), proc or process_name(), window_s
        self.run_id = run_id or _control_run_id  # () -> the current run id or None
        self.queue = queue.Queue(maxsize=max_queue)
        self.dropped = 0
        self._thread = None
        self._lock = threading.Lock()

    def event(self, kind, **fields):
        try:
            now = time.time()
            self.queue.put_nowait({"unix": round(now, 3), "proc": self.proc, "run_id": self.run_id(), "kind": kind, **fields})
        except queue.Full:
            self.dropped += 1
            return
        except Exception:  # never into the caller
            return
        if self._thread is None:
            self._start()

    def _start(self):
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="tracing", daemon=True)
                self._thread.start()
                atexit.register(self.flush)

    def _run(self):
        while True:
            time.sleep(WRITE_EVERY_S)
            self.flush()

    def flush(self):
        """Writes what is queued (the writer thread does it every second; also at exit)."""
        events = []
        while True:
            try:
                events.append(self.queue.get_nowait())
            except queue.Empty:
                break
        if self.dropped:
            events.append({"unix": round(time.time(), 3), "proc": self.proc, "run_id": None, "kind": "trace-dropped",
                           "count": self.dropped})
            self.dropped = 0
        by_file = {}
        for e in events:
            unix = e.pop("unix")
            stamp = datetime.fromtimestamp(unix, timezone.utc).isoformat(timespec="milliseconds")
            try:
                line = json.dumps(_cut({"unix": unix, "t": stamp, **e}), ensure_ascii=False, default=str)
            except Exception as error:
                line = json.dumps({"unix": unix, "t": stamp, "proc": self.proc, "kind": "trace-error", "error": str(error)})
            by_file.setdefault(file_name(self.proc, unix, self.window_s), []).append(line)
        for name, lines in by_file.items():
            try:
                path = self.folder / name
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
            except Exception:
                log.exception("couldn't write %d trace events to %s", len(lines), name)


class _Off:
    def event(self, kind, **fields):
        pass

    def flush(self):
        pass


_run_id_cache = (0.0, None)


def _control_run_id():
    """The current run's id from the captain's control file (read at most once a second), or None
    if no mission is running or paused."""
    global _run_id_cache
    now = time.monotonic()
    if now - _run_id_cache[0] < RUN_ID_CACHE_S:
        return _run_id_cache[1]
    from captain.control import IDLE, Control

    control = Control().read()
    run_id = control.get("run_id") if control.get("state") != IDLE else None
    _run_id_cache = (now, run_id)
    return run_id


_tracer = None


def tracer():
    global _tracer
    if _tracer is None:
        folder = trace_dir()
        _tracer = _Off() if folder == "off" else Tracer(folder)
    return _tracer


def event(kind, **fields):
    """Records an event (see the top of this file). Takes microseconds; never raises."""
    tracer().event(kind, **fields)


def usage_of(response):
    """Token counts from an OpenAI Responses API response: {"in", "cached", "out"} or None."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    details = getattr(usage, "input_tokens_details", None)
    return {"in": usage.input_tokens, "cached": getattr(details, "cached_tokens", 0) or 0, "out": usage.output_tokens}


# ---- trace_uploader: python -m tracing --------------------------------------------------------

class Uploader:
    """Copies changed trace files to the storage account and deletes old local ones."""

    def __init__(self, folder, store, keep_s):
        self.folder, self.store, self.keep_s = Path(folder), store, keep_s
        self.uploaded = {}  # relative name -> (size, mtime) last uploaded

    def pass_once(self, now=None):
        now = time.time() if now is None else now
        uploaded = 0
        for path in sorted(self.folder.rglob("*.jsonl")):
            name = path.relative_to(self.folder).as_posix()
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            seen = (stat.st_size, stat.st_mtime)
            if self.store is not None and self.uploaded.get(name) != seen:
                try:
                    self.store.put(BLOB_PREFIX + name, path.read_bytes())
                    self.uploaded[name] = seen
                    uploaded += 1
                except Exception as e:
                    log.warning("couldn't upload %s: %s", name, e)
                    continue
            if now - stat.st_mtime > self.keep_s and (self.store is None or self.uploaded.get(name) == seen):
                path.unlink(missing_ok=True)
                self.uploaded.pop(name, None)
        return uploaded


class BlobStore:
    def __init__(self, account_url, container):
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient, ContentSettings

        self._content = ContentSettings(content_type="application/x-ndjson")
        service = BlobServiceClient(account_url, credential=DefaultAzureCredential(exclude_interactive_browser_credential=True))
        self._container = service.get_container_client(container)
        self.where = "%s/%s" % (account_url.rstrip("/"), container)

    def put(self, name, data):
        self._container.upload_blob(name, data, overwrite=True, content_settings=self._content)


def main():
    import signal

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stopping.set())
    signal.signal(signal.SIGINT, lambda *a: stopping.set())
    folder = trace_dir()
    if folder == "off":
        log.info("tracing is off (TRACE_DIR=off): nothing to upload")
        stopping.wait()
        return
    account = os.environ.get("TRACE_STORAGE_ACCOUNT_URL")
    store = BlobStore(account, os.environ.get("TRACE_STORAGE_CONTAINER") or "logs") if account else None
    every_s = float(os.environ.get("TRACE_UPLOAD_S") or 30)
    uploader = Uploader(folder, store, float(os.environ.get("TRACE_KEEP_HOURS") or 24) * 3600)
    log.info("traces in %s -> %s every %g s", folder, store.where + "/" + BLOB_PREFIX if store else "(local only: "
             "no TRACE_STORAGE_ACCOUNT_URL)", every_s)
    while True:
        try:
            uploader.pass_once()
        except Exception:
            log.exception("upload pass failed")
        if stopping.wait(every_s):
            time.sleep(2)  # let the other processes write their last events
            uploader.pass_once()
            return


if __name__ == "__main__":
    main()
