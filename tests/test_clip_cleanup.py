"""#90: clips of removed/replaced tracks are pruned after a grace period, and a
file replaced in place (same id, new audio) gets its clips re-cut."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import httpx

from app import clips, db, subsonic, sync

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return db.connect(path), path


def song(sid, **kw):
    s = {"id": sid, "title": sid, "artist": "Band", "album": "A", "duration": 200,
         "suffix": "flac", "samplingRate": 44100, "bitDepth": 16, "channelCount": 2,
         "bitRate": 900, "size": 30_000_000}
    s.update(kw)
    return s


def client_for(songs):
    """A Subsonic server with one album holding `songs` (a list, read at call time)."""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("ping.view"):
            return httpx.Response(200, json={"subsonic-response": {
                "status": "ok", "type": "navidrome", "serverVersion": "0.63.2"}})
        if path.endswith("getAlbumList2.view"):
            albums = [{"id": "al1", "name": "A"}] if request.url.params["offset"] == "0" else []
            return httpx.Response(200, json={"subsonic-response": {
                "status": "ok", "albumList2": {"album": albums} if albums else {}}})
        if path.endswith("getAlbum.view"):
            return httpx.Response(200, json={"subsonic-response": {
                "status": "ok", "album": {"song": list(songs)}}})
        raise AssertionError(f"unexpected call: {request.url}")
    return subsonic.Client(base_url="http://test", transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- signature

def test_signature_ignores_tag_edits_but_sees_new_audio():
    base = song("s")
    assert sync.source_signature(base) == sync.source_signature(
        song("s", size=31_500_000, bitRate=950))  # tags/cover art: bigger file, lossless
    assert sync.source_signature(base) != sync.source_signature(song("s", bitDepth=24, samplingRate=96000))
    assert sync.source_signature(base) != sync.source_signature(song("s", duration=203))  # other master
    assert sync.source_signature(song("s", suffix="mp3", bitRate=128)) != \
        sync.source_signature(song("s", suffix="mp3", bitRate=320))  # lossy re-encode
    assert sync.source_signature(base) != sync.source_signature(song("s", suffix="mp3", bitRate=320))
    assert sync.source_signature({"id": "s"}) is None  # server reports no format


# ---------------------------------------------------------------- sync side

def test_in_place_replacement_flags_a_recut():
    conn, path = make_db()
    songs = [song("s1"), song("s2")]
    client = client_for(songs)
    try:
        sync.sync_library(conn, client)
        conn.execute("UPDATE tracks SET clipped_at='2026-01-01', clip_start_s=0")
        conn.commit()
        # s1's file swapped for a hi-res copy; s2 only had its tags edited
        songs[0] = song("s1", bitDepth=24, samplingRate=96000, size=90_000_000)
        songs[1] = song("s2", size=31_000_000, bitRate=930)
        r = sync.sync_library(conn, client)
        assert r["replaced_in_place"] == 1
        flags = dict(conn.execute("SELECT id, source_changed FROM tracks").fetchall())
        assert flags == {"s1": 1, "s2": 0}
    finally:
        os.unlink(path)


def test_first_signature_is_recorded_not_treated_as_a_change():
    """Upgrading the app must not queue the whole library for re-cutting."""
    conn, path = make_db()
    try:
        conn.execute("INSERT INTO tracks(id,title,artist,clipped_at) VALUES('s1','s1','Band','2026-01-01')")
        conn.commit()
        r = sync.sync_library(conn, client_for([song("s1")]))
        assert "replaced_in_place" not in r
        row = conn.execute("SELECT source_sig, source_changed FROM tracks WHERE id='s1'").fetchone()
        assert row["source_sig"] and row["source_changed"] == 0
    finally:
        os.unlink(path)


def test_unclipped_track_changing_is_not_flagged():
    conn, path = make_db()
    songs = [song("s1")]
    client = client_for(songs)
    try:
        sync.sync_library(conn, client)
        songs[0] = song("s1", suffix="mp3", bitRate=320)
        r = sync.sync_library(conn, client)
        assert "replaced_in_place" not in r  # no clips yet: the first cut uses the new file anyway
        assert conn.execute("SELECT source_changed FROM tracks").fetchone()[0] == 0
    finally:
        os.unlink(path)


def test_inactive_since_set_cleared_and_backfilled():
    conn, path = make_db()
    songs = [song("s1"), song("s2")]
    client = client_for(songs)
    try:
        # a track that went inactive under an older version (no date kept)
        conn.execute("INSERT INTO tracks(id,title,artist,active) VALUES('old','old','X',0)")
        conn.commit()
        sync.sync_library(conn, client)
        assert conn.execute("SELECT inactive_since FROM tracks WHERE id='old'").fetchone()[0]
        songs.pop()  # s2 removed
        sync.sync_library(conn, client)
        assert conn.execute("SELECT inactive_since FROM tracks WHERE id='s2'").fetchone()[0]
        songs.append(song("s2"))  # ...and back
        sync.sync_library(conn, client)
        row = conn.execute("SELECT active, inactive_since FROM tracks WHERE id='s2'").fetchone()
        assert row["active"] == 1 and row["inactive_since"] is None
        assert db.get_setting(conn, "sync_ok_at")
    finally:
        os.unlink(path)


def test_refused_sync_is_recorded():
    conn, path = make_db()
    songs = [song(f"s{i}") for i in range(30)]
    client = client_for(songs)
    try:
        sync.sync_library(conn, client)
        del songs[2:]  # 28 of 30 vanish: the guard refuses
        try:
            sync.sync_library(conn, client)
            raise AssertionError("guard should have refused")
        except sync.SyncGuardError:
            pass
        assert db.get_setting(conn, "sync_refused_at") >= db.get_setting(conn, "sync_ok_at")
    finally:
        os.unlink(path)


# ---------------------------------------------------------------- re-cut

def test_replaced_track_is_recut_and_flag_cleared(tmp_path, monkeypatch):
    conn, path = make_db()
    try:
        conn.execute("INSERT INTO tracks(id,title,artist,tier,clipped_at,clip_start_s,source_changed) "
                     "VALUES('r','r','a','easy','2026-01-01',0,1)")
        conn.execute("INSERT INTO tracks(id,title,artist,tier,clipped_at,clip_start_s) "
                     "VALUES('ok','ok','a','easy','2026-01-01',0)")
        conn.commit()
        cut = []
        monkeypatch.setattr(clips, "cut_track", lambda client, row, d=None: cut.append(row["id"]) or 0.0)
        r = clips.cut_batch(conn, None, clips_dir=str(tmp_path))
        assert cut == ["r"] and r["remaining"] == 0
        assert conn.execute("SELECT source_changed FROM tracks WHERE id='r'").fetchone()[0] == 0
    finally:
        os.unlink(path)


# ---------------------------------------------------------------- prune

def _clip_dir(root, name, age_days=0):
    d = root / name
    d.mkdir()
    (d / "5.mp3").write_bytes(b"x" * 1000)
    t = (NOW - timedelta(days=age_days)).timestamp()
    os.utime(d, (t, t))
    return d


def _prune_db(tracks):
    """tracks: (id, active, inactive_days_ago or None)"""
    conn, path = make_db()
    for tid, active, days in tracks:
        since = (NOW - timedelta(days=days)).isoformat() if days is not None else None
        conn.execute("INSERT INTO tracks(id,title,artist,active,inactive_since,clipped_at) "
                     "VALUES(?,?,?,?,?,'2026-01-01')", (tid, tid, "a", active, since))
    conn.commit()
    db.set_setting(conn, "sync_ok_at", NOW.isoformat())
    return conn, path


def test_prune_deletes_only_what_waited_long_enough(tmp_path):
    conn, path = _prune_db([("live", 1, None), ("gone_old", 0, 45), ("gone_new", 0, 3)])
    for n in ("live", "gone_old", "gone_new"):
        _clip_dir(tmp_path, n)
    _clip_dir(tmp_path, "orphan_old", age_days=60)
    _clip_dir(tmp_path, "orphan_new", age_days=2)
    (tmp_path / ".snapshots").mkdir()          # never touched
    (tmp_path / "stray.txt").write_text("x")   # not a folder: never touched
    try:
        r = clips.prune(conn, clips_dir=str(tmp_path), after_days=30, now=NOW)
        assert r["removed_tracks"] == 1 and r["orphan_folders"] == 1 and r["still_waiting"] == 2
        assert r["freed_mb"] == 0.0  # 2 KB rounds to 0.0 MB
        left = sorted(p.name for p in tmp_path.iterdir())
        assert left == [".snapshots", "gone_new", "live", "orphan_new", "stray.txt"]
        clipped = dict(conn.execute("SELECT id, clipped_at FROM tracks").fetchall())
        assert clipped["gone_old"] is None and clipped["gone_new"] and clipped["live"]
    finally:
        os.unlink(path)


def test_prune_dry_run_deletes_nothing(tmp_path):
    conn, path = _prune_db([("live", 1, None), ("gone_old", 0, 45)])
    _clip_dir(tmp_path, "live")
    _clip_dir(tmp_path, "gone_old")
    try:
        r = clips.prune(conn, clips_dir=str(tmp_path), after_days=30, dry_run=True, now=NOW)
        assert r["dry_run"] and r["removed_tracks"] == 1
        assert (tmp_path / "gone_old").exists()
        assert conn.execute("SELECT clipped_at FROM tracks WHERE id='gone_old'").fetchone()[0]
    finally:
        os.unlink(path)


def test_prune_refuses_without_a_good_sync(tmp_path):
    conn, path = _prune_db([("gone_old", 0, 45)])
    _clip_dir(tmp_path, "gone_old")
    try:
        conn.execute("DELETE FROM settings")
        conn.commit()
        assert "skipped" in clips.prune(conn, clips_dir=str(tmp_path), after_days=30, now=NOW)
        db.set_setting(conn, "sync_ok_at", (NOW - timedelta(hours=2)).isoformat())
        db.set_setting(conn, "sync_refused_at", (NOW - timedelta(hours=1)).isoformat())
        assert "skipped" in clips.prune(conn, clips_dir=str(tmp_path), after_days=30, now=NOW)
        assert (tmp_path / "gone_old").exists()
    finally:
        os.unlink(path)


def test_prune_refuses_to_wipe_most_of_the_clips(tmp_path):
    tracks = [(f"g{i}", 0, 45) for i in range(15)] + [(f"l{i}", 1, None) for i in range(10)]
    conn, path = _prune_db(tracks)
    for tid, _a, _d in tracks:
        _clip_dir(tmp_path, tid)
    try:
        r = clips.prune(conn, clips_dir=str(tmp_path), after_days=30, now=NOW)
        assert "refused" in r["skipped"]
        assert len(list(tmp_path.iterdir())) == 25
    finally:
        os.unlink(path)


def test_prune_disabled_with_zero(tmp_path):
    conn, path = _prune_db([("gone_old", 0, 45)])
    _clip_dir(tmp_path, "gone_old")
    try:
        assert "disabled" in clips.prune(conn, clips_dir=str(tmp_path), after_days=0, now=NOW)["skipped"]
        assert (tmp_path / "gone_old").exists()
    finally:
        os.unlink(path)
