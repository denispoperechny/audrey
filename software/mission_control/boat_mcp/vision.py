"""A vision model in Azure AI Foundry looks at a camera picture and answers a request about it.

The model is an Azure OpenAI deployment (FOUNDRY_OPENAI_URL, FOUNDRY_MODEL), reached with
DefaultAzureCredential: your az login locally, the container's managed identity in Azure (it
needs "Cognitive Services OpenAI User" on the Foundry resource; key access is off there).
"""

import base64
import os
import time

import tracing

DEFAULT_OPENAI_URL = "https://temp-test-01-resource.openai.azure.com/openai/v1/"
DEFAULT_MODEL = "gpt-6-astra"

INSTRUCTIONS = """You look through the forward camera of Audrey, a small autonomous boat, for its
navigator. The camera is fixed on the boat and looks straight ahead over the bow; when the picture
was taken the bow pointed at the azimuth given (degrees clockwise from true north). Red boxes are
drawn on the picture with their angles above them: "0°" is straight ahead, "-25°" is 25 degrees to
the left (port) and "+25°" 25 degrees to the right (starboard); the picture covers about 43 degrees
either side. Give directions in those terms, or as azimuths. Answer the request directly and
briefly, say what you can't make out, and don't guess beyond what the picture shows."""

_client = None


def client():
    global _client
    if _client is None:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        from openai import OpenAI

        token = get_bearer_token_provider(DefaultAzureCredential(exclude_interactive_browser_credential=True),
                                          "https://cognitiveservices.azure.com/.default")
        _client = OpenAI(base_url=os.environ.get("FOUNDRY_OPENAI_URL", DEFAULT_OPENAI_URL), api_key=token)
    return _client


def ask(jpeg, request_note, azimuth_deg, timeout_s):
    """The model's answer (text) to request_note about the picture. Raises on a timeout or an error."""
    picture = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    started = time.monotonic()
    response = client().with_options(timeout=timeout_s, max_retries=0).responses.create(
        model=os.environ.get("FOUNDRY_MODEL", DEFAULT_MODEL),
        instructions=INSTRUCTIONS,
        input=[{"role": "user", "content": [
            {"type": "input_text", "text": "The bow pointed at azimuth %.0f°.\n\nRequest:\n%s" % (azimuth_deg, request_note)},
            {"type": "input_image", "image_url": picture, "detail": "high"},
        ]}],
    )
    tracing.event("model-call", purpose="visual_observe", model=response.model, tokens=tracing.usage_of(response),
                  model_s=round(time.monotonic() - started, 2), request_note=request_note, answer=response.output_text)
    return response.output_text
