"""Intro clip cutter.

Downloads originals from Navidrome and cuts loudness-normalised MP3 clips:
5/10/20-second intros (from intro_offset) plus a "payoff" clip from ~40% in,
played on the answer reveal. Clips land in CLIPS_DIR/<track_id>/.

Audio-only ffmpeg work — negligible CPU; the bottleneck is the download.
"""
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

from . import config, subsonic

CLIP_LENGTHS = (5, 10, 20)
PAYOFF_LEN = 12
BITRATE = "192k"
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
TIER_ORDER = "CASE tier WHEN 'easy' THEN 0 WHEN 'medium' THEN 1 WHEN 'hard' THEN 2 ELSE 3 END"

LOGGER = logging.getLogger(__name__)


class ClipError(RuntimeError):
    pass


def _ffmpeg_cut(src: str, dest: str, start: float, length: int, hide_tags: bool = False) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(start), "-t", str(length),
           "-i", src, "-af", LOUDNORM, "-ar", "44100", "-codec:a", "libmp3lame",
           "-b:a", BITRATE]
    if hide_tags:
        # players/displays show ID3 tags while a clip plays — never leak the answer
        cmd += ["-map_metadata", "-1", "-metadata", "title=Intro Quiz",
                "-metadata", "artist=Guess the song!"]
    cmd += [dest]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise ClipError(f"ffmpeg failed for {dest}: {r.stderr.strip()[:300]}")


def cut_track(client: subsonic.Client, row, clips_dir: str | None = None) -> None:
    clips_dir = clips_dir or config.CLIPS_DIR
    dest = os.path.join(clips_dir, row["id"])
    os.makedirs(dest, exist_ok=True)
    offset = float(row["intro_offset"] or 0)
    duration = int(row["duration"] or 0)
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src")
        client.download(row["id"], src)
        for length in CLIP_LENGTHS:
            _ffmpeg_cut(src, os.path.join(dest, f"{length}.mp3"), offset, length, hide_tags=True)
        # payoff: ~40% in (usually a verse/chorus), clamped inside the song
        payoff_start = max(offset + max(CLIP_LENGTHS), duration * 0.4)
        if duration and payoff_start > duration - PAYOFF_LEN:
            payoff_start = max(offset, duration - PAYOFF_LEN)
        _ffmpeg_cut(src, os.path.join(dest, "payoff.mp3"), payoff_start, PAYOFF_LEN)


def cut_batch(conn, client: subsonic.Client, limit: int = 50,
              clips_dir: str | None = None) -> dict:
    """Cut clips for tiered tracks that don't have them yet, easiest tiers first."""
    rows = conn.execute(
        f"SELECT * FROM tracks WHERE active=1 AND banned=0 AND tier IS NOT NULL AND clipped_at IS NULL "
        f"ORDER BY {TIER_ORDER}, global_listeners DESC LIMIT ?", (limit,)).fetchall()
    done = errors = 0
    for row in rows:
        try:
            cut_track(client, row, clips_dir)
        except ClipError as e:
            # deterministic decode failure (corrupt/odd source) — retrying is
            # futile and the track can never be played: ban it from the pool
            errors += 1
            LOGGER.warning("clip cut failed permanently, banning %s - %s: %s",
                           row["artist"], row["title"], e)
            conn.execute("UPDATE tracks SET banned=1 WHERE id=?", (row["id"],))
            conn.commit()
            shutil.rmtree(os.path.join(clips_dir or config.CLIPS_DIR, row["id"]), ignore_errors=True)
            continue
        except Exception as e:  # noqa: BLE001 - transient (network etc.): retry next batch
            errors += 1
            LOGGER.warning("clip cut failed (will retry) for %s - %s: %s", row["artist"], row["title"], e)
            shutil.rmtree(os.path.join(clips_dir or config.CLIPS_DIR, row["id"]), ignore_errors=True)
            continue
        conn.execute("UPDATE tracks SET clipped_at=? WHERE id=?",
                     (datetime.now(timezone.utc).isoformat(), row["id"]))
        conn.commit()
        done += 1
    remaining = conn.execute(
        "SELECT COUNT(*) c FROM tracks WHERE active=1 AND banned=0 AND tier IS NOT NULL AND clipped_at IS NULL"
    ).fetchone()["c"]
    return {"cut": done, "errors": errors, "remaining": remaining}
