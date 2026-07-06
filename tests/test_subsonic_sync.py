import hashlib
import json
import os
import tempfile

import httpx

from app import db, subsonic, sync


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return db.connect(path), path


def test_auth_params_salted_token():
    p = subsonic.auth_params(user="u", password="sekrit")
    assert p["u"] == "u"
    assert "p" not in p  # never plaintext
    assert p["t"] == hashlib.md5(("sekrit" + p["s"]).encode()).hexdigest()


ALBUMS_PAGE = {"subsonic-response": {"status": "ok", "albumList2": {"album": [
    {"id": "al1", "name": "First Album"}, {"id": "al2", "name": "Second"}]}}}
ALBUMS_EMPTY = {"subsonic-response": {"status": "ok", "albumList2": {}}}
SONGS = {
    "al1": {"subsonic-response": {"status": "ok", "album": {"song": [
        {"id": "s1", "title": "Opener", "artist": "Band", "album": "First Album",
         "genre": "Rock", "year": 1999, "duration": 201, "coverArt": "c1"},
        {"id": "s2", "title": "Deep Cut", "artist": "Band", "album": "First Album",
         "duration": 187}]}}},
    "al2": {"subsonic-response": {"status": "ok", "album": {"song": [
        {"id": "s3", "title": "Hit", "artist": "Other", "album": "Second",
         "genre": "Pop", "year": 2004, "duration": 154, "coverArt": "c3"}]}}},
}


def fake_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getAlbumList2.view"):
            page = ALBUMS_PAGE if request.url.params["offset"] == "0" else ALBUMS_EMPTY
            return httpx.Response(200, json=page)
        if request.url.path.endswith("getAlbum.view"):
            return httpx.Response(200, json=SONGS[request.url.params["id"]])
        raise AssertionError(f"unexpected call: {request.url}")
    return httpx.MockTransport(handler)


def test_sync_upserts_and_deactivates():
    conn, path = make_db()
    try:
        client = subsonic.Client(base_url="http://test", transport=fake_transport())
        r = sync.sync_library(conn, client)
        assert r == {"albums": 2, "tracks_active": 3, "deactivated": 0}
        row = conn.execute("SELECT * FROM tracks WHERE id='s1'").fetchone()
        assert row["genre"] == "Rock" and row["year"] == 1999 and row["album_id"] == "al1"
        # a pre-existing track missing from this pass gets deactivated, scores kept
        conn.execute("INSERT INTO tracks(id,title,artist,play_count) VALUES('gone','Old','X',9)")
        conn.commit()
        r = sync.sync_library(conn, client)
        assert r["deactivated"] == 1
        gone = conn.execute("SELECT active, play_count FROM tracks WHERE id='gone'").fetchone()
        assert gone["active"] == 0 and gone["play_count"] == 9
    finally:
        os.unlink(path)


def test_error_status_raises():
    def handler(request):
        return httpx.Response(200, json={"subsonic-response": {
            "status": "failed", "error": {"code": 40, "message": "Wrong username or password"}}})
    client = subsonic.Client(base_url="http://test", transport=httpx.MockTransport(handler))
    try:
        client.ping()
        raise AssertionError("should have raised")
    except subsonic.SubsonicError as e:
        assert "Wrong username" in str(e)
