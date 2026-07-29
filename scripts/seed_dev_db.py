"""Seed a local dev database + fake clips, so the app runs on a laptop.

Nothing here touches Navidrome or the real library: tracks are invented and the
"audio" is an ffmpeg-generated tone, one distinguishable pitch per track. That's
deliberate — silence would make an audio-routing bug look identical to a working
one, and you can hear a wrong-clip bug immediately when every track has its own
note.

    python scripts/seed_dev_db.py            # 40 tracks
    python scripts/seed_dev_db.py --tracks 8 --force

Point the app at the result with QUIZ_DB / CLIPS_DIR (see the Makefile).
"""
import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import clips, db  # noqa: E402

# Recognisable enough to tell apart by ear, spread over a couple of octaves.
SCALE = [262, 294, 330, 349, 392, 440, 494]
ARTISTS = ["The Dev Tones", "Localhost", "Mock Turtle", "Stub & the Fakes",
           "Sine Wave Surfers", "The Placeholders", "Null Set", "Test Pattern"]


def make_tone(dest: str, freq: int, seconds: int) -> None:
    """A tone with a short silent lead-in, so intro_offset logic has something
    to bite on and clip boundaries are audible."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
         "-ar", "44100", "-codec:a", "libmp3lame", "-b:a", "128k", dest],
        check=True)


def seed(n_tracks: int, db_path: str, clips_dir: str, force: bool) -> None:
    conn = db.connect(db_path)
    existing = conn.execute("SELECT COUNT(*) c FROM tracks").fetchone()["c"]
    if existing and not force:
        print(f"{db_path} already has {existing} tracks — use --force to reseed")
        return
    if force:
        conn.execute("DELETE FROM tracks")
        conn.commit()

    os.makedirs(clips_dir, exist_ok=True)
    for i in range(n_tracks):
        tid = f"dev{i:03d}"
        freq = SCALE[i % len(SCALE)] * (1 + i // len(SCALE) % 3)
        artist = ARTISTS[i % len(ARTISTS)]
        duration = 90
        conn.execute(
            "INSERT INTO tracks(id,title,artist,album,album_artist,year,duration,"
            "tier,clipped_at,global_listeners,play_count,active,banned) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,1,0)",
            (tid, f"Tone {freq} Hz", artist, f"{artist} Vol. {i // 8 + 1}", artist,
             1970 + (i % 6) * 10, duration,
             # a spread across tiers so tier filters have something to filter
             ["easy", "medium", "medium", "hard"][i % 4],
             "2026-07-29T00:00:00",
             500_000 - i * 1000,           # global_listeners, descending
             3 if i % 4 == 0 else 0))      # some "family favourites"

        dest = os.path.join(clips_dir, tid)
        os.makedirs(dest, exist_ok=True)
        if os.path.exists(os.path.join(dest, "payoff.mp3")) and not force:
            continue
        src = os.path.join(dest, "_full.mp3")
        make_tone(src, freq, duration)
        # reuse the real cutter so dev clips match production exactly
        clips._cut_all(src, dest, offset=0, duration=duration)
        os.remove(src)
        print(f"  {tid}  {freq} Hz  {artist}", flush=True)

    conn.commit()
    counts = {r["tier"]: r["c"] for r in conn.execute(
        "SELECT tier, COUNT(*) c FROM tracks GROUP BY tier")}
    conn.close()
    print(f"\nseeded {n_tracks} tracks into {db_path}")
    print(f"clips in {clips_dir}: {counts}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracks", type=int, default=40)
    ap.add_argument("--db", default=os.environ.get("QUIZ_DB", "./devdata/quiz.db"))
    ap.add_argument("--clips", default=os.environ.get("CLIPS_DIR", "./devclips"))
    ap.add_argument("--force", action="store_true", help="wipe and reseed")
    a = ap.parse_args()
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found — brew install ffmpeg")
    seed(a.tracks, a.db, a.clips, a.force)
