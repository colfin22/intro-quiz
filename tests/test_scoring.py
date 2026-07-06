import os
import tempfile

import httpx

from app import db, lastfm, scoring


def make_db(tracks):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = db.connect(path)
    for t in tracks:
        conn.execute(
            "INSERT INTO tracks(id,title,artist,active,play_count,starred,global_listeners) "
            "VALUES(?,?,?,?,?,?,?)",
            (t["id"], t.get("title", "t"), t.get("artist", "a"), t.get("active", 1),
             t.get("play_count", 0), t.get("starred", 0), t.get("global_listeners")))
    conn.commit()
    return conn, path


def test_ingest_annotations_resets_and_matches():
    conn, p = make_db([{"id": "s1", "play_count": 9}, {"id": "s2"}])
    try:
        r = scoring.ingest_annotations(conn, [{"id": "s2", "play_count": 4, "starred": 1},
                                              {"id": "unknown", "play_count": 2}])
        assert r == {"received": 2, "matched": 1}
        rows = {x["id"]: x for x in conn.execute("SELECT * FROM tracks")}
        assert rows["s1"]["play_count"] == 0  # stale count wiped by the reset
        assert rows["s2"]["play_count"] == 4 and rows["s2"]["starred"] == 1
    finally:
        os.unlink(p)


def test_tiers():
    conn, p = make_db([
        {"id": "fam", "play_count": 3, "global_listeners": 10},
        {"id": "star", "starred": 1, "global_listeners": 10},
        {"id": "big", "global_listeners": 500_000},
        {"id": "mid", "global_listeners": 50_000},
        {"id": "obscure", "global_listeners": 12},
        {"id": "nolastfm", "global_listeners": 0},
        {"id": "unscored"},  # global_listeners NULL
        {"id": "gone", "active": 0, "global_listeners": 500_000},
    ])
    try:
        counts = scoring.assign_tiers(conn)
        tiers = {x["id"]: x["tier"] for x in conn.execute("SELECT id, tier FROM tracks")}
        assert tiers["fam"] == "easy" and tiers["star"] == "easy"
        assert tiers["big"] == "medium"
        assert tiers["mid"] == "hard"
        assert tiers["obscure"] == "tiebreak"
        assert tiers["nolastfm"] is None      # Last.fm never heard of it — not quizzable
        assert tiers["unscored"] is None
        assert tiers["gone"] is None          # inactive never tiered
        assert counts == {"easy": 2, "medium": 1, "hard": 1, "tiebreak": 1}
    finally:
        os.unlink(p)


def test_lastfm_batch_caches_zero_for_unknown(monkeypatch):
    monkeypatch.setattr(lastfm, "API_KEY", "test-key")
    def handler(request):
        if request.url.params["track"] == "Known":
            return httpx.Response(200, json={"track": {"listeners": "123456", "playcount": "999"}})
        return httpx.Response(200, json={"error": 6, "message": "Track not found"})
    conn, p = make_db([{"id": "k", "title": "Known"}, {"id": "u", "title": "Unknown"}])
    try:
        http = httpx.Client(transport=httpx.MockTransport(handler))
        r = lastfm.score_batch(conn, http=http, delay_s=0)
        assert r == {"scored": 2, "errors": 0, "remaining": 0}
        rows = {x["id"]: x for x in conn.execute("SELECT * FROM tracks")}
        assert rows["k"]["global_listeners"] == 123456
        assert rows["u"]["global_listeners"] == 0  # cached, won't refetch
    finally:
        os.unlink(p)
