"""Last.fm global-popularity scorer.

track.getInfo listeners/playcount per library track, cached in the tracks
table. Unknown tracks get 0 (not NULL) so they aren't refetched every run.
"""
import logging
import os
import time

import httpx

API_URL = "https://ws.audioscrobbler.com/2.0/"
API_KEY = os.environ.get("LASTFM_API_KEY", "")
DELAY_S = 0.25  # stay well under Last.fm's rate limit

LOGGER = logging.getLogger(__name__)


class LastfmError(RuntimeError):
    pass


def fetch_track(http: httpx.Client, artist: str, title: str) -> tuple[int, int]:
    """Return (listeners, playcount); (0, 0) when Last.fm doesn't know the track."""
    r = http.get(API_URL, params={
        "method": "track.getInfo", "api_key": API_KEY,
        "artist": artist, "track": title, "autocorrect": 1, "format": "json"})
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        if body["error"] == 6:  # track not found
            return 0, 0
        raise LastfmError(f"lastfm error {body['error']}: {body.get('message')}")
    t = body.get("track", {})
    return int(t.get("listeners", 0)), int(t.get("playcount", 0))


def score_batch(conn, limit: int = 200, http: httpx.Client | None = None,
                delay_s: float | None = None) -> dict:
    """Score up to `limit` active tracks that have no global score yet."""
    if not API_KEY:
        raise LastfmError("LASTFM_API_KEY not set")
    own_http = http is None
    http = http or httpx.Client(timeout=20)
    delay = DELAY_S if delay_s is None else delay_s
    rows = conn.execute(
        "SELECT id, artist, title FROM tracks WHERE active=1 AND global_listeners IS NULL "
        "ORDER BY play_count DESC, id LIMIT ?", (limit,)).fetchall()
    done = errors = 0
    try:
        for row in rows:
            try:
                listeners, playcount = fetch_track(http, row["artist"], row["title"])
            except (httpx.HTTPError, LastfmError) as e:
                errors += 1
                LOGGER.warning("lastfm failed for %s - %s: %s", row["artist"], row["title"], e)
                if errors >= 10:  # bail out of a bad run (network down, key revoked)
                    break
                continue
            conn.execute("UPDATE tracks SET global_listeners=?, global_playcount=? WHERE id=?",
                         (listeners, playcount, row["id"]))
            done += 1
            if delay:
                time.sleep(delay)
        conn.commit()
    finally:
        if own_http:
            http.close()
    remaining = conn.execute(
        "SELECT COUNT(*) c FROM tracks WHERE active=1 AND global_listeners IS NULL").fetchone()["c"]
    return {"scored": done, "errors": errors, "remaining": remaining}
