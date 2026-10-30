"""The lookout: a background process (observer.py) that looks at the camera's latest picture every
10 s and writes notes on the surroundings (notes.py), which the captain reads on each wake-up."""

import settings  # noqa: F401  (reads .env before anything looks at the environment)
