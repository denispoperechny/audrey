"""Course correction from the camera's latest picture: the MCP tool get_course_correction.

    1. takes the newest picture the camera streamed (POST /api/stream/pictures; the service
       keeps the latest few in memory), and notes how old it is
    2. draws the course marks on it (marks.py: red boxes at -25°, 0° and +25°)
    3. shows it to a vision model in Azure AI Foundry with the caller's context, instructions
       and course target, and asks how many degrees to turn
The model has TOOL_TIMEOUT_S to answer; past that the tool gives up. With no streamed picture
in the last hour it fails straight away.

The model is an Azure OpenAI deployment (FOUNDRY_OPENAI_URL, FOUNDRY_MODEL in .env), reached
with DefaultAzureCredential: your az login locally, the container app's managed identity in
Azure (role "Cognitive Services OpenAI User" on the Foundry resource; key access is off there).
"""

import base64
import json
import os
from datetime import datetime, timezone

import anyio
from pydantic import BaseModel, Field

import marks

DEFAULT_OPENAI_URL = "https://temp-test-01-resource.openai.azure.com/openai/v1/"
DEFAULT_MODEL = "gpt-6-astra"
TOOL_TIMEOUT_S = 30  # for the model's answer; none by then is an error

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
    picture: str = Field(description="the streamed picture it is based on: GET /api/stream/pictures/<picture>")
    picture_taken_at: str = Field(description="when the camera took the picture (UTC)")
    picture_age_s: float = Field(description="how old the picture was when it was given to the model, in seconds")


class CourseError(Exception):
    """The tool can't answer: no picture, or no answer from the model in time."""


def in_thread(function, *args):
    """Runs a blocking call in a worker thread; a timeout doesn't wait for it to return."""
    return anyio.to_thread.run_sync(lambda: function(*args), abandon_on_cancel=True)


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


async def get_course_correction(stream, context_and_instructions, course_target):
    """stream: the service's stream store (stream_store.RecentCache)."""
    newest = await in_thread(stream.list, None, 1)
    jpeg = await in_thread(stream.picture, newest[0]["name"]) if newest else None
    if jpeg is None:
        raise CourseError("no picture from the camera in the last hour: is it streaming?")
    item = newest[0]
    age_s = (datetime.now(timezone.utc) - datetime.fromisoformat(item["taken_at"])).total_seconds()
    try:
        with anyio.fail_after(TOOL_TIMEOUT_S):
            answer = await in_thread(ask_model, marks.marked(jpeg), context_and_instructions, course_target, TOOL_TIMEOUT_S)
    except TimeoutError:
        raise CourseError("the model didn't answer within %g s (picture %s)" % (TOOL_TIMEOUT_S, item["name"]))
    return CourseCorrection(
        course_correction=float(answer["course_correction"]),
        confidence=min(1.0, max(0.0, float(answer["confidence"]))),
        comment=answer["comment"],
        picture=item["name"],
        picture_taken_at=item["taken_at"],
        picture_age_s=round(age_s, 2),
    )
