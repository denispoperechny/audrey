"""pytest: no traces from the tests (tracing.py), whatever .env says; TRACE_TEST_DIR=<folder> to
see what they would write."""

import os

os.environ["TRACE_DIR"] = os.environ.get("TRACE_TEST_DIR") or "off"
