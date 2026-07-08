import os

NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "http://navidrome.local:4533").rstrip("/")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "quiz")
NAVIDROME_PASSWORD = os.environ.get("NAVIDROME_PASSWORD", "")
CLIENT_NAME = "intro-quiz"
DB_PATH = os.environ.get("QUIZ_DB", "/data/quiz.db")
CLIPS_DIR = os.environ.get("CLIPS_DIR", "/clips")
# half-time trivia: set false to skip the shipped (Irish/UK-centric) pack and run
# purely on your own data/trivia_custom.json + the Open Trivia DB top-up
TRIVIA_BUILTIN_PACK = os.environ.get("TRIVIA_BUILTIN_PACK", "true").lower() not in ("false", "0", "no")
# run-once bulk clip cutter at startup: cuts until every tiered track has clips,
# then stops. Safe to leave set — a start with nothing to cut exits immediately.
CLIP_SWEEP_ON_START = os.environ.get("CLIP_SWEEP_ON_START", "false").lower() in ("true", "1", "yes")
# optional cap on that session's length in hours (0 = run until the pool is dry);
# a capped run stops cleanly after the current batch and resumes on next start
CLIP_SWEEP_MAX_HOURS = float(os.environ.get("CLIP_SWEEP_MAX_HOURS", "0") or 0)
