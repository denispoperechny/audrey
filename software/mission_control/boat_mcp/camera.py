"""The camera service (software/camera_service), for the newest streamed picture.

    GET <CAMERA_SERVICE_URL>/stream/latest?since=<UTC time>   the newest picture taken after that
                                                               time (with the course marks), or 204

CAMERA_SERVICE_URL defaults to the deployed service's /api over plain HTTP.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timezone

DEFAULT_URL = "http://camera-service.orangepond-88aa4066.eastus2.azurecontainerapps.io/api"


@dataclass
class Picture:
    jpeg: bytes
    name: str  # the camera service's name for it: GET /stream/pictures/<name>
    taken_at: str  # ISO 8601 UTC
    taken_unix: float


def iso(unix):
    return datetime.fromtimestamp(unix, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CameraClient:
    def __init__(self, base_url=None, timeout_s=3.0):
        import httpx

        self.base_url = (base_url or os.environ.get("CAMERA_SERVICE_URL") or DEFAULT_URL).rstrip("/")
        self.http = httpx.Client(timeout=timeout_s)

    def picture_between(self, start, end):
        """The newest picture taken from `start` on, if it was taken by `end` (unix seconds); None
        if there is none yet, or the newest was taken later. One request: the picture itself.

        "The newest" is enough: the camera sends its pictures one at a time, in the order taken,
        so one taken after `end` can only arrive after any taken by then, and a caller polling
        faster than the camera takes pictures sees those first."""
        picture = self.latest_since(start)
        return picture if picture is not None and picture.taken_unix <= end else None

    def latest_since(self, unix):
        """The newest picture taken at or after `unix` (seconds), or None if there is none yet."""
        # The service's `since` means "taken after": a millisecond earlier includes a picture
        # taken at exactly that moment.
        r = self.http.get(self.base_url + "/stream/latest", params={"since": iso(unix - 0.001)})
        if r.status_code == 204:
            return None
        r.raise_for_status()
        taken_at = r.headers["x-taken-at"]
        return Picture(r.content, r.headers["x-picture-name"], taken_at, datetime.fromisoformat(taken_at).timestamp())

    def picture_url(self, name):
        return "%s/stream/pictures/%s" % (self.base_url, name)
