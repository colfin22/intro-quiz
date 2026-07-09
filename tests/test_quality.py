import os
import tempfile

from app import db, quality


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = db.connect(path)
    rows = [
        # famous artist: one huge track + one junk-titled zero-scorer
        ("f1", "Big Hit", "Famous Band", 2_000_000),
        ("f2", "Big Hit (JUNKTAG)", "Famous Band", 0),
        # obscure artist: everything low — legitimately unknown, not a miss
        ("o1", "Deep Cut", "Obscure Act", 4),
        ("o2", "Deeper Cut", "Obscure Act", 9),
        # famous artist, unscored track (NULL) — sweep hasn't reached it; not a suspect
        ("f3", "Unscored", "Famous Band", None),
    ]
    for tid, title, artist, listeners in rows:
        conn.execute(
            "INSERT INTO tracks(id,title,artist,album,year,tier,global_listeners,active) "
            "VALUES(?,?,?,?,2000,'medium',?,1)", (tid, title, artist, "A", listeners))
    conn.commit()
    return conn, path


def test_find_suspects_outcome_based():
    conn, p = make_db()
    try:
        ids = [s["id"] for s in quality.find_suspects(conn)]
        assert ids == ["f2"]  # junk-titled track of the famous artist only
    finally:
        conn.close(); os.unlink(p)


def test_banned_and_inactive_excluded():
    conn, p = make_db()
    try:
        conn.execute("UPDATE tracks SET banned=1 WHERE id='f2'")
        conn.commit()
        assert quality.find_suspects(conn) == []
    finally:
        conn.close(); os.unlink(p)


def test_check_pushes_once(monkeypatch):
    conn, p = make_db()
    sent = []
    monkeypatch.setattr(quality.ha, "notify", lambda t, m: sent.append((t, m)) or True)
    try:
        r1 = quality.check(conn)
        assert r1 == {"suspects": 1, "fresh": 1, "pushed": True}
        assert "Famous Band — Big Hit (JUNKTAG)" in sent[0][1]
        # second run: same suspect still listed, but no re-push
        r2 = quality.check(conn)
        assert r2 == {"suspects": 1, "fresh": 0, "pushed": False}
        assert len(sent) == 1
    finally:
        conn.close(); os.unlink(p)


def test_baseline_marks_without_push(monkeypatch):
    conn, p = make_db()
    sent = []
    monkeypatch.setattr(quality.ha, "notify", lambda t, m: sent.append(1) or True)
    try:
        r = quality.check(conn, push=False)
        assert r == {"suspects": 1, "fresh": 1, "pushed": False}
        assert not sent
        assert quality.check(conn) == {"suspects": 1, "fresh": 0, "pushed": False}
    finally:
        conn.close(); os.unlink(p)
