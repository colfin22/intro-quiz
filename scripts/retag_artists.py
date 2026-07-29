"""Fix variant spellings of one artist in the files' own tags.

The app already folds variants together for grouping and decoys (see
app/library.artist_key), so this script is about the text players *see*: a
scoreboard reading 'AC, DC' is wrong even when the quiz treats it correctly.

    # look, change nothing (start here)
    python scripts/retag_artists.py --root "/Volumes/nas-ssd/itunes/iTunes Music" --dry-run

    # one artist at a time while you build trust
    python scripts/retag_artists.py --root ... --only "AC/DC" --write

    # everything, both libraries
    python scripts/retag_artists.py --root "/Volumes/nas-ssd/music" \
        --root "/Volumes/nas-ssd/itunes/iTunes Music" --write

LARGE LIBRARIES. Reading tags from ~22,000 files on a network mount takes tens of
minutes, so this is built to be interrupted:

- **Progress goes to stderr** ("4200/22065 files, 61/s, ~4m left"), so it stays
  visible even when you pipe stdout to `tail`. Silence would be
  indistinguishable from a hang.
- **The tag scan is cached** in --cache (default .retag-cache.json), keyed by
  path + mtime + size. Ctrl-C and re-run and it picks up where it stopped; a
  second full run costs seconds instead of minutes. Edit a file and its entry
  invalidates itself.
- **Writes are journalled** to --journal (default .retag-journal.txt), one path
  per line as it completes. A re-run skips what's already written, so an
  interrupted --write never redoes work and never half-writes a file twice.
- Ctrl-C at any point flushes the cache and exits cleanly.

WHAT IT DOES NOT DO. It never renames or moves a file. Navidrome derives its
track ids from the file path, so a rename re-ids every track and orphans the
clips already cut for it — the single most expensive mistake available here.
Folder names like 'AC+DC/' and 'AC_DC/' are therefore left exactly as they are;
they're only mangled because '/' is illegal in a path, and Navidrome reads
embedded tags, not directory names, so they cost nothing.

HOW IT DECIDES the correct spelling, per file, in order:
  1. `--map "wrong=right"` if you gave one for that artist;
  2. the file's own `album_artist` tag, when that folds to the same artist (it's
     right in 14 of the 26 AC/DC variant files here — 'AC, DC' files carry
     `album_artist=AC/DC`, 'AC-DC' files have no album_artist at all);
  3. the most-used spelling of that artist across the scanned files, ties going
     to the longer string so 'AC/DC' wins over 'ACDC'.

Rule 3 is a popularity vote, so --dry-run prints the reason for every file
grouped by artist — skim it before writing. Rule 3 also needs the WHOLE scan to
be meaningful: pass every --root you own in one run, or a majority computed from
half your library may pick the wrong winner.

A LEADING 'THE' IS NEVER ADDED OR REMOVED by rules 2 and 3. artist_key strips it
so variants fold, which is what the quiz wants — but that also means whichever
spelling the album_artist happens to use would "win", and on a real library that
came out as 'The Beatles' -> 'Beatles', 'The Eagles' -> 'Eagles' and
'Stray Cats' -> 'The Stray Cats': 209 of 811 changes, churn in both directions,
no winner. Use --map if you do have an opinion about a particular band.

After writing, trigger a Navidrome rescan so the DB picks the new tags up, then
re-run the audit (GET /api/library/audit) to confirm the variant count dropped.

Needs mutagen (pip install mutagen) and reads/writes only audio tags.
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.library import artist_key  # noqa: E402
from app.retag import _article_only  # noqa: E402  (shared with the server-side job)

AUDIO_EXT = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wma", ".aac", ".aiff", ".alac"}
# tag keys holding the track artist and the album artist, across container formats
ARTIST_KEYS = ("artist", "TPE1", "\xa9ART")
ALBUM_ARTIST_KEYS = ("albumartist", "album_artist", "TPE2", "aART", "ALBUMARTIST")
PROGRESS_EVERY = 200     # files between progress lines
# Files between cache saves, so a kill loses little. Deliberately the same as
# PROGRESS_EVERY: a bigger interval means a library smaller than it flushes
# nothing until the very end, and an interrupt then throws the whole scan away —
# exactly the case this cache exists for. A save is one small local JSON write.
CACHE_FLUSH_EVERY = PROGRESS_EVERY


def note(msg: str) -> None:
    """Progress to stderr: survives `| tail`, and won't corrupt piped output."""
    print(msg, file=sys.stderr, flush=True)


def human(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


class Cache:
    """path -> [mtime, size, artist, album_artist]. Invalidated by mtime/size."""

    def __init__(self, path: str):
        self.path = path
        self.data: dict[str, list] = {}
        self.dirty = False
        if path and os.path.exists(path):
            try:
                with open(path) as fh:
                    self.data = json.load(fh)
                note(f"cache: {len(self.data)} file(s) already read ({path})")
            except (OSError, ValueError) as e:
                note(f"cache unreadable, starting fresh: {e}")

    def get(self, path: str, st) -> tuple | None:
        hit = self.data.get(path)
        if hit and hit[0] == int(st.st_mtime) and hit[1] == st.st_size:
            return hit[2], hit[3]
        return None

    def put(self, path: str, st, artist, album_artist) -> None:
        self.data[path] = [int(st.st_mtime), st.st_size, artist, album_artist]
        self.dirty = True

    def save(self) -> None:
        if not self.path or not self.dirty:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(self.data, fh)
            os.replace(tmp, self.path)  # atomic: a kill mid-write can't corrupt it
            self.dirty = False
        except OSError as e:
            note(f"could not save cache: {e}")


def load_journal(path: str) -> set[str]:
    if not path or not os.path.exists(path):
        return set()
    try:
        with open(path) as fh:
            done = {ln.rstrip("\n") for ln in fh if ln.strip()}
        if done:
            note(f"journal: {len(done)} file(s) already written ({path})")
        return done
    except OSError as e:
        note(f"journal unreadable, ignoring: {e}")
        return set()


def load(path):
    import mutagen
    try:
        return mutagen.File(path, easy=True)
    except Exception as e:  # noqa: BLE001 — a corrupt file must not stop the scan
        note(f"  ! unreadable, skipped: {path} ({e})")
        return None


def tag_get(audio, keys):
    for k in keys:
        v = audio.get(k)
        if v:
            return (v[0] if isinstance(v, list) else v) or None
    return None


def walk_audio(roots: list[str]) -> list[str]:
    """Every audio path under the roots. Metadata only — fast even on a NAS."""
    out = []
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if os.path.splitext(name)[1].lower() in AUDIO_EXT:
                    out.append(os.path.join(dirpath, name))
    return out


def scan(paths: list[str], cache: Cache) -> list[dict]:
    """Read the artist tags, using and filling the cache. Interruptible."""
    found, started, cached = [], time.monotonic(), 0
    total = len(paths)
    for i, path in enumerate(paths, 1):
        try:
            st = os.stat(path)
        except OSError:
            continue
        hit = cache.get(path, st)
        if hit:
            artist, album_artist = hit
            cached += 1
        else:
            audio = load(path)
            if audio is None:
                continue
            artist = tag_get(audio, ARTIST_KEYS)
            album_artist = tag_get(audio, ALBUM_ARTIST_KEYS)
            cache.put(path, st, artist, album_artist)
        if artist:
            found.append({"path": path, "artist": artist, "album_artist": album_artist})
        if i % PROGRESS_EVERY == 0 or i == total:
            elapsed = time.monotonic() - started
            rate = i / elapsed if elapsed else 0
            left = (total - i) / rate if rate else 0
            note(f"  scanned {i}/{total} ({100 * i // total}%), {rate:.0f}/s, "
                 f"~{human(left)} left, {cached} from cache")
        if i % CACHE_FLUSH_EVERY == 0:
            cache.save()
    cache.save()
    return found


def plan(tracks: list[dict], overrides: dict) -> list[dict]:
    """Decide the target spelling per file. See the module docstring for the order."""
    groups: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for t in tracks:
        groups[artist_key(t["artist"])][t["artist"]] += 1

    # rule 3's winner per artist: most files, then longest string
    majority = {k: max(v, key=lambda a: (v[a], len(a))) for k, v in groups.items()}
    override_by_key = {artist_key(k): v for k, v in overrides.items()}

    changes = []
    for t in tracks:
        k = artist_key(t["artist"])
        if k in override_by_key:
            target, why = override_by_key[k], "--map"
        elif t["album_artist"] and artist_key(t["album_artist"]) == k:
            target, why = t["album_artist"], "album_artist"
        elif len(groups[k]) > 1:
            target, why = majority[k], "majority"
        else:
            continue  # only one spelling and nothing to apply — leave it alone
        if why != "--map" and _article_only(target, t["artist"]):
            # 'The Verve' vs 'Verve' folds to one artist already, so rewriting the
            # files buys nothing and picks a side at random. See app/retag.plan.
            continue
        if target != t["artist"]:
            changes.append({**t, "target": target, "why": why})
    return changes


def apply(changes: list[dict], journal_path: str, done: set[str],
          cache: "Cache | None" = None) -> int:
    """Write the tags, journalling each success so a re-run resumes.

    Also re-caches each file it writes. The cache key is (int mtime, size), and
    swapping an artist tag for a same-length string ('AC, DC' -> 'AC/DC') changes
    neither — so without this the cache keeps returning the OLD artist and the
    next run plans every write again. The journal masks it until you delete the
    journal, which the finished-run message tells you to do.
    """
    written, skipped, started = 0, 0, time.monotonic()
    total = len(changes)
    journal = open(journal_path, "a") if journal_path else None
    try:
        for i, c in enumerate(changes, 1):
            if c["path"] in done:
                skipped += 1
            else:
                audio = load(c["path"])
                if audio is not None:
                    try:
                        audio["artist"] = c["target"]
                        audio.save()
                        written += 1
                        if cache is not None:
                            try:
                                cache.put(c["path"], os.stat(c["path"]),
                                          c["target"], c["album_artist"])
                            except OSError:
                                pass
                        if journal:
                            # flush per file: the journal is only useful if it
                            # survives the kill that made us need it
                            journal.write(c["path"] + "\n")
                            journal.flush()
                    except Exception as e:  # noqa: BLE001 — one bad file isn't fatal
                        note(f"  ! write failed: {c['path']} ({e})")
            if i % PROGRESS_EVERY == 0 or i == total:
                elapsed = time.monotonic() - started
                rate = i / elapsed if elapsed else 0
                note(f"  wrote {i}/{total}, {rate:.0f}/s, "
                     f"~{human((total - i) / rate if rate else 0)} left"
                     + (f", {skipped} already done" if skipped else ""))
    finally:
        if journal:
            journal.close()
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, action="append",
                    help="library root to scan (repeatable — pass them all in one run)")
    ap.add_argument("--only", action="append", default=[],
                    help="limit the CHANGES to this artist (any spelling); repeatable")
    ap.add_argument("--map", action="append", default=[], metavar="WRONG=RIGHT",
                    help="force a spelling, e.g. --map 'AC, DC=AC/DC'")
    ap.add_argument("--cache", default=".retag-cache.json",
                    help="tag-scan cache for restarts (default: %(default)s; '' to disable)")
    ap.add_argument("--journal", default=".retag-journal.txt",
                    help="record of completed writes (default: %(default)s; '' to disable)")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="print changes, write nothing")
    group.add_argument("--write", action="store_true", help="actually write the tags")
    args = ap.parse_args()

    try:
        import mutagen  # noqa: F401
    except ImportError:
        print("needs mutagen:  pip install mutagen", file=sys.stderr)
        return 2

    overrides = {}
    for m in args.map:
        if "=" not in m:
            print(f"--map wants WRONG=RIGHT, got {m!r}", file=sys.stderr)
            return 2
        wrong, right = m.split("=", 1)
        overrides[wrong.strip()] = right.strip()

    for root in args.root:
        if not os.path.isdir(root):
            print(f"not a directory: {root}", file=sys.stderr)
            return 2

    cache = Cache(args.cache)
    note("listing files …")
    paths = walk_audio(args.root)
    note(f"{len(paths)} audio file(s) under {len(args.root)} root(s)")
    if not paths:
        return 0

    try:
        tracks = scan(paths, cache)
    except KeyboardInterrupt:
        cache.save()
        note("\ninterrupted — cache saved. Re-run the same command to resume.")
        return 130
    note(f"read artist tags from {len(tracks)} file(s)")

    changes = plan(tracks, overrides)
    if args.only:
        wanted = {artist_key(a) for a in args.only}
        changes = [c for c in changes if artist_key(c["artist"]) in wanted]

    if not changes:
        print("nothing to change.")
        return 0

    by_target: dict[str, list[dict]] = defaultdict(list)
    for c in changes:
        by_target[c["target"]].append(c)
    print(f"\n{len(changes)} file(s) in {len(by_target)} artist(s):\n")
    for target, cs in sorted(by_target.items(), key=lambda kv: -len(kv[1])):
        print(f"  -> {target!r}")
        # the reason is PER FILE, not per artist: within one artist some files
        # carry a correct album_artist and others fall through to the majority
        # vote. Printing one reason for the group would hide the weaker evidence,
        # which is the thing you're reading this output to check.
        froms: dict[tuple, int] = defaultdict(int)
        for c in cs:
            froms[(c["artist"], c["why"])] += 1
        for (artist, why), n in sorted(froms.items(), key=lambda x: -x[1]):
            print(f"       from {artist!r} x{n}  (by {why})")

    if args.dry_run:
        print("\ndry run — nothing written. Re-run with --write to apply.")
        return 0

    done = load_journal(args.journal)
    note(f"\nwriting {len(changes)} file(s) …")
    try:
        written = apply(changes, args.journal, done, cache)
    except KeyboardInterrupt:
        cache.save()
        note("\ninterrupted — journalled progress kept. Re-run to resume.")
        return 130
    cache.save()  # the writes updated it; persist so a re-run sees the new tags
    print(f"wrote {written} file(s).")
    if args.journal:
        print(f"safe to delete {args.journal} now — the cache also records what was "
              "written, so a re-run won't redo these.")
    print("\nNow trigger a Navidrome rescan, then check:  GET /api/library/audit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
