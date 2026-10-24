"""Camera service: takes orders for pictures, hands them to the camera when it asks, and
receives the pictures.

    python -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/uvicorn main:app --reload          # http://localhost:8000/api/docs

Every path is under /api (API_PREFIX), the docs too: /api/docs, /api/redoc, /api/openapi.json.
Everything else is the web page in static/ (order pictures, see the requests and pictures).

Requestor:
    POST /picture-tasks                 order a picture; the parameters are optional (see
                                        PictureParams: anything left out takes its default)
    GET  /picture-tasks                 list tasks, newest first (?status=pending|assigned|done)
    GET  /picture-tasks/{id}            one task
    GET  /picture-tasks/{id}/picture    the picture, once it has been uploaded, with the navigation
                                        marks drawn on (see marks.py); ?marks=false: as taken
    GET  /picture-defaults              the parameters used when none are given
    GET  /health                        "ok" if the service is up (for probes); touches no storage

Camera:
    POST /camera/next-task              "anything for me?": always answers with the current UTC
                                        time (so the camera can set its clock) and either the
                                        oldest pending task, now assigned to this camera, or
                                        task: null. ?camera=<name> records which camera took it.
    POST /picture-tasks/{id}/picture?taken_at=<UTC time>
                                        upload the picture for a task it was given: the request
                                        body is the JPEG itself (Content-Type: image/jpeg),
                                        taken_at is when the picture was taken, ISO 8601 in UTC
                                        (e.g. 2026-10-23T18:05:12.345Z)

Task ids are GUIDs. Where tasks and pictures are kept is set in .env (see .env.example):
  CAMERA_STORAGE_ACCOUNT_URL set   in that Azure storage account: container CAMERA_STORAGE_CONTAINER
                                   (default "camera"), under an optional CAMERA_STORAGE_PREFIX.
                                   Signed in with DefaultAzureCredential: your az login locally,
                                   a managed identity in Azure ("Storage Blob Data Contributor"
                                   on the container).
  otherwise                        on the local disk: CAMERA_DB (default ./data/camera_tasks.db)
                                   and CAMERA_PICTURES (default ./data/pictures)
"""

import os
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from marks import marked
from store import LocalTaskStore, now_utc

API_PREFIX = "/api"
MAX_PICTURE_BYTES = 5 * 1024 * 1024  # a full-size JPEG from the XIAO is 0.1-0.5 MB
MAX_CLOCK_AHEAD = timedelta(minutes=5)  # a taken_at further in the future than this is refused
MARKED_CACHE_SIZE = 50  # marked pictures kept in memory (a picture never changes once uploaded)


class FrameSize(str, Enum):
    """The camera's picture sizes (esp32-camera names), up to the OV3660's full 3 megapixels."""
    QVGA = "QVGA"  # 320 x 240
    VGA = "VGA"  # 640 x 480
    SVGA = "SVGA"  # 800 x 600
    XGA = "XGA"  # 1024 x 768
    HD = "HD"  # 1280 x 720
    SXGA = "SXGA"  # 1280 x 1024
    UXGA = "UXGA"  # 1600 x 1200
    FHD = "FHD"  # 1920 x 1080
    QXGA = "QXGA"  # 2048 x 1536


class GainCeiling(str, Enum):
    """How far the camera may turn up the gain in the dark (more = brighter but grainier)."""
    X2 = "2X"
    X4 = "4X"
    X8 = "8X"
    X16 = "16X"
    X32 = "32X"
    X64 = "64X"
    X128 = "128X"


class PictureParams(BaseModel):
    """How to take the picture. The defaults come from the XIAO ESP32-S3 Sense tests (OV3660
    camera): high JPEG quality, the left-right flip corrected and the low-light settings, with
    night mode off. Full HD rather than the full 3 megapixels and a 1 s settle, as uploads run
    at about 30 KB/s and a longer settle made little difference to the exposure."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    frame_size: FrameSize = Field(FrameSize.FHD, description="picture size; FHD = 1920 x 1080, QXGA = 2048 x 1536 (the most)")
    jpeg_quality: int = Field(8, ge=4, le=63, description="JPEG quality: lower = better and bigger")
    hmirror: bool = Field(True, description="flip left-right (this camera needs it on to look right)")
    vflip: bool = Field(False, description="flip upside down")
    settle_ms: int = Field(1000, ge=0, le=30000,
                           description="how long auto exposure and white balance run before the shot")
    gain_ceiling: GainCeiling = Field(GainCeiling.X32, description="the most gain auto exposure may use")
    night_mode: bool = Field(False, description="let the exposure run longer than a frame (the sensor's aec2)")
    ae_level: int = Field(2, ge=-2, le=2, description="auto exposure bias: -2 darker .. +2 brighter")


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    params: Optional[PictureParams] = Field(None, description="leave out for all the defaults")
    requested_by: Optional[str] = Field(None, max_length=100, description="who ordered it, free text")


class Task(BaseModel):
    id: uuid.UUID
    status: Literal["pending", "assigned", "done"]
    params: PictureParams
    requested_by: Optional[str]
    created_at: str
    assigned_at: Optional[str]
    camera: Optional[str]
    taken_at: Optional[str] = Field(None, description="when the camera took the picture (UTC)")
    uploaded_at: Optional[str] = Field(None, description="when the picture arrived here (UTC)")
    picture_bytes: Optional[int] = None


class NextTask(BaseModel):
    utc_now: str = Field(description="the service's current time, UTC, ISO 8601")
    task: Optional[Task] = Field(description="the task to do, or null if there is none")


def open_store():
    load_dotenv(Path(__file__).resolve().parent / ".env")  # variables already set win
    if os.environ.get("CAMERA_STORAGE_ACCOUNT_URL"):
        from store_blob import BlobTaskStore

        return BlobTaskStore(os.environ["CAMERA_STORAGE_ACCOUNT_URL"], os.environ.get("CAMERA_STORAGE_CONTAINER", "camera"),
                             os.environ.get("CAMERA_STORAGE_PREFIX", ""))
    base = Path(__file__).resolve().parent / "data"
    db = Path(os.environ.get("CAMERA_DB", base / "camera_tasks.db"))
    db.parent.mkdir(parents=True, exist_ok=True)
    return LocalTaskStore(str(db), os.environ.get("CAMERA_PICTURES", str(db.parent / "pictures")))


store = open_store()
marked_cache = OrderedDict()  # task id -> marked JPEG, most recently used last

app = FastAPI(title="Audrey camera service", version="0.2",
              description="Orders for pictures, a queue the camera picks them up from, and the pictures.",
              docs_url=API_PREFIX + "/docs", redoc_url=API_PREFIX + "/redoc", openapi_url=API_PREFIX + "/openapi.json")
api = APIRouter(prefix=API_PREFIX)


def parse_utc(text):
    """An ISO 8601 time that says it is UTC ("Z" or "+00:00"), normalised; HTTP 422 otherwise."""
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(422, "taken_at is not an ISO 8601 time: %r" % text)
    if when.tzinfo is None or when.utcoffset() != timedelta(0):
        raise HTTPException(422, "taken_at must be in UTC (end it with Z or +00:00): %r" % text)
    if when - datetime.now(timezone.utc) > MAX_CLOCK_AHEAD:
        raise HTTPException(422, "taken_at is in the future: %r (is the camera's clock set?)" % text)
    return when.astimezone(timezone.utc).isoformat(timespec="milliseconds")


@api.get("/health")
def health():
    return {"status": "ok", "utc_now": now_utc()}


@api.get("/picture-defaults", response_model=PictureParams)
def picture_defaults():
    return PictureParams()


@api.post("/picture-tasks", response_model=Task, status_code=201)
def create_task(request: Optional[TaskRequest] = None):
    request = request or TaskRequest()
    params = request.params or PictureParams()
    return store.create(params.model_dump(mode="json"), request.requested_by)


@api.get("/picture-tasks", response_model=list[Task])
def list_tasks(status: Optional[Literal["pending", "assigned", "done"]] = None, limit: int = Query(100, ge=1, le=1000)):
    return store.list(status, limit)


@api.get("/picture-tasks/{task_id}", response_model=Task)
def get_task(task_id: uuid.UUID):
    task = store.get(str(task_id))
    if task is None:
        raise HTTPException(404, "no such task")
    return task


@api.post("/camera/next-task", response_model=NextTask)
def next_task(camera: Optional[str] = Query(None, max_length=100, description="the camera's name, recorded on the task")):
    return {"utc_now": now_utc(), "task": store.claim_next(camera)}


@api.post("/picture-tasks/{task_id}/picture", response_model=Task,
          responses={404: {"description": "no such task"}, 409: {"description": "the task isn't waiting for a picture"},
                     413: {"description": "picture too large"}, 415: {"description": "not a JPEG"}})
async def upload_picture(task_id: uuid.UUID, request: Request,
                         taken_at: str = Query(..., description="when the picture was taken, ISO 8601 UTC")):
    when = parse_utc(taken_at)
    data = await request.body()
    if len(data) > MAX_PICTURE_BYTES:
        raise HTTPException(413, "picture larger than %d bytes" % MAX_PICTURE_BYTES)
    if not data.startswith(b"\xff\xd8\xff"):
        raise HTTPException(415, "the body has to be the JPEG itself (Content-Type: image/jpeg)")
    task, problem = store.complete(str(task_id), when, data)
    if task is None:
        raise HTTPException(404 if problem == "not found" else 409, "task %s" % problem)
    return task


@api.get("/picture-tasks/{task_id}/picture", response_class=Response,
         responses={200: {"content": {"image/jpeg": {}}}, 404: {"description": "no such task or no picture yet"}})
def get_picture(task_id: uuid.UUID, marks: bool = Query(True, description="draw the navigation marks on it")):
    if marks and task_id in marked_cache:
        marked_cache.move_to_end(task_id)
        return Response(marked_cache[task_id], media_type="image/jpeg")
    data = store.picture(str(task_id))
    if data is None:
        raise HTTPException(404, "no picture for this task (yet)")
    if marks:
        try:
            data = marked(data)
        except OSError:  # a JPEG Pillow can't read: serve it as it came
            return Response(data, media_type="image/jpeg")
        marked_cache[task_id] = data
        if len(marked_cache) > MARKED_CACHE_SIZE:
            marked_cache.popitem(last=False)
    return Response(data, media_type="image/jpeg")


app.include_router(api)
# Last, so it only gets what the API doesn't answer.
app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent / "static", html=True), name="web")
