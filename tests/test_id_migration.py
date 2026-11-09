"""#87: Navidrome 0.64 re-encodes every id — the quiz must follow, not orphan."""
import os
import tempfile

import httpx
import pytest

from app import db, id_migration, subsonic, sync
from app.navidrome_ids import canonical_id

# 25 legacy (pre-0.64) hex song ids across 3 albums; enough to arm the sync guard
LEGACY = [f"{i:032x}" for i in range(0xa1b2c3d4e5f60718293a4b5c6d7e8f00, 0xa1b2c3d4e5f60718293a4b5c6d7e8f00 + 25)]
ALBUMS_OLD = ["0000000000000000000a01", "0000000000000000000a02", "zzzzzzzzzzzzzzzzzzzzzz"]


def album_of(i):
    return i % 3


class FakeNavidrome:
    """Serves the same library under pre-0.64 or 0.64+ ids."""

    def __init__(self, version, canonical):
        self.version = version
        self.canonical = canonical
        self.calls = []

    def sid(self, i):
        return canonical_id(LEGACY[i]) if self.canonical else LEGACY[i]

    def aid(self, a):
        return canonical_id(ALBUMS_OLD[a]) if self.canonical else ALBUMS_OLD[a]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, p = request.url.path, request.url.params
        self.calls.append(path.rsplit("/", 1)[-1])
        ok = {"status": "ok"}
        if path.endswith("ping.view"):
            return httpx.Response(200, json={"subsonic-response": {
                **ok, "type": "navidrome", "serverVersion": self.version}})
        if path.endswith("getAlbumList2.view"):
            albums = [] if p["offset"] != "0" else [
                {"id": self.aid(a), "name": f"Album {a}", "artist": "Band"} for a in range(3)]
            return httpx.Response(200, json={"subsonic-response": {**ok, "albumList2": {"album": albums}}})
        if path.endswith("getAlbum.view"):
            a = next(a for a in range(3) if self.aid(a) == p["id"])
            songs = [{"id": self.sid(i), "title": f"Song {i}", "artist": "Band",
                      "album": f"Album {a}", "duration": 200}
                     for i in range(len(LEGACY)) if album_of(i) == a]
            return httpx.Response(200, json={"subsonic-response": {**ok, "album": {"song": songs}}})
        if path.endswith("getSong.view"):
            if p["id"] in {self.sid(i) for i in range(len(LEGACY))}:
                return httpx.Response(200, json={"subsonic-response": {**ok, "song": {"id": p["id"]}}})
            return httpx.Response(200, json={"subsonic-response": {
                "status": "failed", "error": {"code": 70, "message": "Song not found"}}})
        raise AssertionError(f"unexpected call {request.url}")

    def client(self):
        return subsonic.Client(base_url="http://nd", transport=httpx.MockTransport(self.handler))


@pytest.fixture
def env():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "quiz.db")
    clips = os.path.join(d, "clips")
    os.makedirs(clips)
    conn = db.connect(path)
    yield conn, path, clips
    conn.close()


def seed_prepared(conn, clips):
    """What a quiz looks like after months on Navidrome 0.63: tiered, clipped, one ban."""
    for i, sid in enumerate(LEGACY):
        conn.execute(
            "INSERT INTO tracks(id,title,artist,album_id,tier,clipped_at,clip_start_s,intro_offset,"
            "global_listeners,play_count,banned,ban_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, f"Song {i}", "Band", ALBUMS_OLD[album_of(i)], "easy", "2026-07-01", 0, 1.5,
             1000 + i, i, 1 if i == 7 else 0, "flag" if i == 7 else None))
        os.makedirs(os.path.join(clips, sid))
        open(os.path.join(clips, sid, "5.mp3"), "w").write(sid)
    conn.commit()


PREPARED = "id, album_id, tier, clipped_at, clip_start_s, intro_offset, global_listeners, banned, active"


def prepared(conn):
    return [tuple(r) for r in conn.execute(f"SELECT {PREPARED} FROM tracks ORDER BY id")]


def snapshot(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM tracks ORDER BY title")]


def test_upgrade_keeps_every_prepared_track(env):
    """The #87 scenario: Navidrome upgraded to 0.64, quiz syncs as usual."""
    conn, path, clips = env
    seed_prepared(conn, clips)
    nd = FakeNavidrome("0.64.2 (10114574)", canonical=True)
    r = sync.sync_library(conn, nd.client(), clips_dir=clips, db_path=path)
    assert r["deactivated"] == 0
    assert r["tracks_active"] == len(LEGACY)
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == len(LEGACY)
    for i, old in enumerate(LEGACY):
        new = canonical_id(old)
        row = conn.execute("SELECT * FROM tracks WHERE id=?", (new,)).fetchone()
        assert row["tier"] == "easy" and row["clipped_at"] == "2026-07-01"
        assert row["global_listeners"] == 1000 + i and row["intro_offset"] == 1.5
        assert row["album_id"] == canonical_id(ALBUMS_OLD[album_of(i)])
        assert open(os.path.join(clips, new, "5.mp3")).read() == old
        assert not os.path.exists(os.path.join(clips, old))
    assert conn.execute("SELECT banned FROM tracks WHERE id=?",
                        (canonical_id(LEGACY[7]),)).fetchone()[0] == 1
    assert db.get_setting(conn, id_migration.SETTING_KEY) == "canonical"
    assert os.path.exists(r["id_migration"]["backup"])
    assert os.path.exists(r["id_migration"]["map_file"])


def test_second_sync_does_not_migrate_again(env):
    conn, path, clips = env
    seed_prepared(conn, clips)
    nd = FakeNavidrome("0.64.2", canonical=True)
    sync.sync_library(conn, nd.client(), clips_dir=clips, db_path=path)
    nd.calls.clear()
    r = sync.sync_library(conn, nd.client(), clips_dir=clips, db_path=path)
    assert "id_migration" not in r and r["deactivated"] == 0
    assert "ping.view" not in nd.calls  # setting short-circuits before asking the server


def test_old_navidrome_changes_nothing(env):
    conn, path, clips = env
    seed_prepared(conn, clips)
    before = prepared(conn)
    r = sync.sync_library(conn, FakeNavidrome("0.63.2 (be10f89c)", canonical=False).client(),
                          clips_dir=clips, db_path=path)
    assert "id_migration" not in r and r["deactivated"] == 0
    assert prepared(conn) == before   # metadata refresh is normal; ids + state untouched
    assert all(os.path.isdir(os.path.join(clips, s)) for s in LEGACY)
    assert db.get_setting(conn, id_migration.SETTING_KEY) is None


def test_repairs_library_an_old_quiz_already_synced_against_064(env):
    """Upgraded Navidrome first, ran an old quiz version once: every track is now
    an inactive legacy row plus a fresh, unprepared canonical duplicate."""
    conn, path, clips = env
    seed_prepared(conn, clips)
    for i, old in enumerate(LEGACY):
        conn.execute("UPDATE tracks SET active=0 WHERE id=?", (old,))
        conn.execute("INSERT INTO tracks(id,title,artist,album_id,play_count) VALUES(?,?,?,?,?)",
                     (canonical_id(old), f"Song {i}", "Band",
                      canonical_id(ALBUMS_OLD[album_of(i)]), 100 + i))
    conn.commit()
    r = sync.sync_library(conn, FakeNavidrome("0.64.2", canonical=True).client(),
                          clips_dir=clips, db_path=path)
    assert r["id_migration"]["merges"] == len(LEGACY)
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == len(LEGACY)
    for i, old in enumerate(LEGACY):
        row = conn.execute("SELECT * FROM tracks WHERE id=?", (canonical_id(old),)).fetchone()
        assert row["active"] == 1 and row["tier"] == "easy" and row["clipped_at"] == "2026-07-01"
        assert row["play_count"] == 100 + i          # the larger, current count wins
        assert os.path.isdir(os.path.join(clips, canonical_id(old)))
    assert conn.execute("SELECT banned FROM tracks WHERE id=?",
                        (canonical_id(LEGACY[7]),)).fetchone()[0] == 1


def test_merge_keeps_clips_the_new_row_already_has(env):
    conn, path, clips = env
    seed_prepared(conn, clips)
    old = LEGACY[0]
    new = canonical_id(old)
    conn.execute("INSERT INTO tracks(id,title,artist,clipped_at,clip_start_s) VALUES(?,?,?,?,?)",
                 (new, "Song 0", "Band", "2026-11-01", 2.0))
    conn.commit()
    os.makedirs(os.path.join(clips, new))
    open(os.path.join(clips, new, "5.mp3"), "w").write("recut")
    plan = id_migration.build_plan(conn, clips)
    assert next(c for c in plan.changes if c.old == old).clip == "kept-target"
    id_migration.apply_plan(conn, plan, clips, path)
    row = conn.execute("SELECT clipped_at, clip_start_s FROM tracks WHERE id=?", (new,)).fetchone()
    assert row["clipped_at"] == "2026-11-01" and row["clip_start_s"] == 2.0
    assert open(os.path.join(clips, new, "5.mp3")).read() == "recut"
    assert os.path.isdir(os.path.join(clips, old))   # left in place, never deleted


def test_refuses_when_converted_ids_are_unknown_to_server(env):
    """Server says 0.64 but doesn't know the converted ids: change nothing."""
    conn, path, clips = env
    seed_prepared(conn, clips)
    before = snapshot(conn)
    nd = FakeNavidrome("0.64.2", canonical=False)   # claims 0.64, still serves old ids
    with pytest.raises(id_migration.MigrationError):
        sync.sync_library(conn, nd.client(), clips_dir=clips, db_path=path)
    assert snapshot(conn) == before
    assert all(os.path.isdir(os.path.join(clips, s)) for s in LEGACY)


def test_clip_conflict_aborts_before_any_change(env):
    conn, path, clips = env
    seed_prepared(conn, clips)
    os.makedirs(os.path.join(clips, canonical_id(LEGACY[3])))   # stray folder in the way
    before = snapshot(conn)
    plan = id_migration.build_plan(conn, clips)
    assert plan.clip_conflicts
    with pytest.raises(id_migration.MigrationError):
        id_migration.apply_plan(conn, plan, clips, path)
    assert snapshot(conn) == before
    assert all(os.path.isdir(os.path.join(clips, s)) for s in LEGACY)


def test_failed_rename_rolls_everything_back(env, monkeypatch):
    conn, path, clips = env
    seed_prepared(conn, clips)
    before = snapshot(conn)
    real = os.rename
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 10:
            raise OSError("disk went away")
        return real(src, dst)
    monkeypatch.setattr(id_migration.os, "rename", flaky)
    with pytest.raises(OSError):
        id_migration.apply_plan(conn, id_migration.build_plan(conn, clips), clips, path)
    monkeypatch.setattr(id_migration.os, "rename", real)
    assert snapshot(conn) == before
    assert sorted(os.listdir(clips)) == sorted(LEGACY)


def test_dry_run_changes_nothing(env, capsys):
    conn, path, clips = env
    seed_prepared(conn, clips)
    conn.close()
    assert id_migration.main(["--dry-run", "--db", path, "--clips", clips]) == 0
    out = capsys.readouterr().out
    assert f'"track_ids": {len(LEGACY)}' in out and f'"clip_renames": {len(LEGACY)}' in out
    conn2 = db.connect(path)
    assert conn2.execute("SELECT COUNT(*) FROM tracks WHERE id IN (%s)" % ",".join("?" * len(LEGACY)),
                         LEGACY).fetchone()[0] == len(LEGACY)
    conn2.close()
    assert sorted(os.listdir(clips)) == sorted(LEGACY)


def test_reverse_restores_ids_and_clip_folders(env):
    conn, path, clips = env
    seed_prepared(conn, clips)
    out = id_migration.apply_plan(conn, id_migration.build_plan(conn, clips), clips, path)
    r = id_migration.reverse(conn, out["map_file"], clips)
    assert r == {"track_ids_restored": len(LEGACY), "clip_dirs_restored": len(LEGACY)}
    assert sorted(os.listdir(clips)) == sorted(LEGACY)
    assert {row[0] for row in conn.execute("SELECT id FROM tracks")} == set(LEGACY)
    assert db.get_setting(conn, id_migration.SETTING_KEY) is None


def test_sync_guard_refuses_mass_deactivation(env):
    """Any server id change the quiz can't explain must not orphan the library."""
    conn, path, clips = env
    seed_prepared(conn, clips)
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?)",
                 (id_migration.SETTING_KEY, "canonical"))   # migration already 'done'
    conn.commit()
    before = snapshot(conn)
    with pytest.raises(sync.SyncGuardError):
        sync.sync_library(conn, FakeNavidrome("0.64.2", canonical=True).client(),
                          clips_dir=clips, db_path=path)
    assert snapshot(conn) == before


def test_sync_guard_can_be_overridden(env, monkeypatch):
    conn, path, clips = env
    seed_prepared(conn, clips)
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?)", (id_migration.SETTING_KEY, "canonical"))
    conn.commit()
    monkeypatch.setattr(sync.config, "SYNC_MAX_DEACTIVATE_FRACTION", 1.0)
    r = sync.sync_library(conn, FakeNavidrome("0.64.2", canonical=True).client(),
                          clips_dir=clips, db_path=path)
    assert r["deactivated"] == len(LEGACY)
