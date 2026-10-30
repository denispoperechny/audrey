"""Mission control's settings are environment variables; .env.example lists them all.

Importing this module reads software/mission_control/.env (gitignored; copy .env.example) into
the environment. Variables already set win over the file, so in Azure the container app's own
settings apply. Every package imports it in its __init__.py, so .env is read before any module
looks at the environment, whichever process starts.
"""

from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent  # software/mission_control

load_dotenv(APP_DIR / ".env")
