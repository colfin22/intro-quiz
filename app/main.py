from fastapi import FastAPI

from pydantic import BaseModel

from . import __version__, db, lastfm, scoring, subsonic, sync

app = FastAPI(title="Intro Quiz", version=__version__)


@app.get("/health")
def health():
    return {"ok": True, "version": __version__}


@app.post("/api/sync")
def api_sync():
    conn = db.connect()
    try:
        client = subsonic.Client()
        client.ping()
        return sync.sync_library(conn, client)
    finally:
        conn.close()


@app.get("/api/stats")
def api_stats():
    conn = db.connect()
    try:
        return {
            "tracks_active": conn.execute("SELECT COUNT(*) c FROM tracks WHERE active=1").fetchone()["c"],
            "with_family_score": conn.execute("SELECT COUNT(*) c FROM tracks WHERE play_count>0").fetchone()["c"],
            "with_global_score": conn.execute("SELECT COUNT(*) c FROM tracks WHERE global_listeners IS NOT NULL").fetchone()["c"],
            "tiered": conn.execute("SELECT tier, COUNT(*) c FROM tracks WHERE tier IS NOT NULL GROUP BY tier").fetchall(),
        }
    finally:
        conn.close()


class AnnotationRow(BaseModel):
    id: str
    play_count: int = 0
    starred: int = 0


@app.post("/api/ingest/annotations")
def api_ingest_annotations(rows: list[AnnotationRow]):
    conn = db.connect()
    try:
        return scoring.ingest_annotations(conn, [r.model_dump() for r in rows])
    finally:
        conn.close()


@app.post("/api/score/lastfm")
def api_score_lastfm(limit: int = 200):
    conn = db.connect()
    try:
        return lastfm.score_batch(conn, limit=limit)
    finally:
        conn.close()


@app.post("/api/score/tiers")
def api_score_tiers():
    conn = db.connect()
    try:
        return scoring.assign_tiers(conn)
    finally:
        conn.close()
