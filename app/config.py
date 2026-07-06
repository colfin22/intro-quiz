import os

NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "http://navidrome.local:4533").rstrip("/")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "quiz")
NAVIDROME_PASSWORD = os.environ.get("NAVIDROME_PASSWORD", "")
CLIENT_NAME = "intro-quiz"
DB_PATH = os.environ.get("QUIZ_DB", "/data/quiz.db")
CLIPS_DIR = os.environ.get("CLIPS_DIR", "/clips")
