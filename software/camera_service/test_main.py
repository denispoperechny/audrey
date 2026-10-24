"""python -m pytest -q                         local storage, a throwaway database and folder
   CAMERA_TEST_AZURE=1 python -m pytest -q   the same tests against the Azure container from
                                             .env, in a throwaway folder that is deleted after"""

import os
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
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


@pytest.fixture(scope="session", autouse=True)
def clean_up_azure():
    yield
    if AZURE:
        main.store.delete_everything()

client = TestClient(main.app, base_url="http://testserver" + main.API_PREFIX)
DEFAULTS = {"frame_size": "FHD", "jpeg_quality": 8, "hmirror": True, "vflip": False, "settle_ms": 1000,
            "gain_ceiling": "32X", "night_mode": False, "ae_level": 2}
JPEG = b"\xff\xd8\xff\xe0" + b"fake jpeg body" * 100 + b"\xff\xd9"


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
    got = client.get("/picture-tasks/%s/picture" % task["id"])
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
