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


class DummyConn:
    """Stands in for db.connect() in sweep tests — answers the tiered-count query."""
    tiered = 100

    def execute(self, *a, **k):
        class R:
            def fetchone(_self):
                return {"c": DummyConn.tiered}
        return R()

    def close(self):
        pass


def test_sweep_explains_fresh_install(monkeypatch):
    """First boot with nothing tiered: say WHY nothing was cut, don't claim 'complete'."""
    monkeypatch.setattr(clips.db, "connect", lambda *a, **k: DummyConn())
    monkeypatch.setattr(clips.subsonic, "Client", lambda: object())
    monkeypatch.setattr(DummyConn, "tiered", 0)
    try:
        out = clips.sweep(stall_sleep_s=0)
    finally:
        monkeypatch.setattr(DummyConn, "tiered", 100)
    assert out == {"cut": 0, "stopped": "nothing-tiered"}


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

    monkeypatch.setattr(clips, "cut_batch", fake_batch)
    monkeypatch.setattr(clips.db, "connect", lambda *a, **k: DummyConn())
    monkeypatch.setattr(clips.subsonic, "Client", lambda: object())
    out = clips.sweep(stall_sleep_s=0)
    assert out == {"cut": 10, "stopped": "done"}
    assert not results  # consumed everything


def test_sweep_gives_up_after_max_stalls(monkeypatch):
    def always_fails(conn, client, limit=100):
        raise Exception("still down")

    monkeypatch.setattr(clips, "cut_batch", always_fails)
    monkeypatch.setattr(clips.db, "connect", lambda *a, **k: DummyConn())
    monkeypatch.setattr(clips.subsonic, "Client", lambda: object())
    out = clips.sweep(stall_sleep_s=0, max_stalls=3)
    assert out == {"cut": 0, "stopped": "stalled"}


def test_sweep_respects_time_limit(monkeypatch):
    """CLIP_SWEEP_MAX_HOURS: stops cleanly at the deadline, resumes next start."""
    ticker = iter(range(0, 100000, 1800))  # each clock() call advances 30 min

    def fake_batch(conn, client, limit=100):
        return {"cut": 100, "errors": 0, "remaining": 5000}  # never finishes

    monkeypatch.setattr(clips, "cut_batch", fake_batch)
    monkeypatch.setattr(clips.db, "connect", lambda *a, **k: DummyConn())
    monkeypatch.setattr(clips.subsonic, "Client", lambda: object())
    out = clips.sweep(stall_sleep_s=0, max_hours=1, clock=lambda: next(ticker))
    assert out["stopped"] == "time-limit"
    assert out["cut"] > 0  # got at least one batch in before the cap


@pytest.fixture()
def quiet_intro_tone(tmp_path):
    """8 seconds of near-silence, then tone — the metal/ambient intro problem."""
    src = tmp_path / "quiet.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=60",
                    "-af", "adelay=8000|8000",
                    "-codec:a", "libmp3lame", str(src)], check=True)
    return str(src)


def test_detect_intro_offset(tone, quiet_intro_tone):
    assert clips.detect_intro_offset(tone) == 0.0          # audible from the start
    off = clips.detect_intro_offset(quiet_intro_tone)
    assert 6.5 <= off <= 9.5, off                          # skips the quiet opening


def test_cut_batch_persists_detected_offset(quiet_intro_tone, tmp_path):
    conn, p = make_db([{"id": "q1", "duration": 68}])
    clips_dir = tmp_path / "clips"
    try:
        r = clips.cut_batch(conn, FakeClient(quiet_intro_tone), limit=5, clips_dir=str(clips_dir))
        assert r["cut"] == 1
        row = conn.execute("SELECT intro_offset, clipped_at FROM tracks WHERE id='q1'").fetchone()
        assert 6.5 <= row["intro_offset"] <= 9.5
        assert row["clipped_at"] is not None
        # the 5s clip must contain audio, not the quiet opening
        assert probe_duration(str(clips_dir / "q1" / "5.mp3")) >= 4.5
    finally:
        os.unlink(p)


# --- CLIP_START_S: start clips further into the song (#56) -------------------


@pytest.fixture()
def long_tone(tmp_path):
    """70 seconds of audible tone — room for a 30s start plus a 20s clip."""
    src = tmp_path / "long.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=70", "-codec:a", "libmp3lame",
                    str(src)], check=True)
    return str(src)


def capture_offsets(monkeypatch):
    """Record the offset every _cut_all is asked for, while still cutting."""
    seen = []
    real = clips._cut_all
    monkeypatch.setattr(clips, "_cut_all", lambda src, dest, offset, duration: (
        seen.append(offset), real(src, dest, offset, duration))[1])
    return seen


def test_clip_start_s_shifts_the_cut(long_tone, tmp_path, monkeypatch):
    monkeypatch.setattr(clips.config, "CLIP_START_S", 30.0)
    seen = capture_offsets(monkeypatch)
    conn, p = make_db([{"id": "late", "duration": 70}])
    clips_dir = tmp_path / "clips"
    try:
        assert clips.cut_batch(conn, FakeClient(long_tone), clips_dir=str(clips_dir))["cut"] == 1
        assert seen == [30.0]
        assert abs(probe_duration(str(clips_dir / "late" / "20.mp3")) - 20) < 0.6
        row = conn.execute("SELECT intro_offset, clip_start_s FROM tracks WHERE id='late'").fetchone()
        assert row["clip_start_s"] == 30.0
        # the SONG's own start is unchanged — only where we chose to cut moved
        assert row["intro_offset"] == 0
    finally:
        os.unlink(p)


def test_clip_start_s_clamped_on_a_short_song(tone, tmp_path, monkeypatch):
    """A 40s song can't start 30s in — clamp so the 20s clip still fits."""
    monkeypatch.setattr(clips.config, "CLIP_START_S", 30.0)
    seen = capture_offsets(monkeypatch)
    conn, p = make_db([{"id": "short", "duration": 40}])
    clips_dir = tmp_path / "clips"
    try:
        clips.cut_batch(conn, FakeClient(tone), clips_dir=str(clips_dir))
        assert seen == [15.0]
        assert abs(probe_duration(str(clips_dir / "short" / "20.mp3")) - 20) < 0.6
    finally:
        os.unlink(p)


def test_clip_start_s_clamped_when_duration_is_unknown(tone, tmp_path, monkeypatch):
    """No duration on the row: probe the file rather than cutting past its end."""
    monkeypatch.setattr(clips.config, "CLIP_START_S", 30.0)
    seen = capture_offsets(monkeypatch)
    conn, p = make_db([{"id": "nodur", "duration": None}])
    clips_dir = tmp_path / "clips"
    try:
        clips.cut_batch(conn, FakeClient(tone), clips_dir=str(clips_dir))
        assert seen == [15.0]  # probed 40s, clamped to 40-25
        for length in (5, 10, 20):
            f = str(clips_dir / "nodur" / f"{length}.mp3")
            assert abs(probe_duration(f) - length) < 0.6, f
    finally:
        os.unlink(p)


def test_clip_start_s_can_be_lowered_again(long_tone, tmp_path, monkeypatch):
    """Turning the setting back down must actually take effect.

    It only can because intro_offset holds the song's own start, not the
    raised one — otherwise every cut track would be pinned at 30s forever.
    """
    conn, p = make_db([{"id": "late", "duration": 70}])
    clips_dir = tmp_path / "clips"
    try:
        monkeypatch.setattr(clips.config, "CLIP_START_S", 30.0)
        clips.cut_batch(conn, FakeClient(long_tone), clips_dir=str(clips_dir))
        monkeypatch.setattr(clips.config, "CLIP_START_S", 0.0)
        seen = capture_offsets(monkeypatch)
        r = clips.cut_batch(conn, FakeClient(long_tone), clips_dir=str(clips_dir))
        assert r["cut"] == 1 and seen == [0.0]  # re-cut at the top of the song
        assert conn.execute(
            "SELECT clip_start_s FROM tracks WHERE id='late'").fetchone()["clip_start_s"] == 0.0
        assert r["remaining"] == 0
    finally:
        os.unlink(p)


def test_stale_clips_queue_behind_unclipped_ones(tmp_path, monkeypatch):
    """Coverage before conversion: a track with NO clips is always cut first."""
    conn, p = make_db([{"id": "stale"}, {"id": "fresh"}])
    conn.execute("UPDATE tracks SET clipped_at='2026-01-01', clip_start_s=0 WHERE id='stale'")
    conn.commit()
    monkeypatch.setattr(clips.config, "CLIP_START_S", 30.0)
    cut = []
    monkeypatch.setattr(clips, "cut_track", lambda client, row, d=None: cut.append(row["id"]) or 0.0)
    try:
        r = clips.cut_batch(conn, FakeClient("unused"), limit=1, clips_dir=str(tmp_path))
        assert cut == ["fresh"] and r["remaining"] == 1  # the stale one still waiting
        r = clips.cut_batch(conn, FakeClient("unused"), limit=1, clips_dir=str(tmp_path))
        assert cut == ["fresh", "stale"] and r["remaining"] == 0
    finally:
        os.unlink(p)


def test_failed_recut_keeps_the_existing_clips(tone, tmp_path, monkeypatch):
    """A transient failure re-cutting must not strip a playable track's clips."""
    conn, p = make_db([{"id": "t1", "duration": 40}])
    clips_dir = tmp_path / "clips"
    try:
        clips.cut_batch(conn, FakeClient(tone), clips_dir=str(clips_dir))
        monkeypatch.setattr(clips.config, "CLIP_START_S", 30.0)
        monkeypatch.setattr(clips, "cut_track", lambda *a, **k: (_ for _ in ()).throw(OSError("network")))
        r = clips.cut_batch(conn, FakeClient(tone), clips_dir=str(clips_dir))
        assert r["errors"] == 1
        assert os.path.exists(str(clips_dir / "t1" / "5.mp3"))
        assert conn.execute("SELECT clipped_at FROM tracks WHERE id='t1'").fetchone()["clipped_at"]
    finally:
        os.unlink(p)
