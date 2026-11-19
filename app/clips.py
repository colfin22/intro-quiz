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
import time
from datetime import datetime, timedelta, timezone

from . import config, db, game, subsonic

CLIP_LENGTHS = (5, 10, 20)
PAYOFF_LEN = 12
BITRATE = "192k"
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
# Cut the most recognisable songs first: global popularity beats tier —
# 'popular songs from popular artists you have NOT listened to' are exactly
# the medium tier, and they deserve clips as early as the family favourites.
# Obscure tiebreak tracks queue last.
TIER_ORDER = "CASE WHEN tier='tiebreak' THEN 1 ELSE 0 END"
# every track the cutter is ever allowed to touch
CUTTABLE = (f"active=1 AND banned=0 AND tier IS NOT NULL "
            f"AND (duration IS NULL OR duration <= {game.MAX_DURATION_S})")
# ...and, among those, the ones whose clips are out of date: cut at a different
# CLIP_START_S (takes the current setting as its one bound parameter), or cut from a
# file that has since been replaced in place (#90)
STALE = "clipped_at IS NOT NULL AND (COALESCE(clip_start_s, 0) != ? OR source_changed=1)"

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


def _cut_all(src: str, dest: str, offset: float, duration: int) -> None:
    for length in CLIP_LENGTHS:
        _ffmpeg_cut(src, os.path.join(dest, f"{length}.mp3"), offset, length, hide_tags=True)
    # payoff: ~40% in (usually a verse/chorus), clamped inside the song
    payoff_start = max(offset + max(CLIP_LENGTHS), duration * 0.4)
    if duration and payoff_start > duration - PAYOFF_LEN:
        payoff_start = max(offset, duration - PAYOFF_LEN)
    _ffmpeg_cut(src, os.path.join(dest, "payoff.mp3"), payoff_start, PAYOFF_LEN)


SILENCE_NOISE = "-35dB"   # quieter than this counts as "nothing to hear"
SILENCE_MIN_S = 2.0       # ...if it lasts at least this long
MAX_AUTO_OFFSET = 60.0    # never skip more than a minute of ambience


def detect_intro_offset(src: str) -> float:
    """Where does the audible song actually start?

    Metal/post-rock/ambient tracks often open with 30-60s of silence, rain or
    feedback — a 5s clip of that is unguessable for the wrong reason. If the
    track opens with a long quiet stretch, the intro clips start where it ends.
    """
    # ffmpeg echoes the file's tags into stderr — non-UTF-8 metadata (latin-1
    # "Scheiße") must not crash us; the timestamps we parse are ASCII anyway (#23)
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-t", "90", "-i", src, "-af",
         f"silencedetect=noise={SILENCE_NOISE}:d={SILENCE_MIN_S}", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace")
    start = end = None
    for line in r.stderr.splitlines():
        if "silence_start:" in line and start is None:
            try:
                start = float(line.rsplit("silence_start:", 1)[1].strip())
            except ValueError:
                pass
        elif "silence_end:" in line and start is not None:
            try:
                end = float(line.rsplit("silence_end:", 1)[1].split("|")[0].strip())
            except ValueError:
                pass
            break
    if start is None or start > 1.0:  # the song opens audibly — leave it alone
        return 0.0
    if end is None:  # quiet right through the probe window
        return MAX_AUTO_OFFSET
    return min(max(end, 0.0), MAX_AUTO_OFFSET)


def probe_duration(src: str) -> int:
    """Length of a downloaded file in seconds, 0 if ffprobe can't tell us."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", src],
        capture_output=True, text=True, errors="replace")
    try:
        return int(float(r.stdout.strip()))
    except ValueError:
        return 0


def cut_track(client: subsonic.Client, row, clips_dir: str | None = None) -> float:
    """Cut all clips for one track. Returns the song's own audible start.

    Two different offsets are in play and must not be conflated:

    * `detected` — where the song itself starts being audible (past any silent
      ambient intro). It is a property of the track, so it is what we persist
      in `intro_offset` and re-use as a floor on the next cut.
    * `offset` — where we actually cut, i.e. `detected` raised to CLIP_START_S
      for libraries that want clips to skip a long instrumental (#56).

    Persisting the latter as `intro_offset` would make CLIP_START_S a one-way
    door: every track would be permanently pinned at the raised offset and
    lowering the setting again could never take effect.
    """
    clips_dir = clips_dir or config.CLIPS_DIR
    dest = os.path.join(clips_dir, row["id"])
    os.makedirs(dest, exist_ok=True)
    detected = float(row["intro_offset"] or 0)
    duration = int(row["duration"] or 0)
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src")
        try:
            client.download(row["id"], src)
        except subsonic.SubsonicError as e:
            # missing/renamed file — the next library scan reconciles it; skip quietly
            raise ClipError(f"source unavailable on server: {e}") from e
        detected = max(detected, detect_intro_offset(src))
        offset = max(detected, config.CLIP_START_S)
        if not duration and offset:
            # the clamp below is what keeps a short song from being cut past its
            # own end; with no duration on the row (never synced/odd metadata) we
            # have the file in hand, so ask it rather than cutting blind
            duration = probe_duration(src)
        if duration and offset > duration - 25:  # keep room for the 20s clip
            offset = max(0.0, duration - 25)
        try:
            _cut_all(src, dest, offset, duration)
        except ClipError:
            # original undecodable (old WMA etc.) — let Navidrome transcode it
            LOGGER.info("retrying %s - %s via server transcode", row["artist"], row["title"])
            client.download_transcoded(row["id"], src)
            _cut_all(src, dest, offset, duration)
    return detected


def cut_batch(conn, client: subsonic.Client, limit: int = 50,
              clips_dir: str | None = None) -> dict:
    """Cut clips for tiered tracks that don't have them yet, easiest tiers first.

    Tracks whose clips were cut with a different CLIP_START_S are re-cut too,
    but only once nothing is waiting for its first clips — coverage before
    conversion. They keep their old clips (and stay playable) until the moment
    ffmpeg overwrites them, so changing the setting never empties the pool.
    """
    rows = conn.execute(
        f"SELECT * FROM tracks WHERE {CUTTABLE} AND clipped_at IS NULL "
        f"ORDER BY {TIER_ORDER}, global_listeners DESC LIMIT ?", (limit,)).fetchall()
    if len(rows) < limit:
        rows += conn.execute(
            f"SELECT * FROM tracks WHERE {CUTTABLE} AND {STALE} "
            f"ORDER BY {TIER_ORDER}, global_listeners DESC LIMIT ?",
            (config.CLIP_START_S, limit - len(rows))).fetchall()
    done = errors = 0
    for row in rows:
        already_clipped = row["clipped_at"] is not None
        try:
            used_offset = cut_track(client, row, clips_dir)
        except ClipError as e:
            # deterministic decode failure (corrupt/odd source) — retrying is
            # futile and the track can never be played: ban it from the pool
            errors += 1
            LOGGER.warning("clip cut failed permanently, banning %s - %s: %s",
                           row["artist"], row["title"], e)
            conn.execute("UPDATE tracks SET banned=1, ban_reason='decode' WHERE id=?", (row["id"],))
            conn.commit()
            shutil.rmtree(os.path.join(clips_dir or config.CLIPS_DIR, row["id"]), ignore_errors=True)
            continue
        except Exception as e:  # noqa: BLE001 - transient (network etc.): retry next batch
            errors += 1
            LOGGER.warning("clip cut failed (will retry) for %s - %s: %s", row["artist"], row["title"], e)
            if not already_clipped:
                # a half-written first cut is junk; a failed RE-cut leaves the
                # previous clips in place and the track stays playable
                shutil.rmtree(os.path.join(clips_dir or config.CLIPS_DIR, row["id"]), ignore_errors=True)
            continue
        conn.execute("UPDATE tracks SET clipped_at=?, intro_offset=?, clip_start_s=?, source_changed=0 "
                     "WHERE id=?",
                     (datetime.now(timezone.utc).isoformat(), used_offset,
                      config.CLIP_START_S, row["id"]))
        conn.commit()
        done += 1
    remaining = conn.execute(
        f"SELECT COUNT(*) c FROM tracks WHERE {CUTTABLE} AND (clipped_at IS NULL OR {STALE})",
        (config.CLIP_START_S,)).fetchone()["c"]
    return {"cut": done, "errors": errors, "remaining": remaining}


def sweep(batch: int = 100, stall_sleep_s: float = 600, max_stalls: int = 6,
          max_hours: float = 0, clock=time.monotonic) -> dict:
    """Run-once bulk cutter: batch until every tiered track has clips, then stop.

    This is the CLIP_SWEEP_ON_START bootstrap for fresh installs — one long
    session (hours for a big library; the download is the bottleneck) instead
    of drip-feeding /api/clips/cut. Safe to leave enabled: a start with
    nothing to cut returns immediately. If Navidrome is unreachable or every
    track in a batch errors, it backs off and eventually gives up until the
    next start rather than hammering forever. max_hours > 0 caps the session
    (CLIP_SWEEP_MAX_HOURS): it finishes the batch in hand, stops cleanly, and
    resumes from where it left off on the next start.
    """
    conn = db.connect()
    try:
        tiered = conn.execute(
            "SELECT COUNT(*) c FROM tracks WHERE active=1 AND tier IS NOT NULL").fetchone()["c"]
    finally:
        conn.close()
    if tiered == 0:
        # a fresh install hasn't synced/scored yet — "0 cut, complete" would mislead
        LOGGER.warning("clip sweep: no tiered tracks exist yet, nothing to cut. Run "
                       "POST /api/sync, then /api/score/lastfm (repeat until done), then "
                       "/api/score/tiers — the sweep resumes on the next container start. "
                       "See the README's 'Bootstrapping the clips' section.")
        return {"cut": 0, "stopped": "nothing-tiered"}
    deadline = clock() + max_hours * 3600 if max_hours > 0 else None
    total = stalls = 0
    while True:
        if deadline and clock() >= deadline:
            LOGGER.info("clip sweep: %.1fh time limit reached — %d cut; resumes next start", max_hours, total)
            return {"cut": total, "stopped": "time-limit"}
        conn = db.connect()
        try:
            r = cut_batch(conn, subsonic.Client(), limit=batch)
        except Exception as e:  # noqa: BLE001 — server down/unconfigured: back off
            LOGGER.warning("clip sweep: batch failed (%s) — retrying in %ds", e, int(stall_sleep_s))
            r = None
        finally:
            conn.close()
        if r is None or (r["cut"] == 0 and r["errors"] > 0):
            stalls += 1
            if stalls >= max_stalls:
                LOGGER.error("clip sweep: no progress after %d attempts — giving up until next start", max_stalls)
                return {"cut": total, "stopped": "stalled"}
            time.sleep(stall_sleep_s)
            continue
        stalls = 0
        total += r["cut"]
        if r["cut"] or r["errors"]:
            LOGGER.info("clip sweep: +%d clips (%d errors), %d remaining", r["cut"], r["errors"], r["remaining"])
        if r["remaining"] == 0:
            LOGGER.info("clip sweep complete — %d cut this session", total)
            return {"cut": total, "stopped": "done"}


def _dir_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def prune(conn, clips_dir: str | None = None, after_days: float | None = None,
          dry_run: bool = False, now: datetime | None = None) -> dict:
    """Delete clip folders nothing can play any more (#90).

    Two kinds:
    * clips of a track that has been gone from the library for `after_days`
      (CLIP_PRUNE_AFTER_DAYS) — a removed file, or the old copy of one replaced
      under a new path, which the sync re-adds as a brand-new track;
    * folders whose id isn't in the database at all, untouched for as long.

    The wait is the safety margin: a track that drops out and comes back (a scan
    race, the share briefly offline) still has its clips. It also refuses to run
    while the last sync was refused by the mass-deactivation guard, before any
    sync has completed, or when it would delete more than
    SYNC_MAX_DEACTIVATE_FRACTION of all clip folders in one go.
    """
    after_days = config.CLIP_PRUNE_AFTER_DAYS if after_days is None else after_days
    if after_days <= 0:
        return {"skipped": "disabled (CLIP_PRUNE_AFTER_DAYS=0)"}
    ok_at, refused_at = db.get_setting(conn, "sync_ok_at"), db.get_setting(conn, "sync_refused_at")
    if not ok_at:
        return {"skipped": "no library sync has completed yet: run a sync first"}
    if refused_at and refused_at > ok_at:
        return {"skipped": "the last library sync was refused by its safety check: "
                           "nothing deleted until a sync completes normally"}
    clips_dir = clips_dir or config.CLIPS_DIR
    if not os.path.isdir(clips_dir):
        return {"skipped": f"clips folder not found: {clips_dir}"}
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=after_days)).isoformat()
    rows = {r["id"]: r for r in conn.execute("SELECT id, active, inactive_since FROM tracks")}
    if not rows:
        return {"skipped": "no tracks in the database"}

    folders = [e for e in os.scandir(clips_dir)
               if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")]
    gone, orphans, waiting = [], [], 0
    for e in folders:
        row = rows.get(e.name)
        if row is None:
            if e.stat(follow_symlinks=False).st_mtime <= (now - timedelta(days=after_days)).timestamp():
                orphans.append(e.path)
            else:
                waiting += 1
        elif not row["active"]:
            if row["inactive_since"] and row["inactive_since"] <= cutoff:
                gone.append(e.path)
            else:
                waiting += 1
    doomed = gone + orphans
    limit = config.SYNC_MAX_DEACTIVATE_FRACTION
    if (limit < 1 and len(folders) >= config.SYNC_GUARD_MIN_TRACKS
            and len(doomed) > len(folders) * limit):
        return {"skipped": f"would delete {len(doomed)} of {len(folders)} clip folders: refused. "
                           "If the library really shrank, set SYNC_MAX_DEACTIVATE_FRACTION=1 "
                           "for one run"}
    freed = sum(_dir_bytes(p) for p in doomed)
    out = {"removed_tracks": len(gone), "orphan_folders": len(orphans),
           "freed_mb": round(freed / 1e6, 1), "still_waiting": waiting}
    if dry_run:
        out["dry_run"] = True
        return out
    for path in doomed:
        shutil.rmtree(path, ignore_errors=True)
    if gone:
        ids = [os.path.basename(p) for p in gone]
        conn.executemany("UPDATE tracks SET clipped_at=NULL WHERE id=?", [(i,) for i in ids])
        conn.commit()
    LOGGER.info("clip prune: %d removed tracks, %d orphan folders, %.1f MB freed",
                len(gone), len(orphans), freed / 1e6)
    return out
