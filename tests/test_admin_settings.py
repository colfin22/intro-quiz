"""POST /api/admin/game/settings — half-time trivia + difficulty (#79).

Driven by calling the endpoint directly: going through TestClient would fire the
app's startup hooks (clip sweep and friends), which this has nothing to do with.
"""
import asyncio
import os
import tempfile

import pytest
from fastapi import HTTPException

from app import db, game, main

REAL_CONNECT = db.connect          # captured before any monkeypatching


class FakeHub:
    """Just the bits the endpoint touches."""

    def __init__(self):
        self.trivia = True
        self.difficulty = game.DEFAULT_DIFFICULTY
        self.lock = asyncio.Lock()
        self.broadcasts = 0

    async def broadcast(self):
        self.broadcasts += 1


@pytest.fixture()
def env(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    REAL_CONNECT(path).close()                     # create the schema
    monkeypatch.setattr(db, "connect", lambda p=None: REAL_CONNECT(path))
    hub = FakeHub()
    monkeypatch.setattr(main, "hub", hub)
    yield hub, path
    os.unlink(path)


def call(body):
    return asyncio.run(main.api_admin_game_settings(body))


def stored(path, key):
    conn = REAL_CONNECT(path)
    try:
        return db.get_setting(conn, key)
    finally:
        conn.close()


def test_sets_difficulty_and_persists(env):
    hub, path = env
    out = call({"difficulty": "everything"})
    assert out["difficulty"] == "everything"
    assert hub.difficulty == "everything"
    assert hub.broadcasts == 1                     # phones hear about it at once
    assert stored(path, main.DIFFICULTY_SETTING) == "everything"


def test_sets_trivia_and_persists(env):
    hub, path = env
    call({"trivia": False})
    assert hub.trivia is False
    assert stored(path, main.TRIVIA_SETTING) == "0"


def test_both_at_once(env):
    _, path = env
    out = call({"trivia": False, "difficulty": "harder"})
    assert (out["trivia"], out["difficulty"]) == (False, "harder")
    assert stored(path, main.DIFFICULTY_SETTING) == "harder"


def test_unknown_difficulty_rejected_and_nothing_changes(env):
    hub, path = env
    with pytest.raises(HTTPException) as e:
        call({"difficulty": "impossible"})
    assert e.value.status_code == 400
    assert hub.difficulty == game.DEFAULT_DIFFICULTY
    assert hub.broadcasts == 0
    assert stored(path, main.DIFFICULTY_SETTING) is None


def test_empty_body_rejected(env):
    hub, _ = env
    with pytest.raises(HTTPException) as e:
        call({})
    assert e.value.status_code == 400
    assert hub.broadcasts == 0
