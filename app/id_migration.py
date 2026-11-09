"""Convert stored Navidrome IDs to the 0.64+ canonical form (issue #87).

Navidrome 0.64.0 re-encoded every internal ID. The quiz keys tracks, album ids
and clip folders (``CLIPS_DIR/<track_id>/``) on those IDs, so without this
the next library sync would deactivate every track and re-insert the library
as new rows with no tiers, clips, bans or scores.

``ensure_canonical_ids()`` runs at the start of every sync. It does nothing
until the server reports Navidrome 0.64+, then converts once and records that
in ``settings``. It also repairs a library that an older quiz version already
synced against 0.64: the fresh duplicate rows are merged with the originals so
nothing prepared is lost.

Manual use (inside the container)::

    python -m app.id_migration --dry-run     # report only, changes nothing
    python -m app.id_migration --apply       # convert now, no server check
    python -m app.id_migration --reverse id-migration-<stamp>.tsv
"""
import argparse
import datetime as dt
import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field

from . import config, db
from .navidrome_ids import canonical_id, serves_canonical_ids

LOGGER = logging.getLogger(__name__)

SETTING_KEY = "navidrome_id_scheme"
SCHEME_CANONICAL = "canonical"
VERIFY_SAMPLE = 5

# Per-track state worth keeping when two rows turn out to be the same track.
# Track metadata (title, artist, …) and `active` always come from the row the
# server knows today, because the next sync refreshes them anyway.
_KEEP_IF_MISSING = ("tier", "global_listeners", "global_playcount", "quality_notified_at")
_CLIP_COLS = ("clipped_at", "clip_start_s", "intro_offset")


class MigrationError(RuntimeError):
    pass


@dataclass
class Change:
    old: str
    new: str
    action: str            # 'rename' | 'merge'
    clip: str = "none"     # 'rename' | 'kept-target' | 'none'


@dataclass
class Plan:
    changes: list[Change] = field(default_factory=list)
    album_ids: int = 0     # distinct album ids that change
    clip_conflicts: list[str] = field(default_factory=list)

    @property
    def track_ids(self) -> int:
        return len(self.changes)

    @property
    def merges(self) -> int:
        return sum(1 for c in self.changes if c.action == "merge")

    @property
    def clip_renames(self) -> int:
        return sum(1 for c in self.changes if c.clip == "rename")

    def summary(self) -> dict:
        return {"track_ids": self.track_ids, "merges": self.merges,
                "album_ids": self.album_ids, "clip_renames": self.clip_renames,
                "clip_kept_target": sum(1 for c in self.changes if c.clip == "kept-target"),
                "clip_conflicts": len(self.clip_conflicts)}


def _clip_dir(clips_dir: str, track_id: str) -> str:
    return os.path.join(clips_dir, track_id)


def build_plan(conn, clips_dir: str) -> Plan:
    """Work out every change without touching the database or the disk."""
    rows = conn.execute("SELECT id, clipped_at FROM tracks").fetchall()
    ids = {r["id"] for r in rows}
    clipped = {r["id"] for r in rows if r["clipped_at"]}
    plan = Plan()
    targets: dict[str, str] = {}
    planned_dirs: set[str] = set()
    for old in sorted(ids):
        new = canonical_id(old)
        if new == old:
            continue
        # a canonical id is a fixed point, so `new` can never be another
        # changing id; two legacy ids reaching the same target merge in order
        merge = new in ids or new in targets.values()
        change = Change(old, new, "merge" if merge else "rename")
        old_dir, new_dir = _clip_dir(clips_dir, old), _clip_dir(clips_dir, new)
        if os.path.isdir(old_dir):
            target_clipped = merge and (new in clipped or os.path.isdir(new_dir)
                                        or new_dir in planned_dirs)
            if target_clipped:
                change.clip = "kept-target"
            elif os.path.exists(new_dir):
                plan.clip_conflicts.append(new_dir)
            else:
                change.clip = "rename"
                planned_dirs.add(new_dir)
        targets[old] = new
        plan.changes.append(change)
    albums = {r["album_id"] for r in conn.execute(
        "SELECT DISTINCT album_id FROM tracks WHERE album_id IS NOT NULL")}
    plan.album_ids = sum(1 for a in albums if canonical_id(a) != a)
    return plan


def _merge_rows(conn, old: str, new: str, take_clip_state: bool) -> None:
    o = conn.execute("SELECT * FROM tracks WHERE id=?", (old,)).fetchone()
    n = conn.execute("SELECT * FROM tracks WHERE id=?", (new,)).fetchone()
    sets, args = [], []
    for col in _KEEP_IF_MISSING:
        if n[col] is None and o[col] is not None:
            sets.append(f"{col}=?")
            args.append(o[col])
    if take_clip_state:
        for col in _CLIP_COLS:
            sets.append(f"{col}=?")
            args.append(o[col])
    # a ban on either copy stands
    if o["banned"] and not n["banned"]:
        sets += ["banned=1", "ban_reason=?"]
        args.append(o["ban_reason"])
    for col in ("play_count", "starred"):
        if (o[col] or 0) > (n[col] or 0):
            sets.append(f"{col}=?")
            args.append(o[col])
    if sets:
        conn.execute(f"UPDATE tracks SET {', '.join(sets)} WHERE id=?", (*args, new))
    conn.execute("DELETE FROM tracks WHERE id=?", (old,))


def _map_path(db_path: str) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = os.path.dirname(os.path.abspath(db_path)) if db_path != ":memory:" else os.getcwd()
    return os.path.join(base, f"id-migration-{stamp}.tsv")


def _backup_db(conn, db_path: str) -> str | None:
    if not db_path or db_path == ":memory:":
        return None
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = f"{db_path}.pre-id-migration-{stamp}"
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
    finally:
        target.close()
    return dest


def apply_plan(conn, plan: Plan, clips_dir: str, db_path: str) -> dict:
    """Back up the db, rename clip folders, rewrite ids in one transaction.

    Clip folders are renamed first; if anything fails the renames are undone
    and the transaction is rolled back, so the library is never half-converted.
    """
    if plan.clip_conflicts:
        raise MigrationError(
            "clip folders already exist under the new ids for tracks the database "
            f"doesn't link to them: {plan.clip_conflicts[:5]} — move them aside and retry")
    backup = _backup_db(conn, db_path)
    renamed: list[tuple[str, str]] = []
    try:
        for c in plan.changes:
            if c.clip == "rename":
                src, dst = _clip_dir(clips_dir, c.old), _clip_dir(clips_dir, c.new)
                os.rename(src, dst)
                renamed.append((src, dst))
        conn.create_function("canonical_id", 1, canonical_id, deterministic=True)
        for c in plan.changes:
            if c.action == "merge":
                _merge_rows(conn, c.old, c.new, take_clip_state=(c.clip == "rename"))
            else:
                conn.execute("UPDATE tracks SET id=? WHERE id=?", (c.new, c.old))
        conn.execute("UPDATE tracks SET album_id=canonical_id(album_id) "
                     "WHERE album_id IS NOT NULL AND album_id != canonical_id(album_id)")
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (SETTING_KEY, SCHEME_CANONICAL))
        conn.commit()
    except Exception:
        conn.rollback()
        for src, dst in reversed(renamed):
            try:
                os.rename(dst, src)
            except OSError:
                LOGGER.exception("could not undo clip rename %s -> %s", src, dst)
        raise
    map_file = _map_path(db_path)
    try:
        with open(map_file, "w") as f:
            f.write("old_id\tnew_id\taction\tclip\n")
            for c in plan.changes:
                f.write(f"{c.old}\t{c.new}\t{c.action}\t{c.clip}\n")
    except OSError:
        LOGGER.exception("id migration done but the map file could not be written")
        map_file = None
    out = {**plan.summary(), "backup": backup, "map_file": map_file}
    LOGGER.info("navidrome id migration applied: %s", json.dumps(out))
    return out


def ensure_canonical_ids(conn, client, clips_dir: str | None = None,
                         db_path: str | None = None) -> dict | None:
    """Convert once the server serves canonical ids. Called before every sync."""
    if db.get_setting(conn, SETTING_KEY) == SCHEME_CANONICAL:
        return None
    info = client.server_info()
    if not serves_canonical_ids(info.get("type"), info.get("version")):
        return None
    clips_dir = clips_dir or config.CLIPS_DIR
    plan = build_plan(conn, clips_dir)
    if not plan.changes and not plan.album_ids:
        db.set_setting(conn, SETTING_KEY, SCHEME_CANONICAL)
        return {"track_ids": 0}
    # prove the conversion matches this server before changing anything
    sample = [r["id"] for r in conn.execute(
        "SELECT id FROM tracks WHERE active=1 ORDER BY id").fetchall()]
    changed = {c.old: c.new for c in plan.changes if c.action == "rename"}
    probe = [i for i in sample if i in changed][:VERIFY_SAMPLE]
    found = sum(1 for old in probe if client.song_exists(changed[old]))
    # a song removed since the last sync may be missing; most must be there
    if probe and found * 2 <= len(probe):
        raise MigrationError(
            f"server is {info.get('type')} {info.get('version')} but only {found} of "
            f"{len(probe)} converted ids are known to it — nothing was changed")
    LOGGER.info("navidrome %s serves canonical ids; converting %s",
                info.get("version"), json.dumps(plan.summary()))
    return apply_plan(conn, plan, clips_dir, db_path or config.DB_PATH)


def reverse(conn, map_file: str, clips_dir: str) -> dict:
    """Undo the renames recorded in a map file (merged duplicates stay merged).

    Album ids are left canonical; a sync against the old server rewrites them.
    """
    restored = clips = 0
    with open(map_file) as f:
        next(f)
        entries = [line.rstrip("\n").split("\t") for line in f if line.strip()]
    for old, new, action, clip in entries:
        if action != "rename":
            continue
        if clip == "rename":
            src, dst = _clip_dir(clips_dir, new), _clip_dir(clips_dir, old)
            if os.path.isdir(src) and not os.path.exists(dst):
                os.rename(src, dst)
                clips += 1
        restored += conn.execute("UPDATE tracks SET id=? WHERE id=?", (old, new)).rowcount
    conn.execute("DELETE FROM settings WHERE key=?", (SETTING_KEY,))
    conn.commit()
    return {"track_ids_restored": restored, "clip_dirs_restored": clips}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.id_migration", description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="report what would change, change nothing")
    g.add_argument("--apply", action="store_true", help="convert now without asking the server")
    g.add_argument("--reverse", metavar="MAP_TSV", help="undo a previous migration from its map file")
    ap.add_argument("--db", default=config.DB_PATH)
    ap.add_argument("--clips", default=config.CLIPS_DIR)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = db.connect(args.db)
    try:
        if args.reverse:
            print(json.dumps(reverse(conn, args.reverse, args.clips)))
            return 0
        plan = build_plan(conn, args.clips)
        if args.dry_run:
            print(json.dumps(plan.summary()))
            for p in plan.clip_conflicts[:20]:
                print(f"conflict: {p}")
            return 1 if plan.clip_conflicts else 0
        print(json.dumps(apply_plan(conn, plan, args.clips, args.db)))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
