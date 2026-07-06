"""Family-score ingest + difficulty tier assignment."""

# Tier thresholds. Family play data is sparse (one Navidrome user scrobbles),
# so any repeat listening is a strong "the house knows this" signal, and
# Last.fm listeners carry the rest of the tiering.
FAMILY_KNOWN_PLAYS = 2
GLOBAL_WELL_KNOWN = 200_000   # listeners
GLOBAL_KNOWN = 30_000


def ingest_annotations(conn, rows: list[dict]) -> dict:
    """rows: [{id, play_count, starred}] aggregated across all Navidrome users."""
    conn.execute("UPDATE tracks SET play_count=0, starred=0")
    matched = 0
    for r in rows:
        matched += conn.execute(
            "UPDATE tracks SET play_count=?, starred=? WHERE id=?",
            (int(r.get("play_count") or 0), int(r.get("starred") or 0), r["id"])).rowcount
    conn.commit()
    return {"received": len(rows), "matched": matched}


def assign_tiers(conn) -> dict:
    """easy = family knows it; medium = world knows it; hard = plausible deep cut;
    tiebreak = the rest. Only active tracks with a global score are tiered."""
    conn.execute("UPDATE tracks SET tier=NULL")
    conn.execute(
        "UPDATE tracks SET tier='easy' WHERE active=1 AND (play_count>=? OR starred>0)",
        (FAMILY_KNOWN_PLAYS,))
    conn.execute(
        "UPDATE tracks SET tier='medium' WHERE active=1 AND tier IS NULL "
        "AND global_listeners>=?", (GLOBAL_WELL_KNOWN,))
    conn.execute(
        "UPDATE tracks SET tier='hard' WHERE active=1 AND tier IS NULL "
        "AND global_listeners>=?", (GLOBAL_KNOWN,))
    conn.execute(
        "UPDATE tracks SET tier='tiebreak' WHERE active=1 AND tier IS NULL "
        "AND global_listeners IS NOT NULL AND global_listeners>0")
    conn.commit()
    counts = {r["tier"]: r["c"] for r in conn.execute(
        "SELECT tier, COUNT(*) c FROM tracks WHERE tier IS NOT NULL GROUP BY tier")}
    return counts
