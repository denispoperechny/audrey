"""Picture tasks and pictures in an Azure storage container (the "camera" container of the
audreylogs account), with the same interface as store.LocalTaskStore.

Layout inside the container (under an optional prefix):
    tasks/<id>.json                        the task: id, status, params, times, ...
    pictures/<id>.jpg                      the picture, once uploaded
    queue/pending/<created_at>_<id>        an empty marker per pending task; the names sort
                                           oldest first

Nothing here relies on a lock in this process, so several copies of the service can share
the container:
  - Claiming a task deletes its pending marker. Storage lets only one delete of a blob
    succeed, so two cameras asking at once can't both get the same task.
  - Completing uploads the picture without overwrite. Only one upload per task can succeed.
  - Task records are rewritten with an ETag condition, so a change made in between by
    someone else is never silently overwritten (the update is retried on the fresh copy).
"""

import json
import uuid

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContentSettings

from store import ASSIGNED, DONE, PENDING, now_utc

JSON = ContentSettings(content_type="application/json")
JPEG = ContentSettings(content_type="image/jpeg")
UPDATE_RETRIES = 5


class BlobTaskStore:
    def __init__(self, account_url, container, prefix="", credential=None):
        if credential is None:
            from azure.identity import DefaultAzureCredential

            credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        self._container = BlobServiceClient(account_url, credential=credential).get_container_client(container)
        self._prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.where = "%s/%s/%s" % (account_url.rstrip("/"), container, self._prefix)

    # ---- names -------------------------------------------------------------------------

    def _task_name(self, task_id):
        return "%stasks/%s.json" % (self._prefix, task_id)

    def _picture_name(self, task_id):
        return "%spictures/%s.jpg" % (self._prefix, task_id)

    def _pending_name(self, task):
        return "%squeue/pending/%s_%s" % (self._prefix, task["created_at"], task["id"])

    # ---- task records ----------------------------------------------------------------------

    def _read(self, task_id):
        """(task, etag), or (None, None)."""
        try:
            download = self._container.get_blob_client(self._task_name(task_id)).download_blob()
        except ResourceNotFoundError:
            return None, None
        return json.loads(download.readall()), download.properties.etag

    def _update(self, task_id, change):
        """Applies change(task) -> task to the stored record, retrying if it was modified in
        between. Returns the new task, or None if it doesn't exist."""
        for _ in range(UPDATE_RETRIES):
            task, etag = self._read(task_id)
            if task is None:
                return None
            task = change(task)
            try:
                self._container.get_blob_client(self._task_name(task_id)).upload_blob(
                    json.dumps(task), overwrite=True, content_settings=JSON,
                    etag=etag, match_condition=MatchConditions.IfNotModified)
                return task
            except ResourceModifiedError:
                continue
        raise RuntimeError("task %s kept changing; update not applied" % task_id)

    # ---- the interface ---------------------------------------------------------------------

    def create(self, params, requested_by=None):
        task = {"id": str(uuid.uuid4()), "status": PENDING, "params": params, "requested_by": requested_by,
                "created_at": now_utc(), "assigned_at": None, "camera": None,
                "taken_at": None, "uploaded_at": None, "picture_bytes": None}
        self._container.get_blob_client(self._task_name(task["id"])).upload_blob(
            json.dumps(task), overwrite=False, content_settings=JSON)
        self._container.get_blob_client(self._pending_name(task)).upload_blob(b"", overwrite=False)
        return task

    def get(self, task_id):
        return self._read(task_id)[0]

    def list(self, status=None, limit=100):
        tasks = []
        for blob in self._container.list_blobs(name_starts_with=self._prefix + "tasks/"):
            if not blob.name.endswith(".json"):
                continue
            task = self.get(blob.name[len(self._prefix + "tasks/"):-len(".json")])
            if task is not None and (status is None or task["status"] == status):
                tasks.append(task)
        tasks.sort(key=lambda t: t["created_at"], reverse=True)
        return tasks[:limit]

    def claim_next(self, camera=None):
        for blob in self._container.list_blobs(name_starts_with=self._prefix + "queue/pending/"):
            try:
                self._container.delete_blob(blob.name)  # only one caller can delete it
            except ResourceNotFoundError:
                continue  # another camera took it first
            task_id = blob.name.rsplit("_", 1)[1]

            def assign(task):
                task.update(status=ASSIGNED, assigned_at=now_utc(), camera=camera)
                return task

            task = self._update(task_id, assign)
            if task is not None:
                return task
        return None

    def complete(self, task_id, taken_at, data):
        """Stores the picture and marks the task done. Returns (task, None), or (None, reason):
        "not found", or "not assigned (it is ...)"."""
        task = self.get(task_id)
        if task is None:
            return None, "not found"
        if task["status"] != ASSIGNED:
            return None, "not assigned (it is %s)" % task["status"]
        try:
            self._container.get_blob_client(self._picture_name(task_id)).upload_blob(
                data, overwrite=False, content_settings=JPEG)
        except ResourceExistsError:
            return None, "not assigned (it is %s)" % DONE  # another upload got there first

        def finish(task):
            task.update(status=DONE, taken_at=taken_at, uploaded_at=now_utc(), picture_bytes=len(data))
            return task

        return self._update(task_id, finish), None

    def picture(self, task_id):
        try:
            return self._container.get_blob_client(self._picture_name(task_id)).download_blob().readall()
        except ResourceNotFoundError:
            return None

    def delete_everything(self):
        """Removes everything under this store's prefix (for tests with a throwaway prefix)."""
        if not self._prefix:
            raise RuntimeError("refusing to delete a whole container")
        # Longest names first: on this account (hierarchical namespace) folders are entries
        # too, and a folder can only go once it is empty.
        names = sorted((b.name for b in self._container.list_blobs(name_starts_with=self._prefix)), key=len, reverse=True)
        for name in names + [self._prefix.rstrip("/")]:
            try:
                self._container.delete_blob(name)
            except ResourceNotFoundError:
                pass
