"""Artist-tag repair as a background job: fix variant spellings in the files.

app/library.artist_key already folds 'AC/DC' / 'AC, DC' / 'AC-DC' / 'ACDC'
together so the quiz treats them as one band. This module fixes the text players
actually SEE, by writing the agreed spelling back into the files' own tags.

Why it lives in the app rather than only in scripts/retag_artists.py: the server
has the library on a local mount, where reading tags runs at hundreds of files a
second. The same scan from a laptop over SMB manages ~8/s — 46 minutes for a
22,000-file library. It also belongs with the other maintenance work: one job at
a time (jobs.py), progress and log output on /admin, and a resumable cache so a
container restart mid-run costs nothing.

SAFETY, in order of how much they'd cost to get wrong:

- **Never renames or moves a file.** Navidrome derives track ids from the path, so
  a rename re-ids every track and orphans the clips already cut for it. Mangled
  FOLDER names ('AC+DC/') are left alone; '/' is illegal in a path and Navidrome
  reads embedded tags, not directory names, so they cost nothing.
- **Refuses to run unless MUSIC_DIRS is set**, and treats the roots as read-only
  until `write=True`. The default job is a dry run.
- **Journals every write** so an interrupted run resumes instead of redoing work.
- Requires mutagen; without it the job fails with that message rather than
  half-doing anything.

After a write run, trigger a Navidrome rescan so the DB picks up the new tags,
then check GET /api/library/audit to confirm the variant count dropped.
"""
import json
import logging
import os
import time
from collections import defaultdict

from .library import artist_key

LOGGER = logging.getLogger(__name__)

# Library roots to scan, ':'-separated, as seen INSIDE the container. Unset
# disables the job entirely — this writes to the music library, so it must be an
# explicit opt-in rather than something a default path could stumble into.
MUSIC_DIRS = [d for d in os.environ.get("MUSIC_DIRS", "").split(":") if d.strip()]
STATE_DIR = os.environ.get("RETAG_STATE_DIR", "/data")

AUDIO_EXT = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wma", ".aac", ".aiff", ".alac"}
ARTIST_KEYS = ("artist", "TPE1", "\xa9ART")
ALBUM_ARTIST_KEYS = ("albumartist", "album_artist", "TPE2", "aART", "ALBUMARTIST")
PROGRESS_EVERY = 500          # files between set_stage/log updates
CACHE_FLUSH_EVERY = 500       # ...and between cache saves; see scripts/retag_artists.py


class RetagError(RuntimeError):
    pass


def _cache_path() -> str:
    return os.path.join(STATE_DIR, "retag-cache.json")


def _journal_path() -> str:
    return os.path.join(STATE_DIR, "retag-journal.txt")


def _load_cache() -> dict:
    """path -> [mtime, size, artist, album_artist]; invalidated by mtime/size."""
    p = _cache_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        LOGGER.warning("retag cache unreadable, starting fresh: %s", e)
        return {}


def _save_cache(cache: dict) -> None:
    p = _cache_path()
    try:
        tmp = p + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(cache, fh)
        os.replace(tmp, p)  # atomic: a kill mid-write can't corrupt it
    except OSError as e:
        LOGGER.warning("could not save retag cache: %s", e)


def _open_audio(path: str):
    import mutagen
    try:
        return mutagen.File(path, easy=True)
    except Exception as e:  # noqa: BLE001 — one corrupt file must not stop the run
        LOGGER.warning("retag: unreadable, skipped %s (%s)", path, e)
        return None


def _tag(audio, keys):
    for k in keys:
        v = audio.get(k)
        if v:
            return (v[0] if isinstance(v, list) else v) or None
    return None


def _recache(cache: dict, path: str, artist: str | None, album_artist: str | None) -> None:
    """Point the cache entry at the tag the file now has, re-stat'ing it first."""
    try:
        st = os.stat(path)
    except OSError:
        cache.pop(path, None)  # gone; drop it rather than cache a guess
        return
    cache[path] = [int(st.st_mtime), st.st_size, artist, album_artist]


def walk_audio(roots: list[str]) -> list[str]:
    out = []
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if os.path.splitext(name)[1].lower() in AUDIO_EXT:
                    out.append(os.path.join(dirpath, name))
    return out


def scan(paths: list[str], cache: dict, set_stage=None) -> list[dict]:
    """Read artist/album_artist tags, filling the cache as it goes."""
    found, cached, started = [], 0, time.monotonic()
    total = len(paths)
    for i, path in enumerate(paths, 1):
        try:
            st = os.stat(path)
        except OSError:
            continue
        hit = cache.get(path)
        if hit and hit[0] == int(st.st_mtime) and hit[1] == st.st_size:
            artist, album_artist = hit[2], hit[3]
            cached += 1
        else:
            audio = _open_audio(path)
            if audio is None:
                continue
            artist = _tag(audio, ARTIST_KEYS)
            album_artist = _tag(audio, ALBUM_ARTIST_KEYS)
            cache[path] = [int(st.st_mtime), st.st_size, artist, album_artist]
        if artist:
            found.append({"path": path, "artist": artist, "album_artist": album_artist})
        if i % PROGRESS_EVERY == 0 or i == total:
            rate = i / max(1e-6, time.monotonic() - started)
            msg = (f"scanning tags — {i}/{total} ({100 * i // total}%), "
                   f"{rate:.0f}/s, {cached} cached")
            LOGGER.info("retag: %s", msg)
            if set_stage:
                set_stage(msg)
        if i % CACHE_FLUSH_EVERY == 0:
            _save_cache(cache)
    _save_cache(cache)
    return found


def plan(tracks: list[dict], overrides: dict | None = None) -> list[dict]:
    """Decide the target spelling per file.

    Per FILE, in order: an explicit override; the file's own album_artist when it
    folds to the same artist; otherwise the most-used spelling across the scan
    (ties to the longer string, so 'AC/DC' beats 'ACDC').

    The majority rule needs the whole library in one scan to be meaningful — a
    majority computed from half a library can pick the wrong winner.
    """
    groups: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for t in tracks:
        groups[artist_key(t["artist"])][t["artist"]] += 1
    majority = {k: max(v, key=lambda a: (v[a], len(a))) for k, v in groups.items()}
    override_by_key = {artist_key(k): v for k, v in (overrides or {}).items()}

    changes = []
    for t in tracks:
        k = artist_key(t["artist"])
        if k in override_by_key:
            target, why = override_by_key[k], "override"
        elif t["album_artist"] and artist_key(t["album_artist"]) == k:
            target, why = t["album_artist"], "album_artist"
        elif len(groups[k]) > 1:
            target, why = majority[k], "majority"
        else:
            continue  # single spelling, nothing to apply
        if target != t["artist"]:
            changes.append({**t, "target": target, "why": why})
    return changes


def _write(changes: list[dict], cache: dict | None = None, set_stage=None) -> int:
    """Apply the planned artist tag to each file.

    Takes the scan `cache` so it can record what it just wrote. That is not an
    optimisation — it is a correctness fix. The cache is keyed on (int mtime,
    size), and rewriting an artist tag to a string of the same length changes
    NEITHER: 'AC, DC' -> 'AC/DC' leaves the file byte-identical in length and the
    mtime in the same whole second. So the cache would keep serving the OLD
    artist, and the next run would plan the same writes all over again. The
    journal hides that, but only while the journal survives; on a fresh container
    with an empty /data it would rewrite the whole library.
    """
    done = set()
    jp = _journal_path()
    if os.path.exists(jp):
        try:
            with open(jp) as fh:
                done = {ln.rstrip("\n") for ln in fh if ln.strip()}
            LOGGER.info("retag: journal has %d already-written file(s)", len(done))
        except OSError as e:
            LOGGER.warning("retag journal unreadable, ignoring: %s", e)

    written = skipped = 0
    total = len(changes)
    with open(jp, "a") as journal:
        for i, c in enumerate(changes, 1):
            if c["path"] in done:
                skipped += 1
            else:
                audio = _open_audio(c["path"])
                if audio is not None:
                    try:
                        audio["artist"] = c["target"]
                        audio.save()
                        written += 1
                        if cache is not None:
                            _recache(cache, c["path"], c["target"], c["album_artist"])
                        # flush per file: the journal is only useful if it
                        # survives the kill that made us need it
                        journal.write(c["path"] + "\n")
                        journal.flush()
                    except Exception as e:  # noqa: BLE001 — keep going
                        LOGGER.warning("retag: write failed %s (%s)", c["path"], e)
            if i % PROGRESS_EVERY == 0 or i == total:
                msg = f"writing tags — {i}/{total}" + (f", {skipped} already done" if skipped else "")
                LOGGER.info("retag: %s", msg)
                if set_stage:
                    set_stage(msg)
    return written


def run(write: bool = False, only: list[str] | None = None,
        overrides: dict | None = None, set_stage=None) -> dict:
    """Scan the library and report (write=False) or apply (write=True) tag fixes."""
    if not MUSIC_DIRS:
        raise RetagError(
            "MUSIC_DIRS is not set — point it at the library roots as mounted in "
            "the container (e.g. MUSIC_DIRS=/music:/itunes) and mount them read-write")
    try:
        import mutagen  # noqa: F401
    except ImportError as e:
        raise RetagError("mutagen is not installed — add it to requirements.txt") from e
    missing = [d for d in MUSIC_DIRS if not os.path.isdir(d)]
    if missing:
        raise RetagError(f"MUSIC_DIRS not found in the container: {missing}")

    if set_stage:
        set_stage("listing files")
    paths = walk_audio(MUSIC_DIRS)
    LOGGER.info("retag: %d audio file(s) under %s", len(paths), MUSIC_DIRS)
    if not paths:
        return {"files": 0, "changes": 0, "written": 0, "dry_run": not write}

    cache = _load_cache()
    tracks = scan(paths, cache, set_stage)
    changes = plan(tracks, overrides)
    if only:
        wanted = {artist_key(a) for a in only}
        changes = [c for c in changes if artist_key(c["artist"]) in wanted]

    by_target: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for c in changes:
        by_target[c["target"]][f'{c["artist"]} (by {c["why"]})'] += 1
    out = {
        "files": len(tracks),
        "artists": len(by_target),
        "changes": len(changes),
        "dry_run": not write,
        # the whole plan, so a dry run on /admin is reviewable without a second call
        "plan": {t: dict(froms) for t, froms in
                 sorted(by_target.items(), key=lambda kv: -sum(kv[1].values()))},
    }
    if write and changes:
        out["written"] = _write(changes, cache, set_stage)
        _save_cache(cache)  # the writes updated it; persist so a rerun sees them
        out["next"] = "trigger a Navidrome rescan, then check /api/library/audit"
    else:
        out["written"] = 0
    LOGGER.info("retag %s: %d change(s) across %d artist(s)",
                "dry run" if not write else "applied", len(changes), len(by_target))
    return out
