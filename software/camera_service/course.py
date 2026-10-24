"""Course correction from a fresh camera picture: the MCP tool get_course_correction.

    1. orders a picture (the default parameters) and waits for the camera to deliver it
    2. draws the course marks on it (marks.py: red boxes at -25°, 0° and +25°)
    3. shows it to a vision model in Azure AI Foundry with the caller's context, instructions
       and course target, and asks how many degrees to turn
All of it within TOOL_TIMEOUT_S; past that the tool gives up and says which step was still
going (a picture order left pending is taken by the camera when it next asks).

The model is an Azure OpenAI deployment (FOUNDRY_OPENAI_URL, FOUNDRY_MODEL in .env), reached
with DefaultAzureCredential: your az login locally, the container app's managed identity in
Azure (role "Cognitive Services OpenAI User" on the Foundry resource; key access is off there).
"""

import base64
import json
import os
import time

import anyio
from pydantic import BaseModel, Field

import marks

DEFAULT_OPENAI_URL = "https://temp-test-01-resource.openai.azure.com/openai/v1/"
DEFAULT_MODEL = "gpt-6-astra"
TOOL_TIMEOUT_S = 30  # the whole call: picture and model; no answer by then is an error
CAMERA_POLL_S = 0.5
REQUESTED_BY = "mcp get_course_correction"

INSTRUCTIONS = """You steer Audrey, a small autonomous boat, from pictures taken by its camera.
The camera is fixed on the boat and looks straight ahead over the bow. Red boxes are drawn on
the picture with their angles written above them: "0°" is straight ahead (the boat's current
heading), "-25°" is 25 degrees to the left (port) and "+25°" is 25 degrees to the right
(starboard). The picture covers about 43 degrees either side of straight ahead; judge
directions between and beyond the boxes by the same scale.

Answer with:
  course_correction  degrees to turn so the boat heads where the course target asks:
                     positive = turn right (starboard), negative = turn left (port), 0 = keep
                     going. Judge it from the red boxes.
  confidence         0 to 1: how sure you are. Low if the target isn't clearly in the picture,
                     the picture is dark, blurred or ambiguous, or the target is outside the view
                     (then give your best guess of the direction to turn, or 0).
  comment            a short explanation: what you see, where the target is relative to the
                     boxes, and anything the operator should know."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "course_correction": {"type": "number", "description": "degrees, + = turn right (starboard)"},
        "confidence": {"type": "number", "description": "0 to 1"},
        "comment": {"type": "string"},
    },
    "required": ["course_correction", "confidence", "comment"],
    "additionalProperties": False,
}


class CourseCorrection(BaseModel):
    course_correction: float = Field(description="degrees to turn: positive = right (starboard), negative = left (port)")
    confidence: float = Field(ge=0, le=1, description="0 to 1, how sure the model is")
    comment: str = Field(description="what the model saw and why it suggests this correction")
    task_id: str = Field(description="the picture task; its picture: GET /api/picture-tasks/<task_id>/picture")
    picture_taken_at: str = Field(description="when the camera took the picture (UTC)")


class CourseTimeout(Exception):
    pass


def in_thread(function, *args):
    """Runs a blocking call in a worker thread; a timeout doesn't wait for it to return."""
    return anyio.to_thread.run_sync(lambda: function(*args), abandon_on_cancel=True)


async def take_picture(store, params, progress):
    """Orders a picture with these parameters and waits for it. Returns the done task."""
    task = await in_thread(store.create, params, REQUESTED_BY)
    progress["task_id"] = task["id"]
    while True:
        current = await in_thread(store.get, task["id"])
        if current and current["status"] == "done":
            return current
        await anyio.sleep(CAMERA_POLL_S)


_client = None


def model_client():
    global _client
    if _client is None:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        from openai import OpenAI

        token = get_bearer_token_provider(DefaultAzureCredential(exclude_interactive_browser_credential=True),
                                          "https://cognitiveservices.azure.com/.default")
        _client = OpenAI(base_url=os.environ.get("FOUNDRY_OPENAI_URL", DEFAULT_OPENAI_URL), api_key=token)
    return _client


def ask_model(jpeg, context_and_instructions, course_target, timeout_s=TOOL_TIMEOUT_S):
    """The model's answer for this picture: {"course_correction", "confidence", "comment"}."""
    picture = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    # Within the tool's time limit, so the request doesn't outlive it; no retries for the same reason.
    response = model_client().with_options(timeout=timeout_s, max_retries=0).responses.create(
        model=os.environ.get("FOUNDRY_MODEL", DEFAULT_MODEL),
        instructions=INSTRUCTIONS,
        input=[{"role": "user", "content": [
            {"type": "input_text", "text": "Context and instructions:\n%s\n\nCourse target:\n%s"
                                           % (context_and_instructions or "(none)", course_target)},
            {"type": "input_image", "image_url": picture, "detail": "high"},
        ]}],
        text={"format": {"type": "json_schema", "name": "course_correction", "schema": RESPONSE_SCHEMA, "strict": True}},
    )
    return json.loads(response.output_text)


async def get_course_correction(store, params, context_and_instructions, course_target):
    progress = {"step": "the camera didn't deliver the picture", "task_id": None}
    deadline = time.monotonic() + TOOL_TIMEOUT_S
    try:
        with anyio.fail_after(TOOL_TIMEOUT_S):
            task = await take_picture(store, params, progress)
            jpeg = await in_thread(store.picture, task["id"])
            progress["step"] = "the model didn't answer"
            answer = await in_thread(ask_model, marks.marked(jpeg), context_and_instructions, course_target,
                                     max(1.0, deadline - time.monotonic()))
    except TimeoutError:
        raise CourseTimeout("no answer within %g s: %s (picture task %s)%s" % (
            TOOL_TIMEOUT_S, progress["step"], progress["task_id"],
            "; is the camera on and online?" if progress["step"].startswith("the camera") else ""))
    return CourseCorrection(
        course_correction=float(answer["course_correction"]),
        confidence=min(1.0, max(0.0, float(answer["confidence"]))),
        comment=answer["comment"],
        task_id=task["id"],
        picture_taken_at=task["taken_at"],
    )
