import asyncio
import logging
import os

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, board_cast, clips, config, db, game, ha, lastfm, scoring, subsonic, sync

LOGGER = logging.getLogger(__name__)
app = FastAPI(title="Intro Quiz", version=__version__)
BASE = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
if os.path.isdir(config.CLIPS_DIR):
    app.mount("/clips", StaticFiles(directory=config.CLIPS_DIR), name="clips")


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


@app.post("/api/clips/cut")
def api_clips_cut(limit: int = 50):
    conn = db.connect()
    try:
        return clips.cut_batch(conn, subsonic.Client(), limit=limit)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Game: one live game, all clients drive it over one websocket protocol.
# ---------------------------------------------------------------------------

class Hub:
    def __init__(self):
        self.game: game.Game | None = None
        self.sockets: list[WebSocket] = []
        self.boards: set = set()
        self.display: str | None = (board_cast.display_names() or [None])[0]
        self.lock = asyncio.Lock()
        self.deadline_task: asyncio.Task | None = None

    async def broadcast(self):
        snap = self.game.snapshot() if self.game else {"phase": "idle"}
        snap["type"] = "state"
        snap["displays"] = board_cast.display_names()
        snap["display"] = self.display or "none"
        dead = []
        for ws in self.sockets:
            try:
                await ws.send_json(snap)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.sockets.remove(ws)

    def cancel_deadline(self):
        if self.deadline_task and not self.deadline_task.done():
            self.deadline_task.cancel()

    def board_live(self) -> bool:
        return any(b in self.sockets for b in self.boards)

    async def start_round(self):
        rnd = self.game.start_round()
        if not self.board_live():  # board plays its own audio when present
            asyncio.get_event_loop().run_in_executor(None, ha.play_clip, rnd["track"]["id"], str(rnd["clip_len"]))
        self.cancel_deadline()
        self.deadline_task = asyncio.create_task(self._deadline(game.ANSWER_WINDOW_S))
        await self.broadcast()

    async def _deadline(self, seconds: float):
        await asyncio.sleep(seconds)
        async with self.lock:
            if self.game and self.game.phase == "question":
                await self._reveal()

    async def _reveal(self):
        rnd = self.game.reveal()
        if not self.board_live():
            asyncio.get_event_loop().run_in_executor(None, ha.play_clip, rnd["track"]["id"], "payoff")
        await self.broadcast()

    async def maybe_early_reveal(self):
        if self.game.phase == "question" and self.game.all_answered():
            self.cancel_deadline()
            await self._reveal()


hub = Hub()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    hub.sockets.append(ws)
    name = None
    try:
        snap = hub.game.snapshot() if hub.game else {"phase": "idle"}
        await ws.send_json({**snap, "type": "state",
                            "displays": board_cast.display_names(),
                            "display": hub.display or "none"})
        while True:
            msg = await ws.receive_json()
            async with hub.lock:
                try:
                    kind = msg.get("type")
                    if kind == "set_display":
                        want = msg.get("display")
                        if want in (board_cast.display_names() + ["none"]):
                            hub.display = None if want == "none" else want
                        await hub.broadcast()
                    elif kind == "board_hello":
                        hub.boards.add(ws)
                    elif kind == "new_game":
                        if hub.game and hub.game.phase not in ("finished", "lobby"):
                            raise game.GameError("a game is already running")
                        if ha.house_is_sleeping() and not msg.get("force"):
                            raise game.GameError("house is Sleeping — start from the board to override")
                        conn = db.connect()
                        try:
                            hub.game = game.Game(conn, rounds=int(msg.get("rounds", 10)),
                                                 tiers=msg.get("tiers") or ["easy", "medium"])
                        finally:
                            conn.close()
                        asyncio.get_event_loop().run_in_executor(None, board_cast.show_board, hub.display)
                        await hub.broadcast()
                    elif kind == "join":
                        if not hub.game:
                            raise game.GameError("no game — start one first")
                        name = msg.get("name", "")
                        hub.game.join(name)
                        await hub.broadcast()
                    elif kind == "start_round":
                        await hub.start_round()
                    elif kind == "extend_clip":
                        length = hub.game.extend_clip()
                        if not hub.board_live():
                            asyncio.get_event_loop().run_in_executor(
                                None, ha.play_clip, hub.game.rounds[hub.game.current]["track"]["id"], str(length))
                        await hub.broadcast()
                    elif kind == "answer":
                        hub.game.answer(name or msg.get("name", ""), int(msg["choice"]))
                        await hub.maybe_early_reveal()
                        await hub.broadcast()
                    elif kind == "flag_clip":
                        conn = db.connect()
                        try:
                            hub.game.flag_current(conn)
                        finally:
                            conn.close()
                        await hub.broadcast()
                    elif kind == "abort":
                        hub.cancel_deadline()
                        hub.game = None
                        asyncio.get_event_loop().run_in_executor(None, board_cast.hide_board, hub.display)
                        await hub.broadcast()
                    elif kind == "next":
                        if hub.game.phase == "reveal" and hub.game.is_last_round():
                            conn = db.connect()
                            try:
                                hub.game.finish(conn)
                            finally:
                                conn.close()
                            loop = asyncio.get_event_loop()
                            loop.call_later(60, lambda: loop.run_in_executor(None, board_cast.hide_board, hub.display))
                            await hub.broadcast()
                        else:
                            await hub.start_round()
                except game.GameError as e:
                    await ws.send_json({"type": "error", "message": str(e)})
    except WebSocketDisconnect:
        pass
    finally:
        if ws in hub.sockets:
            hub.sockets.remove(ws)


@app.get("/")
def page_index():
    return FileResponse(os.path.join(BASE, "static", "index.html"))


@app.get("/board")
def page_board():
    return FileResponse(os.path.join(BASE, "static", "board.html"))


@app.get("/api/round/audio")
def api_round_audio(kind: str = "5"):
    """Current round's clip without exposing the track id (board audio).

    Intro kinds only work during the question phase; payoff during reveal —
    so a phone probing this endpoint learns nothing it can't already hear.
    """
    if not hub.game or hub.game.current < 0:
        return Response(status_code=404)
    rnd = hub.game.rounds[hub.game.current]
    if kind == "payoff":
        if hub.game.phase not in ("reveal", "finished"):
            return Response(status_code=404)
    elif kind not in ("5", "10", "20") or hub.game.phase != "question":
        return Response(status_code=404)
    path = os.path.join(config.CLIPS_DIR, rnd["track"]["id"], f"{kind}.mp3")
    if not os.path.exists(path):
        return Response(status_code=404)
    return FileResponse(path, media_type="audio/mpeg",
                        headers={"Cache-Control": "no-store"})


@app.post("/api/ban/album")
def api_ban_album(pattern: str):
    """Ban every track whose album matches the SQL LIKE pattern (case-insensitive)."""
    conn = db.connect()
    try:
        n = conn.execute("UPDATE tracks SET banned=1 WHERE album LIKE ? AND banned=0",
                         (pattern,)).rowcount
        conn.commit()
        return {"banned": n, "pattern": pattern}
    finally:
        conn.close()


@app.get("/api/leaderboard")
def api_leaderboard():
    conn = db.connect()
    try:
        return game.all_time_leaderboard(conn)
    finally:
        conn.close()


@app.get("/api/art/{track_id}")
def api_art(track_id: str):
    conn = db.connect()
    try:
        row = conn.execute("SELECT cover_art FROM tracks WHERE id=?", (track_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row["cover_art"]:
        return Response(status_code=404)
    r = httpx.get(f"{config.NAVIDROME_URL}/rest/getCoverArt.view",
                  params={**subsonic.auth_params(), "id": row["cover_art"], "size": 500},
                  timeout=15)
    return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": "max-age=86400"})
