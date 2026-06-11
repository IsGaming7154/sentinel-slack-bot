import os

from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_MODEL = "claude-3-5-sonnet-latest"
GEMINI_MODEL = "gemini-2.5-flash"

BOTH_LLMS_DOWN = "Sorry, both Claude and Gemini are unavailable right now."

DB_PATH = "data.db"

# Proactive incident monitor
INCIDENT_MONITOR = os.environ.get("INCIDENT_MONITOR") == "1"
POLL_INTERVAL_SECS = 15
WINDOW_MINUTES = 10
CASCADE_THRESHOLD = 3
ERROR_KEYWORDS = ["error", "timeout", "bug"]
ALERTS_CHANNEL = os.environ.get("ALERTS_CHANNEL", "#alerts")
COOLDOWN_SECS = 300
