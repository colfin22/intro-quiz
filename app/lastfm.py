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
#
# The soundtrack markers (#38) are the same class, and fail more quietly than #34:
# a film tag does not zero the score, it merely gets the WRONG one. The song is
# looked up under a title Last.fm does not file it by, scores a few thousand
# instead of millions, and lands in the hard/tiebreak tier — present, clipped,
# and never asked. Nothing looks broken.
#
# 'remix' is deliberately NOT here. A remix is a distinct work, not a variant:
# stripping it would score an obscure remix as the famous original and promote it
# into the pool the quiz draws from. ('mix' covers the (Extended Mix) variant case.)
_NOISE_PAREN = re.compile(
    r"\s*[(\[][^)\]]*\b(remaster\w*|orig\w*|mono|stereo|single version|album version|"
    r"radio edit|re-?record\w*|live|demo|session\w*|edit|version|mix|duet|feat\.?|"
    r"featuring|with|bonus|deluxe|explicit|clean|acoustic|instrumental|pmedia|"
    r"soundtrack|o\.?s\.?t\.?|motion picture|theme|"
    r"from the (film|movie|motion picture)|from ['‘“\"])"
    r"[^)\]]*[)\]]\s*$", re.I)
_TRACK_NUM = re.compile(r"^\s*\d{1,3}[\s.\-_]+\s*")

# A featured credit in the ARTIST tag ("X Feat. Y", "X ft. Y", "X (feat. Y)").
# Last.fm files a song under its PRIMARY artist and keeps the guest in the credits,
# so the tagged string matches no artist at all — the lookup returns nothing, the
# track never gets a tier, never gets a clip, and silently never appears in the quiz.
# Chart pop is tagged this way most of all, so the music the family is likeliest to
# KNOW was the likeliest to be missing (#34).
#
# Deliberately narrow. '&', '+' and 'and' are NOT featured credits — Simon & Garfunkel,
# Hall & Oates, Florence + The Machine are whole artists, and stripping them would
# invent a different act and score the wrong song.
_FEAT_ARTIST = re.compile(
    r"\s*[(\[]?\b(feat\.?|ft\.?|featuring|with)\b.*$", re.I)


def clean_title(title: str) -> str:
    """Strip vinyl-rip track numbers and variant-marker parentheticals."""
    t = _TRACK_NUM.sub("", title).strip()
    while True:  # peel stacked suffixes: "Crazy (remastered) (live)"
        t2 = _NOISE_PAREN.sub("", t).strip()
        if t2 == t or not t2:
            break
        t = t2
    return t


def clean_artist(artist: str) -> str:
    """Strip a featured-artist credit, leaving the primary artist (#34)."""
    return _FEAT_ARTIST.sub("", artist or "").strip(" -–—,").strip()


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
    """fetch_track, retrying with a normalised artist/title when the exact lookup
    comes back weak — keeps whichever variant scores highest.

    Ways a tag hides a famous song from us, all silent:
      - a mangled TITLE: '01 Rape Me', 'What A Fool Believes (orig)'   (#11)
      - a featured credit in the ARTIST: 'X Feat. Y'                   (#34)
      - a film marker: 'Ticket to Ride [from the Film "Help!"]'        (#38)
    None of them error. The track scores wrongly, tiers wrongly, and quietly stops
    appearing in the quiz — indistinguishable from music you don't own.

    A healthy-looking hit is NOT evidence the tag was right (#40). A mangled title
    often does resolve — to a junk entry or a soundtrack listing — with a few
    thousand listeners, clearing RETRY_BELOW and leaving the wrong number in place:
    'Ticket to Ride [from the Film "Help! "]' scores 4,299; the song scores 771,304.
    So whenever the cleaner CHANGES anything, we always try the cleaned forms and
    keep the highest. RETRY_BELOW only short-circuits a tag with nothing to clean.
    """
    c_artist, c_title = clean_artist(artist), clean_title(title)
    cleanable = (c_artist, c_title) != (artist, title)

    best = fetch_track(http, artist, title)
    if best[0] >= RETRY_BELOW and not cleanable:
        return best

    tried = {(artist, title)}
    for a, t in ((artist, c_title), (c_artist, title), (c_artist, c_title)):
        if (a, t) in tried or not a or not t:
            continue          # deduped: with only the title cleanable, two of these coincide
        tried.add((a, t))
        got = fetch_track(http, a, t)
        if got[0] > best[0]:
            LOGGER.info("lastfm matched via cleaned tags: %r/%r -> %r/%r (%d listeners)",
                        artist, title, a, t, got[0])
            best = got
    return best


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
