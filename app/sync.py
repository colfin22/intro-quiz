"""Library sync: walk every album via the Subsonic API and upsert track metadata.

Play counts and stars are per-user in Navidrome, so they do NOT come from this
sync (the app's own user has none) — they arrive separately via the annotation
export ingest. This sync owns identity + metadata + liveness (`active`).
"""
import logging
from datetime import datetime, timezone

from . import config, db, id_migration, subsonic

LOGGER = logging.getLogger(__name__)

# lossy formats: their bitrate is part of the audio itself, so a different one means a
# different encode. For lossless files the reported bitrate moves with the file size,
# which a tag edit or embedded cover art changes — so it is left out for those.
_LOSSY = {"mp3", "m4a", "aac", "ogg", "oga", "opus", "wma", "mpc"}


def source_signature(song: dict) -> str | None:
    """The audio properties of the file behind a song, as one comparable string.

    Two syncs giving a different value for the same track id mean the file was
    replaced in place (same path, new audio) and its clips should be re-cut (#90).
    Tag edits never change it: the file size is deliberately not part of it.
    None when the server doesn't report the file format at all.
    """
    suffix = (song.get("suffix") or "").lower()
    if not suffix:
        return None
    parts = [suffix, song.get("samplingRate"), song.get("bitDepth"),
             song.get("channelCount"), song.get("duration")]
    if suffix in _LOSSY:
        parts.append(song.get("bitRate"))
    return "|".join("" if v is None else str(v) for v in parts)


class SyncGuardError(RuntimeError):
    """A sync pass that would deactivate most of the library — rolled back."""


def sync_library(conn, client: subsonic.Client, clips_dir: str | None = None,
                 db_path: str | None = None) -> dict:
    # Navidrome 0.64+ re-encoded every id (#87): convert stored ids first, or
    # this pass would see none of them and orphan the whole library
    migrated = id_migration.ensure_canonical_ids(conn, client, clips_dir=clips_dir,
                                                 db_path=db_path)
    active_before = conn.execute("SELECT COUNT(*) c FROM tracks WHERE active=1").fetchone()["c"]
    now = datetime.now(timezone.utc).isoformat()
    # signatures of tracks that have clips, to count in-place replacements this pass
    old_sigs = {r["id"]: r["source_sig"] for r in conn.execute(
        "SELECT id, source_sig FROM tracks WHERE clipped_at IS NOT NULL AND source_sig IS NOT NULL")}
    replaced = 0
    seen: set[str] = set()
    albums = 0
    offset = 0
    while True:
        batch = client.album_list(offset=offset)
        if not batch:
            break
        for album in batch:
            albums += 1
            for s in client.album_songs(album["id"]):
                seen.add(s["id"])
                sig = source_signature(s)
                old = old_sigs.get(s["id"])
                if old is not None and sig is not None and old != sig:
                    replaced += 1
                # a changed signature on a track that has clips = replaced in place:
                # flag it for a re-cut (a first signature is only recorded, never a change)
                conn.execute(
                    "INSERT INTO tracks(id,title,artist,album,album_artist,album_id,genre,year,duration,"
                    "cover_art,active,source_sig) VALUES(?,?,?,?,?,?,?,?,?,?,1,?) "
                    "ON CONFLICT(id) DO UPDATE SET title=excluded.title, artist=excluded.artist, "
                    "album=excluded.album, album_artist=excluded.album_artist, album_id=excluded.album_id, "
                    "genre=excluded.genre, year=excluded.year, duration=excluded.duration, "
                    "cover_art=excluded.cover_art, active=1, inactive_since=NULL, "
                    "source_changed=CASE WHEN tracks.clipped_at IS NOT NULL AND tracks.source_sig IS NOT NULL "
                    "AND excluded.source_sig IS NOT NULL AND tracks.source_sig != excluded.source_sig "
                    "THEN 1 ELSE tracks.source_changed END, "
                    "source_sig=COALESCE(excluded.source_sig, tracks.source_sig)",
                    (s["id"], s.get("title", "?"), s.get("artist", "?"), s.get("album"),
                     album.get("artist"), album["id"], s.get("genre"), s.get("year"),
                     s.get("duration"), s.get("coverArt"), sig))
        offset += len(batch)
        LOGGER.info("synced %d albums so far", offset)
    # anything not seen this pass is gone from the library
    if seen:
        placeholders = ",".join("?" * len(seen))
        deactivated = conn.execute(
            f"UPDATE tracks SET active=0, inactive_since=? WHERE active=1 AND id NOT IN ({placeholders})",
            (now, *seen)).rowcount
    else:
        deactivated = 0
    # tracks that went inactive before the date was kept start their wait now
    conn.execute("UPDATE tracks SET inactive_since=? WHERE active=0 AND inactive_since IS NULL", (now,))
    limit = config.SYNC_MAX_DEACTIVATE_FRACTION
    if (limit < 1 and active_before >= config.SYNC_GUARD_MIN_TRACKS
            and deactivated > active_before * limit):
        conn.rollback()
        # the clip prune (#90) stays off until a sync completes normally again
        db.set_setting(conn, "sync_refused_at", now)
        raise SyncGuardError(
            f"sync would deactivate {deactivated} of {active_before} active tracks — "
            "rolled back, nothing changed. If the music server changed its ids, upgrade "
            "intro-quiz; if the library really shrank, set SYNC_MAX_DEACTIVATE_FRACTION=1 "
            "for one sync")
    conn.commit()
    db.set_setting(conn, "sync_ok_at", now)
    total = conn.execute("SELECT COUNT(*) c FROM tracks WHERE active=1").fetchone()["c"]
    out = {"albums": albums, "tracks_active": total, "deactivated": deactivated}
    if replaced:
        out["replaced_in_place"] = replaced
    if migrated:
        out["id_migration"] = migrated
    return out
