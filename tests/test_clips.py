import os
import shutil
import subprocess
import tempfile

import pytest

from app import clips, db

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")


class FakeClient:
    """Stands in for the Subsonic client: 'downloads' a locally generated tone."""
    def __init__(self, src):
        self.src = src

    def download(self, song_id, dest_path):
        if song_id == "broken":
            with open(dest_path, "wb") as f:
                f.write(b"not audio at all")
            return
        shutil.copy(self.src, dest_path)

    def download_transcoded(self, song_id, dest_path):
        # the transcode fallback can't rescue this one either
        self.download(song_id, dest_path)


def probe_duration(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True)
    return float(out.stdout.strip())


@pytest.fixture()
def tone(tmp_path):
    src = tmp_path / "tone.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=40", "-codec:a", "libmp3lame",
                    str(src)], check=True)
    return str(src)


def make_db(tracks):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = db.connect(path)
    for t in tracks:
        conn.execute(
            "INSERT INTO tracks(id,title,artist,duration,tier,intro_offset,global_listeners,active) "
            "VALUES(?,?,?,?,?,?,?,1)",
            (t["id"], t.get("title", "t"), t.get("artist", "a"), t.get("duration", 40),
             t.get("tier", "easy"), t.get("intro_offset", 0), t.get("global_listeners", 100)))
    conn.commit()
    return conn, path


def test_cut_batch_produces_clips_and_marks(tone, tmp_path):
    conn, p = make_db([{"id": "ok1"}, {"id": "broken"}, {"id": "untiered", "tier": None}])
    conn.execute("UPDATE tracks SET tier=NULL WHERE id='untiered'")
    conn.commit()
    clips_dir = str(tmp_path / "clips")
    try:
        r = clips.cut_batch(conn, FakeClient(tone), clips_dir=clips_dir)
        assert r["cut"] == 1 and r["errors"] == 1
        assert r["remaining"] == 0  # undecodable source = banned, not endlessly retried
        assert conn.execute("SELECT banned FROM tracks WHERE id='broken'").fetchone()["banned"] == 1
        for length in (5, 10, 20):
            f = os.path.join(clips_dir, "ok1", f"{length}.mp3")
            assert abs(probe_duration(f) - length) < 0.6, f
        assert os.path.exists(os.path.join(clips_dir, "ok1", "payoff.mp3"))
        # failed track: no leftover dir, not marked as clipped
        assert not os.path.exists(os.path.join(clips_dir, "broken"))
        marks = {x["id"]: x["clipped_at"] for x in conn.execute("SELECT id, clipped_at FROM tracks")}
        assert marks["ok1"] and not marks["broken"] and not marks["untiered"]
        # second run: nothing left at all (banned track excluded)
        r2 = clips.cut_batch(conn, FakeClient(tone), clips_dir=clips_dir)
        assert r2["cut"] == 0 and r2["errors"] == 0
    finally:
        os.unlink(p)


def test_intro_offset_respected(tone, tmp_path):
    conn, p = make_db([{"id": "off", "intro_offset": 8, "duration": 40}])
    clips_dir = str(tmp_path / "clips")
    try:
        clips.cut_batch(conn, FakeClient(tone), clips_dir=clips_dir)
        # 20s clip starting at 8s of a 40s file → full 20s available
        assert abs(probe_duration(os.path.join(clips_dir, "off", "20.mp3")) - 20) < 0.6
    finally:
        os.unlink(p)


def test_sweep_runs_to_done_and_survives_stalls(monkeypatch):
    """The CLIP_SWEEP_ON_START bootstrap batches to zero and backs off on stalls."""
    results = [
        Exception("navidrome down"),          # transient failure -> back off
        {"cut": 0, "errors": 5, "remaining": 10},   # all-error batch -> back off
        {"cut": 8, "errors": 2, "remaining": 2},
        {"cut": 2, "errors": 0, "remaining": 0},    # done
    ]

    def fake_batch(conn, client, limit=100):
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    class DummyConn:
        def close(self):
            pass

    monkeypatch.setattr(clips, "cut_batch", fake_batch)
    monkeypatch.setattr(clips.db, "connect", lambda *a, **k: DummyConn())
    monkeypatch.setattr(clips.subsonic, "Client", lambda: object())
    out = clips.sweep(stall_sleep_s=0)
    assert out == {"cut": 10, "stopped": "done"}
    assert not results  # consumed everything


def test_sweep_gives_up_after_max_stalls(monkeypatch):
    def always_fails(conn, client, limit=100):
        raise Exception("still down")

    class DummyConn:
        def close(self):
            pass

    monkeypatch.setattr(clips, "cut_batch", always_fails)
    monkeypatch.setattr(clips.db, "connect", lambda *a, **k: DummyConn())
    monkeypatch.setattr(clips.subsonic, "Client", lambda: object())
    out = clips.sweep(stall_sleep_s=0, max_stalls=3)
    assert out == {"cut": 0, "stopped": "stalled"}
