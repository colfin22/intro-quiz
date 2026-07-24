"""Half-time trivia bank: read-aloud facts + true/false questions.

A curated seed pack ships with the app (Irish/UK-leaning, family-friendly);
drop your own pack at data/trivia_custom.json (same format) to add local
flavour, and set TRIVIA_BUILTIN_PACK=false to skip the shipped one entirely.
The true/false pool tops itself up from Open Trivia DB (free, no key) when
it runs low. Picks prefer never-used items and recycle the oldest once the
bank is exhausted, so repeats take months to come around.
"""
import html
import json
import logging
import os
from datetime import datetime, timezone

import httpx

from . import config

LOGGER = logging.getLogger(__name__)
SEED_PATH = os.path.join(os.path.dirname(__file__), "data", "trivia_seed.json")
OPENTDB_URL = "https://opentdb.com/api.php?amount=50&category=12&type=boolean"
TOPUP_MIN_UNUSED = 30  # fetch more T/F once the fresh pool dips below this


def custom_pack_path() -> str:
    """User-supplied pack lives beside the DB, on the data volume —
    it survives image rebuilds and never conflicts with a git pull."""
    return os.path.join(os.path.dirname(config.DB_PATH), "trivia_custom.json")


def insert_items(conn, pack: list, source: str, origin: str = "pack") -> dict:
    """Validate + insert a list of pack items. Idempotent — text is UNIQUE, so
    re-running only inserts new items; duplicates are counted, not errors.
    Per-item problems skip that item (with a reason), never the whole batch."""
    added, duplicates, rejects = 0, 0, []
    for item in pack:
        if not isinstance(item, dict):
            rejects.append(f"not an object: {str(item)[:60]}")
            continue
        kind = item.get("kind")
        if kind not in ("fact", "tf") or not item.get("text"):
            LOGGER.warning("trivia %s: skipping malformed item %r", origin, item)
            rejects.append(f"malformed (needs kind fact/tf + text): {str(item)[:60]}")
            continue
        if kind == "tf" and item.get("answer") not in (0, 1, True, False):
            LOGGER.warning("trivia %s: T/F item without a 0/1 answer skipped: %r",
                           origin, item.get("text"))
            rejects.append(f"T/F without a 0/1 answer: {str(item.get('text'))[:60]}")
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO trivia(kind, text, answer, source) VALUES(?,?,?,?)",
            (kind, item["text"],
             int(item["answer"]) if kind == "tf" else None, source))
        if cur.rowcount:
            added += 1
        else:
            duplicates += 1
    conn.commit()
    return {"added": added, "duplicates": duplicates, "rejects": rejects}


def _seed_pack(conn, path: str, source: str) -> int:
    """A missing/broken pack must never block a game — half time just
    degrades to a plain snacks break."""
    try:
        with open(path, encoding="utf-8") as f:
            pack = json.load(f)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as e:
        LOGGER.error("trivia pack %s unusable (%s) — skipped", path, e)
        return 0
    return insert_items(conn, pack, source, origin=path)["added"]


LLM_PROMPT = """Write me a music trivia pack for a family quiz night, as a single JSON
list and nothing else. Two kinds of item:
  {"kind": "fact", "text": "..."}                 — a fun music fact one
    player reads aloud to the table
  {"kind": "tf", "text": "...", "answer": 1 or 0} — a true/false question
    (1 = true, 0 = false)

Produce {facts} facts and {tf} true/false questions about popular music from
{region}, plus internationally famous acts as heard from there. Rules:
- Only well-established, easily verifiable claims — no obscure trivia,
  no disputed stories. If a story is folklore, start it with "Legend has
  it" or "As the story goes".
- Facts must read naturally when spoken aloud, one or two sentences.
- False T/F statements must be plainly false, not technicalities.
- Make roughly 40% of the T/F items false, and don't cluster them.
- Family-friendly; span the 1960s to today; vary the artists — no more
  than three items about any one act.
- Output raw JSON only: no markdown fences, no commentary."""


def llm_prompt(region: str, facts: int = 60, tf: int = 80) -> str:
    """The docs/trivia-pack.md prompt with the blanks filled in."""
    # str.format would choke on the JSON braces in the template — plain replace
    return (LLM_PROMPT
            .replace("{region}", region.strip() or "<YOUR REGION/COUNTRY>")
            .replace("{facts}", str(max(1, min(500, facts))))
            .replace("{tf}", str(max(1, min(500, tf)))))


def parse_pack(text: str) -> list:
    """Pull the JSON array out of an LLM response — tolerates code fences and
    surrounding prose. Raises ValueError when no JSON list can be found."""
    text = text.strip()
    if not text:
        raise ValueError("nothing pasted")
    start = text.find("[")
    if start < 0:
        raise ValueError("no JSON list found — expected a [ ... ] array")
    try:
        pack, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError as e:
        raise ValueError(f"could not parse the JSON list: {e}") from e
    if not isinstance(pack, list):
        raise ValueError("parsed JSON is not a list")
    return pack


def ensure_seeded(conn) -> int:
    added = 0
    if config.TRIVIA_BUILTIN_PACK:
        if not os.path.exists(SEED_PATH):  # custom pack missing is normal; this isn't
            LOGGER.error("built-in trivia pack missing at %s — half time may be trivia-less", SEED_PATH)
        added += _seed_pack(conn, SEED_PATH, "seed")
    else:
        LOGGER.info("built-in trivia pack disabled (TRIVIA_BUILTIN_PACK=false)")
    added += _seed_pack(conn, custom_pack_path(), "custom")
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
