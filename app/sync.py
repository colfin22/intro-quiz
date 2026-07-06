"""Library sync: walk every album via the Subsonic API and upsert track metadata.

Play counts and stars are per-user in Navidrome, so they do NOT come from this
sync (the app's own user has none) — they arrive separately via the annotation
export ingest. This sync owns identity + metadata + liveness (`active`).
"""
import logging

from . import subsonic

LOGGER = logging.getLogger(__name__)


def sync_library(conn, client: subsonic.Client) -> dict:
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
                    "INSERT INTO tracks(id,title,artist,album,album_id,genre,year,duration,cover_art,active) "
                    "VALUES(?,?,?,?,?,?,?,?,?,1) "
                    "ON CONFLICT(id) DO UPDATE SET title=excluded.title, artist=excluded.artist, "
                    "album=excluded.album, album_id=excluded.album_id, genre=excluded.genre, "
                    "year=excluded.year, duration=excluded.duration, cover_art=excluded.cover_art, active=1",
                    (s["id"], s.get("title", "?"), s.get("artist", "?"), s.get("album"),
                     album["id"], s.get("genre"), s.get("year"), s.get("duration"),
                     s.get("coverArt")))
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
    conn.commit()
    total = conn.execute("SELECT COUNT(*) c FROM tracks WHERE active=1").fetchone()["c"]
    return {"albums": albums, "tracks_active": total, "deactivated": deactivated}
