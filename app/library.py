"""Library hygiene: artist-name variants, duplicate rows, and unclippable tracks.

Three problems that all look like "duplicates" but need opposite treatment, so
they're measured apart here rather than fixed with one blunt rule:

1. **Variant spellings of one artist.** 'AC/DC' (257 tracks), 'AC, DC' (14),
   'AC-DC' (12), 'ACDC' (1) are one band. Last.fm's autocorrect mostly rescues
   the SCORE, so this is not a scoring bug — it's a game bug two ways. The
   artist wall needs `n >= 3` per artist and the split drops 33 artists below
   it (376 tracks unreachable), and `pick_decoys` excludes on a literal
   `artist != ?`, so a round can offer 'Back In Black — AC/DC' and
   'Back In Black — AC, DC' side by side: two identical-looking options, one
   scored right. Nothing here edits tags; `artist_key` gives a grouping key so
   the variants count as one artist. See scripts/retag_artists.py for the
   on-disk fix that also corrects the displayed text.

2. **True duplicate rows.** The same recording on three compilations
   ('Alone — Heart 218s'). Redundant: one is enough, the rest waste a clip and
   let a song come up twice in a game.

3. **Rows whose tags cannot be trusted at all.** A tagger that lost the part
   that told two tracks apart: 17 rows titled 'Fear and Loathing' on ONE
   'Electra Heart (Extended)' album spanning 154-367s, and '[Unknown Artist]'
   rows with no duration. These are DIFFERENT songs wearing one name, so the
   title is a lie and the answer would be unmarkable.

The duplicate rule is why 2 and 3 are separate. Keying on title+artist alone
would delete real music: *The Metallica Blacklist* has 12 different covers of
'Nothing Else Matters', each a distinct artist under `album_artist=Metallica`,
and Phoebe Bridgers' has 138k listeners. Only title+artist+EXACT duration is a
duplicate (784 rows here); same title+artist with a spread of durations is the
opposite finding — evidence the tags are junk (47 rows).

Everything destructive here BANS rather than deletes: a resync re-inserts by
Navidrome id, so a deleted row comes straight back, and `banned` is the pool's
existing "never pick this" flag.
"""
import logging
import re
import unicodedata
from collections import defaultdict

LOGGER = logging.getLogger(__name__)

# A clip needs a 20s intro plus a 12s payoff from a LATER part of the song. Below
# roughly that, the "intro" is the whole track and the payoff overlaps it — the
# clip gives the answer away, or there's simply nothing to cut. Skits, stings and
# hidden-track fragments live down here (65 tracks in this library, shortest 8s).
MIN_DURATION_S = 32

# Rows sharing album_id + artist + title this many times over have lost whatever
# distinguished them. Two is normal and legitimate (a compilation carrying both a
# studio and a live take, a bonus-disc reprise), so the floor is 3.
JUNK_GROUP_MIN = 3

_ARTICLE = re.compile(r"^(the|a|an)\s+", re.I)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def artist_key(artist: str | None) -> str:
    """Fold spelling variants of one artist onto a single key.

    Punctuation, case, spacing, a leading article and '&' vs 'and' are the whole
    variant vocabulary in a real tag set: 'AC/DC' / 'AC, DC' / 'AC-DC' / 'ACDC',
    'The Black Eyed Peas' / 'Black Eyed Peas', 'Mumford & Sons' / 'Mumford And
    Sons', 'Mark Ronson' / 'Mark  Ronson', 'coldplay'.

    Deliberately NOT a fuzzy match. This is exact after normalisation, so it can
    never merge two genuinely different acts — the failure mode that matters,
    since a wrong merge silently makes one artist's songs answer as another's.
    """
    a = unicodedata.normalize("NFKD", artist or "")
    a = a.lower().replace("&", " and ")
    a = _ARTICLE.sub("", a.strip())
    return _NON_ALNUM.sub("", a)


def artist_variants(conn) -> list[dict]:
    """Groups of >1 spelling of one artist, most-used spelling first.

    Read-only. Returns [{key, primary, spellings: [{artist, tracks}], tracks}].
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in conn.execute(
            "SELECT artist, COUNT(*) n FROM tracks WHERE active=1 AND artist IS NOT NULL "
            "GROUP BY artist"):
        groups[artist_key(r["artist"])].append({"artist": r["artist"], "tracks": r["n"]})
    out = []
    for key, spellings in groups.items():
        if len(spellings) < 2:
            continue
        # most tracks wins; ties break on the longer string, which keeps the
        # punctuated form ('AC/DC' over 'ACDC') rather than picking at random
        spellings.sort(key=lambda s: (-s["tracks"], -len(s["artist"])))
        out.append({"key": key, "primary": spellings[0]["artist"], "spellings": spellings,
                    "tracks": sum(s["tracks"] for s in spellings)})
    out.sort(key=lambda g: -g["tracks"])
    return out


def find_duplicates(conn) -> list[dict]:
    """Redundant rows: same title+artist+EXACT duration, keep the best-scored one.

    Artist is compared on `artist_key`, so a duplicate that also carries a
    variant spelling is caught. Duration must match exactly and be non-NULL —
    the whole point of the rule (see the module docstring).

    Returns the rows to ban: [{id, title, artist, album, duration, keeping}].
    """
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in conn.execute(
            "SELECT id, title, artist, album, duration, global_listeners, clipped_at "
            "FROM tracks WHERE active=1 AND banned=0 AND duration IS NOT NULL"):
        k = (r["title"].strip().lower(), artist_key(r["artist"]), r["duration"])
        groups[k].append(dict(r))
    losers = []
    for rows in groups.values():
        if len(rows) < 2:
            continue
        # keep whichever row the pipeline has already invested in or knows best:
        # an existing clip first (no wasted work), then the Last.fm score
        rows.sort(key=lambda r: (r["clipped_at"] is None, -(r["global_listeners"] or 0), r["id"]))
        keep = rows[0]
        for r in rows[1:]:
            losers.append({**r, "keeping": keep["id"]})
    return losers


def find_untrustworthy(conn) -> list[dict]:
    """Rows in album+artist+title groups of JUNK_GROUP_MIN or more.

    Every row in the group goes — unlike duplicates there is no "best" one to
    keep, because we can't tell which row is which song. Also catches the
    duration-less '[Unknown Artist]' rows, which group by title alone.
    """
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in conn.execute(
            "SELECT id, title, artist, album, album_id, duration FROM tracks "
            "WHERE active=1 AND banned=0 AND album_id IS NOT NULL"):
        groups[(r["album_id"], artist_key(r["artist"]), r["title"].strip().lower())].append(dict(r))
    out = []
    for rows in groups.values():
        if len(rows) >= JUNK_GROUP_MIN:
            out.extend(rows)
    return out


def find_too_short(conn) -> list[dict]:
    """Tracks too short to cut a non-spoiling clip from (see MIN_DURATION_S)."""
    return [dict(r) for r in conn.execute(
        "SELECT id, title, artist, album, duration FROM tracks "
        "WHERE active=1 AND banned=0 AND duration IS NOT NULL AND duration < ?",
        (MIN_DURATION_S,))]


# ORDER IS SIGNIFICANT — clean() runs these in sequence against live state, so
# each sees the previous one's bans. 'duplicate' MUST come before 'untrustworthy':
# deduping first can drop a group below JUNK_GROUP_MIN and rightly spare it, while
# the reverse order bans the whole group and loses a real song. See clean().
FINDERS = {
    "duplicate": find_duplicates,
    "untrustworthy": find_untrustworthy,
    "too_short": find_too_short,
}


def audit(conn) -> dict:
    """What a clean-up WOULD do. Read-only — the dry run for `clean`."""
    out: dict = {"variants": len(artist_variants(conn))}
    for reason, finder in FINDERS.items():
        out[reason] = len(finder(conn))
    return out


def clean(conn, reasons: list[str] | None = None, dry_run: bool = False) -> dict:
    """Ban the rows the finders flag. Bans, never deletes (module docstring).

    `ban_reason` records which finder caught a row, so a mistake is reversible:
    UPDATE tracks SET banned=0, ban_reason=NULL WHERE ban_reason='duplicate'.

    Finders run IN ORDER against live state, each seeing the previous one's bans,
    and that order is deliberate. Deduping first can legitimately dissolve an
    'untrustworthy' group: three rows of AC/DC's 'Round And Round' on one album
    (201s, 201s, 200s) look like tag damage, but once the exact-duration duplicate
    is banned only two rows remain — below JUNK_GROUP_MIN, and a 200s/201s pair is
    a normal near-identical rip, not a tagger that lost the distinguishing part of
    the title. Running untrustworthy first would have banned all three, losing a
    real song. So counts here can come out lower than a standalone audit(), which
    scores each finder independently against the untouched table.
    """
    picked = reasons or list(FINDERS)
    out: dict = {"dry_run": dry_run, "banned": {}, "examples": {}}
    for reason in picked:
        if reason not in FINDERS:
            raise ValueError(f"unknown clean-up: {reason} (have {sorted(FINDERS)})")
        rows = FINDERS[reason](conn)
        out["banned"][reason] = len(rows)
        out["examples"][reason] = [
            f'{r["artist"]} — {r["title"]} ({r.get("duration")}s)' for r in rows[:5]]
        if rows and not dry_run:
            conn.executemany("UPDATE tracks SET banned=1, ban_reason=? WHERE id=?",
                             [(reason, r["id"]) for r in rows])
    if not dry_run:
        conn.commit()
    out["total"] = sum(out["banned"].values())
    LOGGER.info("library clean%s: %s", " (dry run)" if dry_run else "", out["banned"])
    return out
