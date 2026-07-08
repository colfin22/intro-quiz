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
