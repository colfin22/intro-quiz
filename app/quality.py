"""Match-quality check: find tracks whose Last.fm lookup probably failed.

The signal is outcome-based, not a junk-string blocklist: a track scoring
almost no listeners while other tracks by the SAME artist are genuinely
popular is nearly always a mangled title (tagger junk in the subtitle that
the server appends to titles, typos, encoding damage) — not an obscure
song. This catches tagger signatures we've never seen before.

Real-world example that motivated this: a compilation rip put "PMEDIA" in
the subtitle tag of every file, Navidrome appended it to the Subsonic
titles, and "Teenage Kicks (PMEDIA)" scored 0 listeners.
"""
import logging
import os
from datetime import datetime, timezone

from . import ha

LOGGER = logging.getLogger(__name__)

# a suspect scores at or below the floor while its artist's best track
# clears the ceiling — both tunable for unusual libraries
LISTENER_FLOOR = int(os.environ.get("QUALITY_LISTENER_FLOOR", "25"))
ARTIST_CEILING = int(os.environ.get("QUALITY_ARTIST_CEILING", "50000"))
NOTIFY_MAX_LINES = 8


def find_suspects(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT t.id, t.title, t.artist, t.album, t.global_listeners,
                  a.top artist_top, t.quality_notified_at
           FROM tracks t
           JOIN (SELECT artist, MAX(global_listeners) top FROM tracks
                 WHERE active=1 AND banned=0 GROUP BY artist) a
             ON a.artist = t.artist
           WHERE t.active=1 AND t.banned=0
             AND t.global_listeners IS NOT NULL AND t.global_listeners <= ?
             AND a.top >= ?
           ORDER BY a.top DESC, t.artist, t.title""",
        (LISTENER_FLOOR, ARTIST_CEILING))]


def check(conn, push: bool = True) -> dict:
    """Find suspects, push the not-yet-notified ones via HA, mark them.

    push=False baselines: marks current suspects as seen without a
    notification (useful right after install, so only FUTURE misses alert).
    """
    suspects = find_suspects(conn)
    fresh = [s for s in suspects if not s["quality_notified_at"]]
    pushed = False
    if fresh:
        now = datetime.now(timezone.utc).isoformat()
        if push:
            lines = [f'{s["artist"]} — {s["title"]}' for s in fresh[:NOTIFY_MAX_LINES]]
            if len(fresh) > NOTIFY_MAX_LINES:
                lines.append(f"…and {len(fresh) - NOTIFY_MAX_LINES} more")
            pushed = ha.notify(
                f"Intro Quiz: {len(fresh)} track(s) look mis-tagged",
                "Popular artist but ~zero Last.fm listeners — the titles "
                "probably contain junk:\n" + "\n".join(lines))
        conn.executemany(
            "UPDATE tracks SET quality_notified_at=? WHERE id=?",
            [(now, s["id"]) for s in fresh])
        conn.commit()
    LOGGER.info("quality check: %d suspects, %d fresh, pushed=%s",
                len(suspects), len(fresh), pushed)
    return {"suspects": len(suspects), "fresh": len(fresh), "pushed": pushed}
