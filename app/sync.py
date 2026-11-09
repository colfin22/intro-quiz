"""Library sync: walk every album via the Subsonic API and upsert track metadata.

Play counts and stars are per-user in Navidrome, so they do NOT come from this
sync (the app's own user has none) — they arrive separately via the annotation
export ingest. This sync owns identity + metadata + liveness (`active`).
"""
import logging

from . import config, id_migration, subsonic

LOGGER = logging.getLogger(__name__)


class SyncGuardError(RuntimeError):
    """A sync pass that would deactivate most of the library — rolled back."""


def sync_library(conn, client: subsonic.Client, clips_dir: str | None = None,
                 db_path: str | None = None) -> dict:
    # Navidrome 0.64+ re-encoded every id (#87): convert stored ids first, or
    # this pass would see none of them and orphan the whole library
    migrated = id_migration.ensure_canonical_ids(conn, client, clips_dir=clips_dir,
                                                 db_path=db_path)
    active_before = conn.execute("SELECT COUNT(*) c FROM tracks WHERE active=1").fetchone()["c"]
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
                conn.execute(
                    "INSERT INTO tracks(id,title,artist,album,album_artist,album_id,genre,year,duration,cover_art,active) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,1) "
                    "ON CONFLICT(id) DO UPDATE SET title=excluded.title, artist=excluded.artist, "
                    "album=excluded.album, album_artist=excluded.album_artist, album_id=excluded.album_id, "
                    "genre=excluded.genre, year=excluded.year, duration=excluded.duration, "
                    "cover_art=excluded.cover_art, active=1",
                    (s["id"], s.get("title", "?"), s.get("artist", "?"), s.get("album"),
                     album.get("artist"), album["id"], s.get("genre"), s.get("year"),
                     s.get("duration"), s.get("coverArt")))
        offset += len(batch)
        LOGGER.info("synced %d albums so far", offset)
    # anything not seen this pass is gone from the library
    if seen:
        placeholders = ",".join("?" * len(seen))
        deactivated = conn.execute(
            f"UPDATE tracks SET active=0 WHERE active=1 AND id NOT IN ({placeholders})",
            tuple(seen)).rowcount
    else:
        deactivated = 0
    limit = config.SYNC_MAX_DEACTIVATE_FRACTION
    if (limit < 1 and active_before >= config.SYNC_GUARD_MIN_TRACKS
            and deactivated > active_before * limit):
        conn.rollback()
        raise SyncGuardError(
            f"sync would deactivate {deactivated} of {active_before} active tracks — "
            "rolled back, nothing changed. If the music server changed its ids, upgrade "
            "intro-quiz; if the library really shrank, set SYNC_MAX_DEACTIVATE_FRACTION=1 "
            "for one sync")
    conn.commit()
    total = conn.execute("SELECT COUNT(*) c FROM tracks WHERE active=1").fetchone()["c"]
    out = {"albums": albums, "tracks_active": total, "deactivated": deactivated}
    if migrated:
        out["id_migration"] = migrated
    return out
