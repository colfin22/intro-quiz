from fastapi import FastAPI

from . import __version__, db, subsonic, sync

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
