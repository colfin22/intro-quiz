# Local development on a laptop — no Docker, no Navidrome needed.
#
#   make setup     one-off: venv + deps (needs ffmpeg: brew install ffmpeg)
#   make seed      build ./devdata/quiz.db + ./devclips with fake tone tracks
#   make dev       run the app on http://localhost:8000 with reload
#   make test      the full suite (python + the node render smokes)
#
# Dev config lives in .env.local (gitignored, created by `make dev` if absent).
# Production config stays in .env and is never read here.

PY      := .venv/bin/python
UVICORN := .venv/bin/uvicorn
PORT    ?= 8000

# Dev-only paths: the container defaults are /data and /clips (app/config.py).
export QUIZ_DB   ?= ./devdata/quiz.db
export CLIPS_DIR ?= ./devclips

.PHONY: setup seed dev test test-js lint clean-dev

setup:
	python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements.txt pytest
	@command -v ffmpeg >/dev/null || echo "⚠️  ffmpeg missing — brew install ffmpeg"
	@echo "✅ setup done"

seed:
	$(PY) scripts/seed_dev_db.py $(SEED_ARGS)

.env.local:
	@cp .env.local.example .env.local
	@echo "created .env.local — edit it if you want real HA/Navidrome from dev"

# --env-file keeps dev config out of the shell and out of .env.
dev: .env.local
	@test -f $(QUIZ_DB) || $(MAKE) seed
	$(UVICORN) app.main:app --reload --host 0.0.0.0 --port $(PORT) --env-file .env.local

test:
	$(PY) -m pytest tests/ -q

test-js:
	node tests/js/render_smoke.js && node tests/js/admin_smoke.js

clean-dev:
	rm -rf devdata devclips
