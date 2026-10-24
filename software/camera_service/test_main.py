"""python -m pytest -q                         local storage, a throwaway database and folder
   CAMERA_TEST_AZURE=1 python -m pytest -q   the same tests against the Azure container from
                                             .env, in a throwaway folder that is deleted after"""

import io
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image
from dotenv import dotenv_values

AZURE = bool(os.environ.get("CAMERA_TEST_AZURE"))
TMP = tempfile.mkdtemp()
if AZURE:
    settings = dotenv_values(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    os.environ["CAMERA_STORAGE_ACCOUNT_URL"] = settings["CAMERA_STORAGE_ACCOUNT_URL"]
    os.environ["CAMERA_STORAGE_CONTAINER"] = settings.get("CAMERA_STORAGE_CONTAINER") or "camera"
    os.environ["CAMERA_STORAGE_PREFIX"] = "tests/run-" + uuid.uuid4().hex[:8]
else:
    os.environ["CAMERA_STORAGE_ACCOUNT_URL"] = ""  # set, so .env can't switch the tests to Azure
    os.environ["CAMERA_DB"] = os.path.join(TMP, "test.db")
    os.environ["CAMERA_PICTURES"] = os.path.join(TMP, "pictures")

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import course  # noqa: E402
import marks  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def clean_up_azure():
    yield
    if AZURE:
        main.store.delete_everything()

client = TestClient(main.app, base_url="http://testserver" + main.API_PREFIX)
DEFAULTS = {"frame_size": "FHD", "jpeg_quality": 8, "hmirror": True, "vflip": False, "settle_ms": 1000,
            "gain_ceiling": "32X", "night_mode": False, "ae_level": 2}
def make_jpeg(width=640, height=480):
    out = io.BytesIO()
    Image.new("RGB", (width, height), (40, 90, 160)).save(out, "JPEG")
    return out.getvalue()


JPEG = make_jpeg()


def drain():
    while client.post("/camera/next-task").json()["task"] is not None:
        pass


def claimed_task(**params):
    drain()
    task = client.post("/picture-tasks", json={"params": params} if params else None).json()
    assert client.post("/camera/next-task", params={"camera": "xiao-1"}).json()["task"]["id"] == task["id"]
    return task


def utc_iso(delta=timedelta(0)):
    return (datetime.now(timezone.utc) + delta).isoformat().replace("+00:00", "Z")


def upload(task_id, data=JPEG, taken_at=None):
    return client.post("/picture-tasks/%s/picture" % task_id, params={"taken_at": taken_at or utc_iso()},
                       content=data, headers={"Content-Type": "image/jpeg"})


def test_defaults_are_the_last_xiao_settings_with_night_mode_off():
    assert client.get("/picture-defaults").json() == DEFAULTS


def test_task_ids_are_guids():
    task = client.post("/picture-tasks").json()
    assert str(uuid.UUID(task["id"])) == task["id"] and task["id"].count("-") == 4


def test_order_without_parameters_gets_the_defaults():
    r = client.post("/picture-tasks")
    assert r.status_code == 201
    task = r.json()
    assert task["status"] == "pending" and task["params"] == DEFAULTS and task["assigned_at"] is None


def test_partial_parameters_fill_in_the_rest():
    r = client.post("/picture-tasks", json={"params": {"frame_size": "VGA", "night_mode": True}, "requested_by": "denys"})
    assert r.status_code == 201
    assert r.json()["params"] == dict(DEFAULTS, frame_size="VGA", night_mode=True)
    assert r.json()["requested_by"] == "denys"


def test_bad_parameters_are_refused():
    for bad in ({"jpeg_quality": 0}, {"frame_size": "8K"}, {"ae_level": 3}, {"gain_ceiling": "5X"},
                {"settle_ms": -1}, {"unknown_setting": 1}):
        assert client.post("/picture-tasks", json={"params": bad}).status_code == 422, bad


def test_next_task_always_tells_the_time():
    drain()
    before = datetime.now(timezone.utc)
    r = client.post("/camera/next-task")
    assert r.status_code == 200 and r.json()["task"] is None
    told = datetime.fromisoformat(r.json()["utc_now"])
    assert told.utcoffset() == timedelta(0) and abs((told - before).total_seconds()) < 5


def test_camera_gets_tasks_oldest_first_then_nothing():
    drain()
    first = client.post("/picture-tasks").json()
    second = client.post("/picture-tasks", json={"params": {"frame_size": "XGA"}}).json()
    got = client.post("/camera/next-task", params={"camera": "xiao-1"}).json()
    assert got["task"]["id"] == first["id"] and got["task"]["status"] == "assigned" and got["task"]["camera"] == "xiao-1"
    assert got["task"]["assigned_at"] is not None and got["utc_now"]
    assert client.post("/camera/next-task").json()["task"]["id"] == second["id"]
    assert client.post("/camera/next-task").json()["task"] is None


def test_task_lookup_and_listing():
    drain()
    task = client.post("/picture-tasks").json()
    assert client.get("/picture-tasks/" + task["id"]).json() == task
    assert client.get("/picture-tasks/" + str(uuid.uuid4())).status_code == 404
    assert client.get("/picture-tasks/not-a-guid").status_code == 422
    assert [t["id"] for t in client.get("/picture-tasks", params={"status": "pending"}).json()] == [task["id"]]
    client.post("/camera/next-task")
    assert client.get("/picture-tasks", params={"status": "pending"}).json() == []
    assert client.get("/picture-tasks/" + task["id"]).json()["status"] == "assigned"
    assert client.get("/picture-tasks", params={"status": "finished"}).status_code == 422


def test_upload_completes_the_task_and_the_picture_can_be_fetched():
    task = claimed_task()
    taken = utc_iso(timedelta(seconds=-3))
    r = upload(task["id"], taken_at=taken)
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "done" and done["picture_bytes"] == len(JPEG)
    stored = datetime.fromisoformat(done["taken_at"])  # kept to the millisecond
    assert abs(stored - datetime.fromisoformat(taken.replace("Z", "+00:00"))) < timedelta(milliseconds=1)
    assert done["uploaded_at"] >= done["taken_at"]
    got = client.get("/picture-tasks/%s/picture" % task["id"], params={"marks": "false"})
    assert got.status_code == 200 and got.content == JPEG and got.headers["content-type"] == "image/jpeg"
    assert [t["id"] for t in client.get("/picture-tasks", params={"status": "done"}).json()][0] == task["id"]


def test_upload_is_refused_when_it_should_be():
    pending = client.post("/picture-tasks").json()  # not handed to a camera yet
    assert upload(pending["id"]).status_code == 409
    assert upload(str(uuid.uuid4())).status_code == 404
    task = claimed_task()
    assert upload(task["id"], data=b"not a jpeg").status_code == 415
    assert upload(task["id"], data=b"\xff\xd8\xff" + b"0" * main.MAX_PICTURE_BYTES).status_code == 413
    for bad_time in ("yesterday", "2026-10-23T18:00:00", "2026-10-23T18:00:00+02:00", utc_iso(timedelta(hours=1))):
        assert upload(task["id"], taken_at=bad_time).status_code == 422, bad_time
    assert client.get("/picture-tasks/%s/picture" % task["id"]).status_code == 404  # nothing stored by the refusals
    assert upload(task["id"]).status_code == 200
    assert upload(task["id"]).status_code == 409  # a second upload for the same task


def test_two_cameras_never_get_the_same_task():
    drain()
    ids = {client.post("/picture-tasks").json()["id"] for _ in range(50 if not AZURE else 12)}
    got, lock = [], threading.Lock()

    def camera(name):
        while True:
            task = client.post("/camera/next-task", params={"camera": name}).json()["task"]
            if task is None:
                return
            with lock:
                got.append(task["id"])

    threads = [threading.Thread(target=camera, args=("cam%d" % i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == sorted(ids)  # every task handed out exactly once


def test_tasks_survive_a_restart():
    task = client.post("/picture-tasks").json()
    reopened = main.open_store()
    assert reopened.get(task["id"])["status"] == "pending"


@pytest.mark.skipif(AZURE, reason="local database only")
def test_a_database_from_the_previous_version_is_upgraded():
    import sqlite3
    path = os.path.join(TMP, "old.db")
    old = sqlite3.connect(path)
    old.executescript("CREATE TABLE picture_tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, params TEXT NOT NULL, "
                      "requested_by TEXT, created_at TEXT NOT NULL, assigned_at TEXT, camera TEXT);"
                      "INSERT INTO picture_tasks VALUES ('a', 'pending', '{}', NULL, '2026-01-01', NULL, NULL);")
    old.commit()
    old.close()
    upgraded = main.LocalTaskStore(path, os.path.join(TMP, "old_pictures"))
    assert upgraded.get("a")["taken_at"] is None


def test_web_page():
    root = TestClient(main.app)  # the page is outside /api
    page = root.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert root.get("/api/openapi.json").json()["components"]["schemas"]["PictureParams"]


def test_picture_is_served_with_the_marks_unless_asked_not_to():
    task = claimed_task()
    assert upload(task["id"]).status_code == 200
    got = client.get("/picture-tasks/%s/picture" % task["id"])
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"
    image = Image.open(io.BytesIO(got.content)).convert("RGB")
    assert image.size == (640, 480)
    # The rectangle's left edge, half way down it: red where the original is blue.
    r, g, b = image.getpixel((round(320 - 640 * marks.BOX_W / 2), round(480 * marks.CENTER_Y)))
    assert r > 150 and g < 80 and b < 80, (r, g, b)
    assert client.get("/picture-tasks/%s/picture" % task["id"]).content == got.content  # the cached copy


def test_a_picture_that_cannot_be_decoded_is_served_as_it_came():
    task = claimed_task()
    broken = b"\xff\xd8\xff\xe0" + b"not really a jpeg" * 50
    assert upload(task["id"], data=broken).status_code == 200
    assert client.get("/picture-tasks/%s/picture" % task["id"]).content == broken


def test_angle_marks_land_where_the_clock_calibration_put_them():
    # QXGA: +-24 deg (4 minute ticks of the clock) measured at 571 and 1572, about 500 px either
    # side of the 6; the model puts +-25 deg a quarter of the width either side of the centre.
    assert abs(marks.x_of(0, 2048) - 1024) < 1
    assert abs(marks.x_of(-25, 2048) - 512) < 5 and abs(marks.x_of(25, 2048) - 1536) < 5


# ---- MCP: get_course_correction (a fake camera and a fake model) ----------------------------

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def mcp_client():
    with TestClient(main.app) as c:  # runs the lifespan, which the MCP session manager needs
        yield c


def mcp_call(c, method, params, request_id=1):
    r = c.post("/api/mcp", headers=MCP_HEADERS, json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def fake_camera(stop):
    """Takes whatever task is pending and uploads a picture for it, like the XIAO does."""
    while not stop.is_set():
        task = main.store.claim_next("fake-camera")
        if task:
            main.store.complete(task["id"], utc_iso(), JPEG)
        stop.wait(0.05)


def test_mcp_lists_the_tool(mcp_client):
    mcp_call(mcp_client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    tools = {t["name"]: t for t in mcp_call(mcp_client, "tools/list", {})["tools"]}
    tool = tools["get_course_correction"]
    assert set(tool["inputSchema"]["required"]) == {"context_and_instructions", "course_target"}
    assert {"course_correction", "confidence", "comment"} <= set(tool["outputSchema"]["properties"])


def test_get_course_correction_takes_a_picture_and_asks_the_model(mcp_client, monkeypatch):
    seen = {}

    def model(jpeg, context, target, timeout_s=None):
        seen.update(jpeg=jpeg, context=context, target=target)
        return {"course_correction": -12.5, "confidence": 1.4, "comment": "the buoy is left of 0°"}

    monkeypatch.setattr(course, "ask_model", model)
    drain()
    stop = threading.Event()
    camera = threading.Thread(target=fake_camera, args=(stop,))
    camera.start()
    try:
        result = mcp_call(mcp_client, "tools/call", {"name": "get_course_correction", "arguments": {
            "context_and_instructions": "on a pond", "course_target": "the red buoy"}})
    finally:
        stop.set()
        camera.join()
    assert not result.get("isError"), result
    out = result["structuredContent"]
    assert out["course_correction"] == -12.5 and out["confidence"] == 1.0  # clamped to 0..1
    assert out["comment"] == "the buoy is left of 0°" and out["picture_taken_at"]
    task = client.get("/picture-tasks/" + out["task_id"]).json()
    assert task["status"] == "done" and task["requested_by"] == course.REQUESTED_BY and task["camera"] == "fake-camera"
    assert seen["context"] == "on a pond" and seen["target"] == "the red buoy"
    image = Image.open(io.BytesIO(seen["jpeg"])).convert("RGB")  # the model got the marked picture
    r, g, b = image.getpixel((round(320 - 640 * marks.BOX_W / 2), round(480 * marks.CENTER_Y)))
    assert r > 150 and g < 80 and b < 80, (r, g, b)


def test_get_course_correction_reports_a_camera_that_does_not_answer(mcp_client, monkeypatch):
    monkeypatch.setattr(course, "TOOL_TIMEOUT_S", 0.5)
    monkeypatch.setattr(course, "ask_model", lambda *a: pytest.fail("the model shouldn't be asked"))
    drain()
    result = mcp_call(mcp_client, "tools/call", {"name": "get_course_correction", "arguments": {
        "context_and_instructions": "", "course_target": "anything"}})
    assert result["isError"] and "the camera didn't deliver" in result["content"][0]["text"]
    drain()  # the order it left behind


def test_get_course_correction_gives_up_on_a_slow_model(mcp_client, monkeypatch):
    monkeypatch.setattr(course, "TOOL_TIMEOUT_S", 1.0)
    monkeypatch.setattr(course, "ask_model", lambda *a: time.sleep(5) or {})  # far past the limit
    drain()
    stop = threading.Event()
    camera = threading.Thread(target=fake_camera, args=(stop,))
    camera.start()
    try:
        started = time.monotonic()
        result = mcp_call(mcp_client, "tools/call", {"name": "get_course_correction", "arguments": {
            "context_and_instructions": "", "course_target": "anything"}})
        took = time.monotonic() - started
    finally:
        stop.set()
        camera.join()
    assert result["isError"] and "the model didn't answer" in result["content"][0]["text"]
    assert took < 2.5, took  # gave up at the limit, didn't wait for the model


def test_ask_model_sends_the_picture_and_reads_the_answer(monkeypatch):
    sent = {}

    class FakeResponses:
        def create(self, **request):
            sent.update(request)
            return type("Response", (), {"output_text": '{"course_correction": 5, "confidence": 0.5, "comment": "ok"}'})()

    class FakeClient:
        responses = FakeResponses()

        def with_options(self, **options):
            sent["options"] = options
            return self

    monkeypatch.setattr(course, "_client", FakeClient())
    answer = course.ask_model(JPEG, "context", "target", 12.5)
    assert answer == {"course_correction": 5, "confidence": 0.5, "comment": "ok"}
    assert sent["options"] == {"timeout": 12.5, "max_retries": 0}
    assert sent["text"]["format"]["schema"] == course.RESPONSE_SCHEMA
    content = sent["input"][0]["content"]
    assert "target" in content[0]["text"] and content[1]["image_url"].startswith("data:image/jpeg;base64,")
