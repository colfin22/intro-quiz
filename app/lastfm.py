"""Last.fm global-popularity scorer.

track.getInfo listeners/playcount per library track, cached in the tracks
table. Unknown tracks get 0 (not NULL) so they aren't refetched every run.
"""
import logging
import os
import re
import time

import httpx

API_URL = "https://ws.audioscrobbler.com/2.0/"
API_KEY = os.environ.get("LASTFM_API_KEY", "")
DELAY_S = 0.25  # stay well under Last.fm's rate limit
RETRY_BELOW = 1000  # a hit this weak on a mangled-looking title is worth a cleaned retry

LOGGER = logging.getLogger(__name__)

# Trailing parentheticals that mark a VARIANT of a song rather than a different
# song — safe to strip for a popularity lookup, where the SONG's fame is what
# we're measuring. Deliberately a word-list, not "any parenthetical": titles
# like "Song 2 (Woo Hoo)" or "(I Can't Get No) Satisfaction" must survive. (#11)
_NOISE_PAREN = re.compile(
    r"\s*[(\[][^)\]]*\b(remaster\w*|orig\w*|mono|stereo|single version|album version|"
    r"radio edit|re-?record\w*|live|demo|session\w*|edit|version|mix|duet|feat\.?|"
    r"featuring|with|bonus|deluxe|explicit|clean|acoustic|instrumental|pmedia)\b"
    r"[^)\]]*[)\]]\s*$", re.I)
_TRACK_NUM = re.compile(r"^\s*\d{1,3}[\s.\-_]+\s*")


def clean_title(title: str) -> str:
    """Strip vinyl-rip track numbers and variant-marker parentheticals."""
    t = _TRACK_NUM.sub("", title).strip()
    while True:  # peel stacked suffixes: "Crazy (remastered) (live)"
        t2 = _NOISE_PAREN.sub("", t).strip()
        if t2 == t or not t2:
            break
        t = t2
    return t


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


def lookup_best(http: httpx.Client, artist: str, title: str) -> tuple[int, int]:
    """fetch_track, retrying once with a normalised title when the exact
    lookup comes back weak and the title looks mangled — keeps whichever
    result scores higher. Fixes silent misses on '01 Rape Me' and
    'What A Fool Believes (orig)' class titles. (#11)"""
    listeners, playcount = fetch_track(http, artist, title)
    if listeners < RETRY_BELOW:
        cleaned = clean_title(title)
        if cleaned and cleaned != title:
            l2, p2 = fetch_track(http, artist, cleaned)
            if l2 > listeners:
                LOGGER.info("lastfm matched via cleaned title: %r -> %r (%d listeners)",
                            title, cleaned, l2)
                return l2, p2
    return listeners, playcount


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
                listeners, playcount = lookup_best(http, row["artist"], row["title"])
            except (httpx.HTTPError, LastfmError) as e:
                errors += 1
                LOGGER.warning("lastfm failed for %s - %s: %s", row["artist"], row["title"], e)
                if errors >= 10:  # bail out of a bad run (network down, key revoked)
                    break
                continue
            conn.execute("UPDATE tracks SET global_listeners=?, global_playcount=? WHERE id=?",
                         (listeners, playcount, row["id"]))
            conn.commit()  # tiny write locks — a batch must not starve other writers
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
