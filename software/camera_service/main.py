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

Stream (pictures the camera sends on its own, without a task):
    POST /stream/pictures?taken_at=<UTC time>[&camera=<name>]
                                        the body is the JPEG itself, as for a task's picture.
                                        Kept in 10-minute folders, named by when it was taken
                                        (see stream_store.py). The answer comes once the picture
                                        is checked and cached; it is stored right after, in the
                                        background (failures are counted in /health)
    GET  /stream/pictures               the latest, newest first (?after=<name> for only newer
                                        ones, ?window=<folder> for one 10-minute folder, ?limit=)
    GET  /stream/pictures/{name}        one picture, with the navigation marks; ?marks=false: as taken
    GET  /stream/latest[?since=<UTC time>][&max=<n>]
                                        the newest picture(s), only those taken after `since`;
                                        204 if there are none. max=1 (the default): the picture
                                        itself (marked unless ?marks=false), its name and time in
                                        the X-Picture-Name / X-Taken-At / X-Camera headers, and an
                                        ETag (send it back as If-None-Match: 304 if still the
                                        newest). max>1: the details of up to max, newest first, as
                                        JSON; fetch each from /stream/pictures/{name}
    The web page /stream.html shows them as they arrive. The newest 5 are also kept in memory
    (stream_store.RecentCache), so checking for new ones and fetching them rarely touch storage.

MCP (streamable HTTP, no auth), at /api/mcp:
    get_course_correction(context_and_instructions, course_target)
                                        takes the latest streamed picture, draws the course marks
                                        on it and asks the Foundry model which way to turn (see
                                        course.py): course_correction (degrees, + = right),
                                        confidence (0-1), comment, the picture and its age

Task ids are GUIDs. Where tasks and pictures are kept is set in .env (see .env.example):
  CAMERA_STORAGE_ACCOUNT_URL set   in that Azure storage account: container CAMERA_STORAGE_CONTAINER
                                   (default "camera"), under an optional CAMERA_STORAGE_PREFIX.
                                   Signed in with DefaultAzureCredential: your az login locally,
                                   a managed identity in Azure ("Storage Blob Data Contributor"
                                   on the container). Stream pictures go to container
                                   CAMERA_STREAM_CONTAINER (default "camera-stream").
  otherwise                        on the local disk: CAMERA_DB (default ./data/camera_tasks.db),
                                   CAMERA_PICTURES (default ./data/pictures) and CAMERA_STREAM_DIR
                                   (default ./data/stream)
"""

import contextlib
import os
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field

import course
import stream_store
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


class StreamPicture(BaseModel):
    name: str = Field(description="<10-minute folder>/<taken_at>_<suffix>.jpg; fetch it at /api/stream/pictures/<name>")
    taken_at: str = Field(description="when the camera took it (UTC)")
    uploaded_at: Optional[str] = Field(None, description="when it arrived here (UTC)")
    camera: Optional[str] = None
    bytes: int


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


STREAM_CACHE_SIZE = 5  # newest stream pictures kept in memory, so the live viewer rarely waits on storage


def open_stream_store():
    if os.environ.get("CAMERA_STORAGE_ACCOUNT_URL"):
        store = stream_store.BlobStreamStore(os.environ["CAMERA_STORAGE_ACCOUNT_URL"],
                                             os.environ.get("CAMERA_STREAM_CONTAINER", "camera-stream"))
    else:
        store = stream_store.LocalStreamStore(os.environ.get("CAMERA_STREAM_DIR", str(Path(__file__).resolve().parent / "data" / "stream")))
    return stream_store.RecentCache(store, STREAM_CACHE_SIZE)


store = open_store()
stream = open_stream_store()
marked_cache = OrderedDict()  # task id or stream picture name -> marked JPEG, most recently used last

mcp = MCPServer("audrey-camera", instructions="Steering help from Audrey's camera: get_course_correction "
                 "takes a fresh picture and says how many degrees to turn towards a target.")
mcp_app = mcp.streamable_http_app(
    streamable_http_path="/mcp", stateless_http=True, json_response=True,
    # The Host check is for servers on localhost; this one is public behind Azure's ingress.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))


@contextlib.asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Audrey camera service", version="0.2", lifespan=lifespan,
              description="Orders for pictures, a queue the camera picks them up from, and the pictures.",
              docs_url=API_PREFIX + "/docs", redoc_url=API_PREFIX + "/redoc", openapi_url=API_PREFIX + "/openapi.json")
api = APIRouter(prefix=API_PREFIX)


def parse_utc(text, field="taken_at"):
    """An ISO 8601 time that says it is UTC ("Z" or "+00:00"), normalised; HTTP 422 otherwise."""
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(422, "%s is not an ISO 8601 time: %r" % (field, text))
    if when.tzinfo is None or when.utcoffset() != timedelta(0):
        raise HTTPException(422, "%s must be in UTC (end it with Z or +00:00): %r" % (field, text))
    if when - datetime.now(timezone.utc) > MAX_CLOCK_AHEAD:
        raise HTTPException(422, "%s is in the future: %r (is the camera's clock set?)" % (field, text))
    return when.astimezone(timezone.utc).isoformat(timespec="milliseconds")


@api.get("/health")
def health():
    return {"status": "ok", "utc_now": now_utc(), "stream": stream.counts()}


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


async def jpeg_body(request):
    """The request body, if it is a JPEG of an acceptable size; HTTP 413 / 415 otherwise."""
    data = await request.body()
    if len(data) > MAX_PICTURE_BYTES:
        raise HTTPException(413, "picture larger than %d bytes" % MAX_PICTURE_BYTES)
    if not data.startswith(b"\xff\xd8\xff"):
        raise HTTPException(415, "the body has to be the JPEG itself (Content-Type: image/jpeg)")
    return data


def jpeg_response(key, load, marks):
    """A picture as a response, with the navigation marks drawn on unless marks is false.
    load() returns the stored JPEG or None; marked copies are cached under key."""
    if marks and key in marked_cache:
        marked_cache.move_to_end(key)
        return Response(marked_cache[key], media_type="image/jpeg")
    data = load()
    if data is None:
        return None
    if marks:
        try:
            data = marked(data)
        except OSError:  # a JPEG Pillow can't read: serve it as it came
            return Response(data, media_type="image/jpeg")
        marked_cache[key] = data
        if len(marked_cache) > MARKED_CACHE_SIZE:
            marked_cache.popitem(last=False)
    return Response(data, media_type="image/jpeg")


@api.post("/picture-tasks/{task_id}/picture", response_model=Task,
          responses={404: {"description": "no such task"}, 409: {"description": "the task isn't waiting for a picture"},
                     413: {"description": "picture too large"}, 415: {"description": "not a JPEG"}})
async def upload_picture(task_id: uuid.UUID, request: Request,
                         taken_at: str = Query(..., description="when the picture was taken, ISO 8601 UTC")):
    when = parse_utc(taken_at)
    data = await jpeg_body(request)
    task, problem = store.complete(str(task_id), when, data)
    if task is None:
        raise HTTPException(404 if problem == "not found" else 409, "task %s" % problem)
    return task


@api.get("/picture-tasks/{task_id}/picture", response_class=Response,
         responses={200: {"content": {"image/jpeg": {}}}, 404: {"description": "no such task or no picture yet"}})
def get_picture(task_id: uuid.UUID, marks: bool = Query(True, description="draw the navigation marks on it")):
    response = jpeg_response(task_id, lambda: store.picture(str(task_id)), marks)
    if response is None:
        raise HTTPException(404, "no picture for this task (yet)")
    return response


@api.post("/stream/pictures", response_model=StreamPicture, status_code=201,
          responses={413: {"description": "picture too large"}, 415: {"description": "not a JPEG"}})
async def push_stream_picture(request: Request, background: BackgroundTasks,
                              taken_at: str = Query(..., description="when the picture was taken, ISO 8601 UTC"),
                              camera: Optional[str] = Query(None, max_length=100, description="the camera's name")):
    """Answers as soon as the picture is checked and cached (the live viewer can show it from
    then on); it is written to storage after the answer, in a worker thread. A failed write is
    logged and counted in /health ("stream")."""
    when = datetime.fromisoformat(parse_utc(taken_at))
    data = await jpeg_body(request)
    item = stream.accept(when, data, camera)
    background.add_task(stream.save, item, data)  # a plain function: run in the thread pool
    return item


@api.get("/stream/pictures", response_model=list[StreamPicture])
def list_stream_pictures(after: Optional[str] = Query(None, description="only pictures newer than this one (its name)"),
                         window: Optional[str] = Query(None, description="one 10-minute folder, e.g. 20261028T1450Z"),
                         limit: int = Query(50, ge=1, le=500)):
    if after is not None and not stream_store.NAME_RE.match(after):
        raise HTTPException(422, "after has to be a stream picture's name: %r" % after)
    if window is not None and not stream_store.WINDOW_RE.match(window):
        raise HTTPException(422, "window has to be a 10-minute folder like 20261028T1450Z: %r" % window)
    return stream.list(after, limit, window)


@api.get("/stream/latest",
         responses={200: {"content": {"image/jpeg": {}, "application/json": {}},
                          "description": "max=1: the newest picture itself; max>1: the newest pictures' details"},
                    204: {"description": "no picture (newer than since, or in the last hour)"},
                    304: {"description": "max=1: the newest is still the one named in If-None-Match"}})
def latest_stream_pictures(request: Request,
                           since: Optional[str] = Query(None, description="only pictures taken after this time, ISO 8601 UTC"),
                           max: int = Query(1, ge=1, le=100, description="how many of the newest; 1 returns the picture itself"),
                           marks: bool = Query(True, description="max=1: draw the navigation marks on it")):
    after = stream_store.after_time(datetime.fromisoformat(parse_utc(since, "since"))) if since else None
    items = stream.list(after, max)
    if not items:
        return Response(status_code=204)
    if max > 1:
        return [StreamPicture(**item) for item in items]
    item = items[0]
    etag = '"%s%s"' % (item["name"], "" if marks else ";raw")
    headers = {"ETag": etag, "Cache-Control": "no-cache", "X-Picture-Name": item["name"], "X-Taken-At": item["taken_at"],
               "X-Camera": item.get("camera") or ""}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response = jpeg_response(("stream", item["name"]), lambda: stream.picture(item["name"]), marks)
    if response is None:
        return Response(status_code=204)
    response.headers.update(headers)
    return response


@api.get("/stream/pictures/{name:path}", response_class=Response,
         responses={200: {"content": {"image/jpeg": {}}}, 404: {"description": "no such picture"}})
def get_stream_picture(name: str, marks: bool = Query(True, description="draw the navigation marks on it")):
    response = jpeg_response(("stream", name), lambda: stream.picture(name), marks)
    if response is None:
        raise HTTPException(404, "no such picture")
    return response


@mcp.tool(name="get_course_correction", structured_output=True)
async def get_course_correction(context_and_instructions: str, course_target: str) -> course.CourseCorrection:
    """Takes the latest picture streamed by the boat's forward camera (red marks drawn on it at
    -25°, 0° and +25° from the bow) and asks a vision model how many degrees to turn so the boat
    heads for the course target. Takes a few seconds (the model); gives up after 30.

    context_and_instructions: the situation and how to judge it (e.g. "on a lake, keep clear of
        the shore", "steer for the middle of the gap between the two buoys")
    course_target: what to head for (e.g. "the red buoy", "the dock with the white boathouse")

    Returns course_correction in degrees (positive = turn right / starboard, negative = left /
    port), confidence from 0 to 1, a comment explaining it, and the picture it used with how old
    it was (picture_age_s) when the model looked at it.
    """
    try:
        return await course.get_course_correction(stream, context_and_instructions, course_target)
    except course.CourseError as e:
        raise ToolError(str(e))


app.include_router(api)
app.mount(API_PREFIX, mcp_app)  # /api/mcp; after the router, so the API's own paths come first
# Last, so it only gets what the API doesn't answer.
app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent / "static", html=True), name="web")
