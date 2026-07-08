"""Half-time trivia bank: read-aloud facts + true/false questions.

A curated seed pack ships with the app (Irish/UK-leaning, family-friendly);
the true/false pool tops itself up from Open Trivia DB (free, no key) when
it runs low. Picks prefer never-used items and recycle the oldest once the
bank is exhausted, so repeats take months to come around.
"""
import html
import json
import logging
import os
from datetime import datetime, timezone

import httpx

LOGGER = logging.getLogger(__name__)
SEED_PATH = os.path.join(os.path.dirname(__file__), "data", "trivia_seed.json")
OPENTDB_URL = "https://opentdb.com/api.php?amount=50&category=12&type=boolean"
TOPUP_MIN_UNUSED = 30  # fetch more T/F once the fresh pool dips below this


def ensure_seeded(conn) -> int:
    """Idempotent — text is UNIQUE, so re-running only inserts new items."""
    with open(SEED_PATH, encoding="utf-8") as f:
        seed = json.load(f)
    added = 0
    for item in seed:
        cur = conn.execute(
            "INSERT OR IGNORE INTO trivia(kind, text, answer, source) VALUES(?,?,?,'seed')",
            (item["kind"], item["text"], item.get("answer")))
        added += cur.rowcount
    conn.commit()
    return added


def pick(conn, kind: str, n: int) -> list[dict]:
    """n items: fresh ones first (random), then oldest-used recycled."""
    rows = conn.execute(
        "SELECT * FROM trivia WHERE kind=? "
        "ORDER BY used_at IS NOT NULL, used_at, RANDOM() LIMIT ?", (kind, n)).fetchall()
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        conn.execute("UPDATE trivia SET used_at=? WHERE id=?", (now, r["id"]))
    conn.commit()
    return [dict(r) for r in rows]


def topup_tf(conn) -> dict:
    """Refill the true/false pool from Open Trivia DB when it runs low."""
    unused = conn.execute(
        "SELECT COUNT(*) c FROM trivia WHERE kind='tf' AND used_at IS NULL").fetchone()["c"]
    if unused >= TOPUP_MIN_UNUSED:
        return {"fetched": 0, "unused": unused}
    r = httpx.get(OPENTDB_URL, timeout=20)
    r.raise_for_status()
    added = 0
    for q in r.json().get("results", []):
        text = html.unescape(q.get("question", "")).strip()
        if not text or q.get("correct_answer") not in ("True", "False"):
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO trivia(kind, text, answer, source) VALUES('tf',?,?,'opentdb')",
            (text, 1 if q["correct_answer"] == "True" else 0))
        added += cur.rowcount
    conn.commit()
    LOGGER.info("trivia topup: %d new T/F from opentdb (pool was %d)", added, unused)
    return {"fetched": added, "unused": unused + added}
