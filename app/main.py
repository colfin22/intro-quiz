import asyncio
import io
import json
import logging
import os
import shutil
import time

import httpx
import segno
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, board_cast, clips, config, db, game, ha, jobs, lastfm, quality, scoring, subsonic, sync, trivia

LOGGER = logging.getLogger(__name__)
TRIVIA_SETTING = "trivia_enabled"  # settings-table key for the half-time toggle (#60)
DIFFICULTY_SETTING = "difficulty"  # settings-table key for the difficulty picker (#58)
app = FastAPI(title="Intro Quiz", version=__version__)


def require_admin(x_admin_token: str = Header(default="")):
    """Maintenance endpoints honour ADMIN_PASSWORD when set; open when unset."""
    if not jobs.check_token(x_admin_token):
        raise HTTPException(status_code=401, detail="admin token required")


ADMIN = [Depends(require_admin)]


@app.middleware("http")
async def no_cache(request, call_next):
    resp = await call_next(request)
    # phones cached old JS across our deploys and played half-fixed games
    if request.url.path == "/" or request.url.path in ("/board",) or request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp
BASE = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
if os.path.isdir(config.CLIPS_DIR):
    app.mount("/clips", StaticFiles(directory=config.CLIPS_DIR), name="clips")


@app.get("/health")
def health():
    """Liveness AND readiness: is the app actually able to run a game yet?"""
    out = {"ok": True, "version": __version__}
    try:
        conn = db.connect()
        try:
            out["tracks_synced"] = conn.execute(
                "SELECT COUNT(*) c FROM tracks WHERE active=1").fetchone()["c"]
            out["tracks_tiered"] = conn.execute(
                "SELECT COUNT(*) c FROM tracks WHERE active=1 AND tier IS NOT NULL").fetchone()["c"]
            playable = conn.execute(
                f"SELECT COUNT(*) c FROM tracks WHERE {game.QUIZZABLE} "
                "AND tier IN ('easy','medium')").fetchone()["c"]
            out["tracks_playable"] = playable
            out["ready_to_play"] = playable >= 10
            if not out["ready_to_play"]:
                out["message"] = ("not enough clipped easy/medium tracks yet — run "
                                  "POST /api/sync, /api/score/lastfm (repeat), /api/score/tiers, "
                                  "then cut clips (CLIP_SWEEP_ON_START=true or /api/clips/cut)")
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — health must never 500
        out["ready_to_play"] = False
        out["message"] = f"db not readable: {e}"
    compat = jobs.bootstrap_compat()
    if compat:
        out["bootstrap"] = compat
    return out


@app.on_event("startup")
async def board_watchdog():
    """Reap stale games so an abandoned lobby/game doesn't haunt the TV (#26).

    This used to ALSO re-cast a board it judged "dead" (#21/#25) — but that was a
    DashCast-era mechanism, and every crash it "recovered" turned out to be a
    healthy board it killed on a websocket blip or a between-games reload (#47).
    Our own Cast receiver persists and doesn't rot, so the recast is gone. What
    remains here is only the stale-game reaper.
    """
    import time as _t

    async def _loop():
        while True:
            await asyncio.sleep(5)
            try:
                if not (hub.game and hub.game.phase in ("lobby", "question", "reveal", "break")):
                    continue
                # stale games expire — an abandoned lobby/game must not haunt the TV (#26)
                idle = _t.time() - hub.last_activity
                empty_lobby = hub.game.phase == "lobby" and not hub.game.players
                if (empty_lobby and idle > 600) or idle > 2700:
                    async with hub.lock:
                        if hub.game:  # re-check under the lock
                            LOGGER.warning("stale game (%s, idle %.0f min) — auto-abandoning",
                                           hub.game.phase, idle / 60)
                            hub.cancel_deadline()
                            hub.game = None
                            asyncio.get_event_loop().run_in_executor(None, board_cast.hide_board, hub.display)
                            await hub.broadcast()
            except Exception:  # noqa: BLE001 — the reaper must never die
                LOGGER.exception("board watchdog tick failed")
    asyncio.create_task(_loop())


@app.on_event("startup")
async def maybe_clip_sweep():
    """Startup checks + CLIP_SWEEP_ON_START background clip-cutting session."""
    conn = db.connect()
    try:  # unset = on, so an existing install is unchanged (#60)
        hub.trivia = db.get_setting(conn, TRIVIA_SETTING) != "0"
        # unset = "normal", i.e. the tiers the game always used before #58
        hub.difficulty = db.get_setting(conn, DIFFICULTY_SETTING) or game.DEFAULT_DIFFICULTY
    finally:
        conn.close()
    if board_cast.BOARD_URL and not board_cast.BOARD_URL.startswith("https://"):
        LOGGER.warning("=" * 60)
        LOGGER.warning("BOARD_URL is not https:// — cast devices REFUSE plain-http "
                       "pages, so the board will silently fail to appear on the TV. "
                       "Put the app behind a reverse proxy with TLS and set "
                       "BOARD_URL=https://... (current: %s)", board_cast.BOARD_URL)
        LOGGER.warning("=" * 60)
    if config.CLIP_SWEEP_ON_START:
        cap = f" (capped at {config.CLIP_SWEEP_MAX_HOURS}h)" if config.CLIP_SWEEP_MAX_HOURS else ""
        LOGGER.info("CLIP_SWEEP_ON_START set — bulk clip session starting in the background%s", cap)
        # through the registry so /admin sees it (and it can't collide with a manual run)
        jobs.start("clips")


@app.post("/api/sync", dependencies=ADMIN)
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


@app.post("/api/ingest/annotations", dependencies=ADMIN)
def api_ingest_annotations(rows: list[AnnotationRow]):
    conn = db.connect()
    try:
        return scoring.ingest_annotations(conn, [r.model_dump() for r in rows])
    finally:
        conn.close()


@app.post("/api/score/lastfm", dependencies=ADMIN)
def api_score_lastfm(limit: int = 200):
    conn = db.connect()
    try:
        return lastfm.score_batch(conn, limit=limit)
    finally:
        conn.close()


@app.post("/api/score/tiers", dependencies=ADMIN)
def api_score_tiers():
    conn = db.connect()
    try:
        return scoring.assign_tiers(conn)
    finally:
        conn.close()


@app.post("/api/clips/cut", dependencies=ADMIN)
def api_clips_cut(limit: int = 50):
    conn = db.connect()
    try:
        return clips.cut_batch(conn, subsonic.Client(), limit=limit)
    finally:
        conn.close()


@app.post("/api/clips/recut", dependencies=ADMIN)
def api_clips_recut(track_id: str = "", q: str = ""):
    """Queue tracks for re-cutting (e.g. after the silence-aware cutter landed).

    Clears clipped_at + deletes the clip files; the sweep/nightly re-cuts them
    with silence detection. Target one track_id, or q= a LIKE pattern matched
    against artist and title (e.g. q=%Sunn O%%%).
    """
    if not track_id and not q:
        return Response(status_code=400, content="give track_id= or q= (SQL LIKE pattern)")
    conn = db.connect()
    try:
        if track_id:
            rows = conn.execute("SELECT id FROM tracks WHERE id=?", (track_id,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM tracks WHERE clipped_at IS NOT NULL AND (artist LIKE ? OR title LIKE ?)",
                (q, q)).fetchall()
        for r in rows:
            conn.execute("UPDATE tracks SET clipped_at=NULL WHERE id=?", (r["id"],))
            shutil.rmtree(os.path.join(config.CLIPS_DIR, r["id"]), ignore_errors=True)
        conn.commit()
        return {"queued_for_recut": len(rows)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Maintenance jobs (#52): every action the admin page can run, registered with
# the one-at-a-time job registry. The runners reuse the same module functions
# the plain endpoints call.
# ---------------------------------------------------------------------------

def _job_sync(set_stage) -> dict:
    conn = db.connect()
    try:
        client = subsonic.Client()
        client.ping()
        return sync.sync_library(conn, client)
    finally:
        conn.close()


def _job_lastfm(set_stage) -> dict:
    """Loop score_batch until the backlog is dry (a single batch is 200 tracks)."""
    conn = db.connect()
    try:
        total = {"scored": 0, "errors": 0, "remaining": 0}
        for _ in range(2000):  # safety bound, ~400k tracks
            r = lastfm.score_batch(conn, limit=200)
            total["scored"] += r["scored"]
            total["errors"] += r["errors"]
            total["remaining"] = r["remaining"]
            set_stage(f"scoring — {r['remaining']} remaining")
            if r["remaining"] == 0:
                break
            if r["scored"] == 0:
                # no progress (key revoked / network down) is a warning, not an
                # error — re-running resumes where it left off
                total["warning"] = ("no progress — check LASTFM_API_KEY / network, "
                                    "then run again to resume")
                break
        return total
    finally:
        conn.close()


def _job_tiers(set_stage) -> dict:
    conn = db.connect()
    try:
        return scoring.assign_tiers(conn)
    finally:
        conn.close()


def _job_clips(set_stage) -> dict:
    return clips.sweep(max_hours=config.CLIP_SWEEP_MAX_HOURS)


def _job_quality(set_stage) -> dict:
    conn = db.connect()
    try:
        return quality.check(conn, push=True)
    finally:
        conn.close()


def _job_bootstrap(set_stage) -> dict:
    """The whole first-time pipeline, resumable: each step only processes
    what's missing, so re-running after a failure continues where it stopped."""
    out: dict = {}
    conn = db.connect()
    try:
        set_stage("sync")
        r = sync.sync_library(conn, subsonic.Client())
        out["tracks_synced"] = r.get("tracks_active")
        set_stage("lastfm")
        for _ in range(2000):
            r = lastfm.score_batch(conn, limit=200)
            out["lastfm_remaining"] = r["remaining"]
            set_stage(f"lastfm — {r['remaining']} remaining")
            if r["remaining"] == 0:
                break
            if r["scored"] == 0:
                out["warning"] = ("lastfm scoring made no progress — check "
                                  "LASTFM_API_KEY / network, then run bootstrap "
                                  "again to resume where it left off")
                break
        set_stage("tiers")
        out["tiers"] = scoring.assign_tiers(conn)
    finally:
        conn.close()
    set_stage("clips")
    r = clips.sweep()
    out.update({"clips_cut": r.get("cut"), "clips_stopped": r.get("stopped")})
    return out


jobs.register("sync", "Library sync", _job_sync)
jobs.register("lastfm", "Popularity scoring", _job_lastfm)
jobs.register("tiers", "Tier rebuild", _job_tiers)
jobs.register("clips", "Clip cutting", _job_clips)
jobs.register("quality", "Quality check", _job_quality)
jobs.register("bootstrap", "Full pipeline", _job_bootstrap)


@app.post("/api/bootstrap", dependencies=ADMIN)
async def api_bootstrap():
    """Chain the whole first-time setup as one background job.

    Progress shows in /health under "bootstrap" (and on /admin, and in the logs).
    """
    started, st = jobs.start("bootstrap")
    if not started:
        return {"started": False, "already_running": True, "status": st}
    return {"started": True, "watch": "/health"}


# ---------------------------------------------------------------------------
# Game: one live game, all clients drive it over one websocket protocol.
# ---------------------------------------------------------------------------

class Hub:
    def __init__(self):
        self.game: game.Game | None = None
        self.sockets: list[WebSocket] = []
        self.boards: set = set()
        self.board_last_seen: float = 0.0
        self.host_ws = None
        self.display: str | None = (board_cast.display_names() or [None])[0]
        # half-time trivia on/off (#60). Loaded from the settings table at startup —
        # NOT here, this runs at import time and must not touch the DB.
        self.trivia: bool = True
        # which tiers new games draw from (#58) — same story: loaded at startup, not here
        self.difficulty: str = game.DEFAULT_DIFFICULTY
        self.next_host: str | None = None  # game-master rotation across games
        self.last_activity = 0.0   # any game-mutating message — stale games expire (#26)
        self.cast_attempts = 0     # watchdog re-casts per outage — capped (#26)
        # The board refreshes ITSELF between games to keep the DashCast receiver young
        # (#35). That reload leaves a heartbeat gap longer than the 12s death threshold,
        # so the watchdog concluded the board had DIED and fired a full recast on top of
        # it — a far bigger disruption than the reload. That double hit is what dropped
        # the cast at the start of games 2 and 3. A deliberate reload is not a death:
        # the board says so first, and the watchdog holds off until this expires.
        self.board_reload_until = 0.0
        self.games_started = 0  # phones reset per-game state when this changes (#22)
        self.lock = asyncio.Lock()
        self.deadline_task: asyncio.Task | None = None

    async def broadcast(self):
        snap = self.game.snapshot() if self.game else {"phase": "idle"}
        snap["type"] = "state"
        snap["displays"] = board_cast.display_names()
        snap["display"] = self.display or "none"
        snap["trivia"] = self.trivia
        snap["difficulty"] = self.difficulty
        snap["next_host"] = self.next_host  # shown on the finished screen
        snap["game_no"] = self.games_started
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
        """A board is live only if it has HEARTBEATED recently. A socket merely
        being open is not enough: behind a reverse proxy, a dead cast receiver
        can leave a zombie websocket that lingers open — trusting it blocked
        both the re-cast watchdog and the speaker fallback (#21)."""
        import time as _t
        return (any(b in self.sockets for b in self.boards)
                and (_t.time() - self.board_last_seen) < 12)  # heartbeat 5s; dead after 12s (#25/#29)

    def board_expected(self) -> bool:
        """A board was here recently — don't fall back to a speaker over a WS blip."""
        import time as _t
        return self.board_live() or (_t.time() - self.board_last_seen) < 120

    async def start_round(self):
        if not self.game.rounds:
            conn = db.connect()
            try:
                self.game.build_rounds(conn)
            finally:
                conn.close()
        rnd = self.game.start_round()
        if not self.board_expected():  # board plays its own audio when present
            asyncio.get_event_loop().run_in_executor(None, ha.play_clip, rnd["track"]["id"], str(rnd["clip_len"]))
        self.cancel_deadline()
        self.deadline_task = asyncio.create_task(self._deadline(game.ANSWER_WINDOW_S))
        await self.broadcast()

    async def _deadline(self, seconds: float):
        await asyncio.sleep(seconds)
        async with self.lock:
            if not (self.game and self.game.phase == "question"):
                return
            rnd = self.game.rounds[self.game.current]
            if not rnd["answers"] and not rnd.get("replay"):
                # nobody answered — the table is probably talking. One more go.
                rnd["replay"] = 1
                rnd["started_at"] = self.game.clock()
                rnd["deadline_at"] = rnd["started_at"] + game.ANSWER_WINDOW_S
                if not self.board_expected():
                    asyncio.get_event_loop().run_in_executor(
                        None, ha.play_clip, rnd["track"]["id"], str(rnd["clip_len"]))
                self.deadline_task = asyncio.create_task(self._deadline(game.ANSWER_WINDOW_S))
                await self.broadcast()
                return
            await self._reveal()

    async def _reveal(self):
        rnd = self.game.reveal()
        if not self.board_expected():
            asyncio.get_event_loop().run_in_executor(None, ha.play_clip, rnd["track"]["id"], "payoff")
        await self.broadcast()

    async def maybe_early_reveal(self):
        if self.game.phase == "question" and self.game.all_answered():
            self.cancel_deadline()
            await self._reveal()


hub = Hub()

TF_REVEAL_PAUSE_S = 2.0  # drumroll between the last T/F answer and the verdict


async def reveal_tf_after_pause(game_obj, tf_index: int):
    """Reveal the T/F verdict a beat after everyone has answered.

    Bails quietly if the game moved on meanwhile (host force-next, abort,
    new game) — advance_break stays the single reveal path either way."""
    await asyncio.sleep(TF_REVEAL_PAUSE_S)
    g = hub.game
    if g is not game_obj or g.phase != "break" or g.tf_index != tf_index:
        return
    if g.tf_qs[tf_index]["revealed"]:
        return  # the host already forced it
    g.advance_break()
    await hub.broadcast()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    hub.sockets.append(ws)
    name = None
    try:
        snap = hub.game.snapshot() if hub.game else {"phase": "idle"}
        await ws.send_json({**snap, "type": "state",
                            "displays": board_cast.display_names(),
                            "display": hub.display or "none",
                            "trivia": hub.trivia,
                            "difficulty": hub.difficulty})
        while True:
            msg = await ws.receive_json()
            async with hub.lock:
                try:
                    kind = msg.get("type")
                    if kind == "ping":
                        # liveness probe only (#50) — must not touch last_activity,
                        # or an idle phone left on the page keeps a stale game alive
                        await ws.send_json({"type": "pong"})
                        continue
                    if kind not in ("board_hello", "set_display"):
                        # phones are actively playing: stale-game clock and the
                        # watchdog's re-cast budget both reset (#26)
                        import time as _t
                        hub.last_activity = _t.time()
                        hub.cast_attempts = 0
                    if kind == "set_display":
                        want = msg.get("display")
                        if want in (board_cast.display_names() + ["none"]):
                            new = None if want == "none" else want
                            # quit the cast on the display we're leaving so a
                            # DashCast session never lingers/zombies on that TV (#31)
                            if hub.display and hub.display != new:
                                asyncio.get_event_loop().run_in_executor(
                                    None, board_cast.hide_board, hub.display)
                            hub.display = new
                            hub.cast_attempts = 0
                        await hub.broadcast()
                    # NB half-time trivia and difficulty used to be settable from here.
                    # They moved to POST /api/admin/game/settings (#79): they are the
                    # server owner's preferences, and any phone being able to change
                    # them silently for every future game was the bug, not the feature.
                    elif kind == "stop_board":
                        # kill a stuck/zombie DashCast on demand and stand the
                        # watchdog down; music falls back to the speaker (#31)
                        if hub.game and hub.game.host and name and name != hub.game.host:
                            raise game.GameError(f"only {hub.game.host} can turn off the TV board")
                        if hub.display:
                            asyncio.get_event_loop().run_in_executor(
                                None, board_cast.hide_board, hub.display)
                        hub.display = None
                        hub.cast_attempts = 999  # don't auto-recast for this game
                        await hub.broadcast()
                    elif kind == "board_hello":
                        # also the board's 15s heartbeat — liveness, not just registration
                        hub.boards.add(ws)
                        import time as _t
                        hub.board_last_seen = _t.time()
                    elif kind == "new_game":
                        if hub.game and hub.game.phase not in ("finished", "lobby"):
                            raise game.GameError("a game is already running")
                        hub.host_ws = ws
                        if ha.house_is_sleeping() and not msg.get("force"):
                            raise game.GameError("house is Sleeping — start from the board to override")
                        conn = db.connect()
                        try:
                            hub.game = game.Game(conn, rounds=int(msg.get("rounds", 10)),
                                                 tiers=msg.get("tiers") or game.tiers_for(hub.difficulty),
                                                 trivia=hub.trivia)
                            if hub.trivia:
                                trivia.ensure_seeded(conn)
                        finally:
                            conn.close()
                        hub.games_started += 1
                        if hub.next_host:  # the master's chair rotates each game
                            hub.game.host = hub.next_host
                        # refill the T/F pool in the background if it's running low

                        def _topup():
                            c = db.connect()
                            try:
                                trivia.topup_tf(c)
                            except Exception as e:  # noqa: BLE001 — opentdb down ≠ no game
                                LOGGER.warning("trivia topup failed: %s", e)
                            finally:
                                c.close()
                        if hub.trivia:  # no game will ask for it — don't call OpenTDB
                            asyncio.get_event_loop().run_in_executor(None, _topup)
                        # Cast the board ONLY if one isn't already up (#47). Our own
                        # receiver persists across games, so re-casting on every new_game
                        # just tore down a healthy board and reloaded it mid-transition —
                        # that was the "stuck after 10 rounds" between-games crash. The
                        # unconditional recast was a DashCast-era hack (its session died
                        # between games); ours doesn't, so a live board just gets the new
                        # lobby over its existing websocket. First game / no board -> cast.
                        if hub.display and not hub.board_expected():
                            asyncio.get_event_loop().run_in_executor(None, board_cast.show_board, hub.display, False)
                        await hub.broadcast()
                    elif kind == "join":
                        if not hub.game:
                            raise game.GameError("no game — start one first")
                        name = msg.get("name", "")
                        hub.game.join(name)
                        if hub.game.host is None and ws is hub.host_ws:
                            hub.game.host = name.strip()[:24]
                        await hub.broadcast()
                    elif kind == "set_artists":
                        hub.game.set_artists(name or msg.get("name", ""), msg.get("artists") or [])
                        await hub.broadcast()
                    elif kind == "ready":
                        hub.game.set_ready(name or msg.get("name", ""))
                        await hub.broadcast()
                    elif kind == "start_round":
                        if hub.game.host is None and name:
                            hub.game.host = name  # starter never joined from that socket — first driver takes the wheel
                        if hub.game.host and name != hub.game.host:
                            if hub.game.phase == "lobby" and hub.game.host not in hub.game.players:
                                hub.game.host = name  # rotated master isn't playing — presser takes over
                            else:
                                raise game.GameError(f"only {hub.game.host} controls the rounds")
                        if hub.game.phase == "lobby":
                            waiting = hub.game.waiting_on()
                            if waiting:
                                raise game.GameError("not everyone is ready: " + ", ".join(waiting))
                        await hub.start_round()
                    elif kind == "extend_clip":
                        length = hub.game.extend_clip()
                        # the window moved out with the longer clip — move the reveal too
                        hub.cancel_deadline()
                        hub.deadline_task = asyncio.create_task(hub._deadline(hub.game.window_left()))
                        if not hub.board_expected():
                            asyncio.get_event_loop().run_in_executor(
                                None, ha.play_clip, hub.game.rounds[hub.game.current]["track"]["id"], str(length))
                        await hub.broadcast()
                    elif kind == "answer":
                        hub.game.answer(name or msg.get("name", ""), int(msg["choice"]))
                        await hub.maybe_early_reveal()
                        await hub.broadcast()
                    elif kind == "flag_clip":
                        if hub.game.host and name != hub.game.host:
                            raise game.GameError(f"only {hub.game.host} can flag clips")
                        conn = db.connect()
                        try:
                            hub.game.flag_current(conn)
                        finally:
                            conn.close()
                        if hub.game.phase == "question":
                            # a flagged clip isn't worth guessing — end the round now
                            hub.cancel_deadline()
                            await hub._reveal()
                        await hub.broadcast()
                    elif kind == "abort":
                        # Only a PRESENT host owns the abandon. If the rotated master
                        # isn't in the game (crowned absent), anyone can abandon it —
                        # mirrors the take-over start rule, so an orphaned game can't
                        # get stuck unabandonable from every phone (#46).
                        host_present = bool(hub.game and hub.game.host in hub.game.players)
                        if hub.game and hub.game.host and name != hub.game.host and host_present:
                            raise game.GameError(f"only {hub.game.host} can abandon the game")
                        hub.cancel_deadline()
                        hub.game = None
                        asyncio.get_event_loop().run_in_executor(None, board_cast.hide_board, hub.display)
                        await hub.broadcast()
                    elif kind == "tf_answer":
                        hub.game.tf_answer(name or msg.get("name", ""), bool(msg.get("answer")))
                        if hub.game.tf_all_answered():
                            # everyone's in — hold a 2s drumroll before the verdict
                            # (an instant flip read as "the TV knew early" to guests)
                            asyncio.create_task(reveal_tf_after_pause(hub.game, hub.game.tf_index))
                        await hub.broadcast()
                    elif kind == "next":
                        if hub.game.host and name != hub.game.host:
                            raise game.GameError(f"only {hub.game.host} controls the rounds")
                        wait = hub.game.payoff_wait()
                        if wait > 0:  # the payoff plays in full — no skipping the song
                            raise game.GameError(f"let the song play out — {int(wait) + 1}s left")
                        if hub.game.phase == "break":
                            if hub.game.advance_break() == "resume":
                                await hub.start_round()
                            else:
                                await hub.broadcast()
                        elif hub.game.phase == "reveal" and hub.game.is_halfway() and not hub.game.is_last_round():
                            conn = db.connect()
                            try:
                                hub.game.start_break(conn)
                            finally:
                                conn.close()
                            await hub.broadcast()
                        elif hub.game.phase == "reveal" and hub.game.is_last_round():
                            conn = db.connect()
                            try:
                                hub.game.finish(conn)
                            finally:
                                conn.close()
                            if not hub.board_expected():  # board plays its own fanfare
                                asyncio.get_event_loop().run_in_executor(
                                    None, ha.play_url,
                                    f"{ha.APP_BASE_URL}/static/fanfare.mp3", "fanfare")
                            # Rotate the game master: whoever has waited LONGEST.
                            #
                            # It used to rotate over the current game's join order — which
                            # is just who picked up their phone first, and it changes every
                            # game. Alice -> Bob -> Alice, and Carol was never once picked
                            # across the whole life of the app. (Worse: if the host wasn't
                            # in the list, ValueError -> i=-1 -> order[0] -> the role pinned
                            # itself to the fastest joiner.)
                            #
                            # Now: the outgoing master is stamped, and the next one is the
                            # present player who mastered least recently. Never mastered =
                            # first in the queue. Survives restarts, missed games and any
                            # join order.
                            present = list(hub.game.players)
                            if present:
                                conn = db.connect()
                                try:
                                    hist = json.loads(db.get_setting(conn, "master_history") or "{}")
                                    hist[hub.game.host] = int(time.time())    # stamp the outgoing master
                                    hub.next_host = min(
                                        present,
                                        key=lambda n: (hist.get(n, 0), present.index(n)))
                                    db.set_setting(conn, "master_history", json.dumps(hist))
                                finally:
                                    conn.close()
                            # Drop the board to ambient 60s after the game — but ONLY if no
                            # new game has started by then. Back-to-back play (Play Again)
                            # begins the next game within that window; firing this quit
                            # unconditionally sent cast.quit_app() to the LIVE board mid-game-2
                            # (~60s in = round 2) and killed it. Proven by adb logcat: a
                            # USER_REQUEST stop from our own IP at exactly finish+60s (#47).
                            loop = asyncio.get_event_loop()
                            def _to_ambient_if_idle(disp=hub.display):
                                if hub.game is None or hub.game.phase == "finished":
                                    board_cast.hide_board(disp)
                            loop.call_later(60, lambda: loop.run_in_executor(None, _to_ambient_if_idle))
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
        if ws in hub.boards:
            hub.boards.discard(ws)
            # re-casting is handled by the periodic board watchdog (#21) —
            # a clean disconnect just means it notices within one tick


@app.get("/")
def page_index():
    return FileResponse(os.path.join(BASE, "static", "index.html"))


@app.get("/board")
def page_board():
    return FileResponse(os.path.join(BASE, "static", "board.html"))


@app.get("/admin")
def page_admin():
    # the page itself is open; every API call it makes is admin-gated, so the
    # browser prompts for the password on first use (when ADMIN_PASSWORD is set)
    return FileResponse(os.path.join(BASE, "static", "admin.html"))


def _admin_game_summary() -> dict:
    """Game state for /admin — deliberately SPOILER-SAFE.

    The admin might also be a player, so mid-round this must not leak the
    answer: no current track, no options (they'd narrow it to 4). Only rounds
    already revealed to everyone are named."""
    g = hub.game
    if not g:
        return {"phase": "idle"}
    out = {
        "phase": g.phase,
        "host": g.host,
        "round": g.current + 1,
        "total_rounds": len(g.rounds),
        "players": [{"name": p["name"], "score": p["score"]}
                    for p in g.snapshot()["players"]],
        "display": hub.display or "none",
    }
    if g.phase == "question" and g.current >= 0:
        out["answered"] = len(g.rounds[g.current]["answers"])
        out["window_left"] = round(g.window_left(), 1)
    # last track everyone has already seen revealed
    last = g.current if g.phase in ("reveal", "finished") else g.current - 1
    if 0 <= last < len(g.rounds):
        t = g.rounds[last]["track"]
        out["last_revealed"] = {"round": last + 1, "title": t["title"], "artist": t["artist"]}
    return out


def _trivia_stats(conn) -> dict:
    """Per-kind pool counts: total, played (picked at least once), left (fresh)."""
    out = {}
    for r in conn.execute(
            "SELECT kind, COUNT(*) total, SUM(used_at IS NOT NULL) played "
            "FROM trivia GROUP BY kind"):
        out[r["kind"]] = {"total": r["total"], "played": r["played"] or 0,
                          "left": r["total"] - (r["played"] or 0)}
    return out


@app.get("/api/admin/status", dependencies=ADMIN)
def api_admin_status():
    conn = db.connect()
    try:
        trivia_stats = _trivia_stats(conn)
        games = conn.execute("SELECT COUNT(*) c FROM games").fetchone()["c"]
    finally:
        conn.close()
    return {**jobs.status(), "game": _admin_game_summary(),
            "trivia": trivia_stats, "leaderboard_games": games,
            # household game settings live here rather than on the phones (#79)
            "settings": {"trivia": hub.trivia, "difficulty": hub.difficulty,
                         "difficulties": list(game.DIFFICULTIES)}}


@app.post("/api/admin/leaderboard/wipe", dependencies=ADMIN)
def api_admin_leaderboard_wipe():
    """Wipe the all-time leaderboard — the /admin button's confirm dialog is
    the second click; same effect as /api/leaderboard/reset?confirm=yes."""
    conn = db.connect()
    try:
        games = conn.execute("SELECT COUNT(*) c FROM games").fetchone()["c"]
        conn.execute("DELETE FROM results")
        conn.execute("DELETE FROM games")
        conn.commit()
        LOGGER.warning("leaderboard wiped from /admin (%d games)", games)
        return {"reset": True, "games_removed": games}
    finally:
        conn.close()


@app.get("/api/admin/trivia/prompt", dependencies=ADMIN)
def api_admin_trivia_prompt(region: str = "", facts: int = 60, tf: int = 80):
    """The docs/trivia-pack.md LLM prompt, parameterised, ready to paste."""
    return {"prompt": trivia.llm_prompt(region, facts, tf)}


class TriviaImport(BaseModel):
    text: str
    commit: bool = False


@app.post("/api/admin/trivia/import", dependencies=ADMIN)
def api_admin_trivia_import(body: TriviaImport):
    """Import a pasted LLM response into the trivia pool.

    commit=false parses + validates only (the preview); commit=true inserts.
    New items land as never-played (used_at NULL, source 'import') so they're
    picked first — same rules as every other pack."""
    try:
        pack = trivia.parse_pack(body.text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    counts = {"fact": 0, "tf": 0}
    sample = []
    for item in pack:
        if isinstance(item, dict) and item.get("kind") in counts and item.get("text"):
            counts[item["kind"]] += 1
            if len(sample) < 3:
                sample.append(f"[{item['kind']}] {str(item['text'])[:80]}")
    if not body.commit:
        # dry-run the validation for honest reject counts, without inserting
        conn = db.connect(":memory:")
        try:
            r = trivia.insert_items(conn, pack, "import", origin="/admin preview")
        finally:
            conn.close()
        return {"preview": True, "items": len(pack), **counts,
                "would_reject": r["rejects"], "sample": sample}
    conn = db.connect()
    try:
        r = trivia.insert_items(conn, pack, "import", origin="/admin import")
    finally:
        conn.close()
    LOGGER.warning("trivia import from /admin: %d added, %d duplicates, %d rejected",
                   r["added"], r["duplicates"], len(r["rejects"]))
    return {"preview": False, "items": len(pack), **counts, **r}


@app.post("/api/admin/trivia/reset", dependencies=ADMIN)
def api_admin_trivia_reset():
    """Mark every trivia item fresh again — facts and T/F both re-enter the
    pick pool as never-used. Nothing is deleted."""
    conn = db.connect()
    try:
        n = conn.execute("UPDATE trivia SET used_at=NULL WHERE used_at IS NOT NULL").rowcount
        conn.commit()
        LOGGER.warning("trivia usage reset from /admin (%d items marked fresh)", n)
        return {"reset": True, "marked_fresh": n}
    finally:
        conn.close()


@app.post("/api/admin/run/{name}", dependencies=ADMIN)
def api_admin_run(name: str):
    try:
        started, st = jobs.start(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown action: {name}")
    if not started:
        raise HTTPException(status_code=409, detail=f"busy with {st.get('busy_with')}")
    return {"started": True, "job": name}


@app.post("/api/admin/game/settings", dependencies=ADMIN)
async def api_admin_game_settings(body: dict):
    """Half-time trivia and difficulty (#79).

    These were on every phone's setup screen, which put seven option buttons above
    the one button that starts a game — and let any player silently change a
    preference that persists for every future game. They belong to whoever runs the
    server, so they live behind the admin password like the rest of this page.

    Both are optional: send either, or both. Unknown difficulties are rejected.
    """
    changed = {}
    if "trivia" in body:
        changed["trivia"] = bool(body["trivia"])
    if "difficulty" in body:
        want = body["difficulty"]
        if want not in game.DIFFICULTIES:
            raise HTTPException(status_code=400, detail=f"unknown difficulty: {want}")
        changed["difficulty"] = want
    if not changed:
        raise HTTPException(status_code=400, detail="nothing to set")

    conn = db.connect()
    try:
        if "trivia" in changed:
            hub.trivia = changed["trivia"]
            db.set_setting(conn, TRIVIA_SETTING, "1" if hub.trivia else "0")
        if "difficulty" in changed:
            hub.difficulty = changed["difficulty"]
            db.set_setting(conn, DIFFICULTY_SETTING, hub.difficulty)
    finally:
        conn.close()
    # phones show the half-time line off the snapshot, so tell them straight away
    async with hub.lock:
        await hub.broadcast()
    return {"trivia": hub.trivia, "difficulty": hub.difficulty}


@app.post("/api/admin/game/abandon", dependencies=ADMIN)
async def api_admin_game_abandon():
    """Abandon the running game from /admin. The admin overrides the ws
    host-only rule — same reset path as the ws "abort" handler."""
    async with hub.lock:
        if not hub.game:
            raise HTTPException(status_code=404, detail="no game running")
        LOGGER.warning("game abandoned from /admin (%s, round %d)", hub.game.phase, hub.game.current + 1)
        hub.cancel_deadline()
        hub.game = None
        asyncio.get_event_loop().run_in_executor(None, board_cast.hide_board, hub.display)
        await hub.broadcast()
    return {"abandoned": True}


@app.post("/api/admin/game/master", dependencies=ADMIN)
async def api_admin_game_master(name: str):
    """Hand the game-master role to a present player, effective immediately."""
    async with hub.lock:
        if not hub.game:
            raise HTTPException(status_code=404, detail="no game running")
        if name not in hub.game.players:
            raise HTTPException(status_code=400, detail=f"{name} is not in the game")
        old = hub.game.host
        hub.game.host = name
        conn = db.connect()
        try:  # stamp history so the fair-rotation queue stays truthful
            hist = json.loads(db.get_setting(conn, "master_history") or "{}")
            hist[name] = int(time.time())
            db.set_setting(conn, "master_history", json.dumps(hist))
        finally:
            conn.close()
        LOGGER.info("game master changed from /admin: %s -> %s", old, name)
        await hub.broadcast()
    return {"master": name, "was": old}


@app.get("/favicon.ico")
def favicon():
    return FileResponse(os.path.join(BASE, "static", "favicon.svg"),
                        media_type="image/svg+xml")


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
        # a clip we KNOW was cut is unreachable mid-game — almost always a
        # storage blip (soft-mounted share timing out), not a missing file (#24)
        t = rnd["track"]
        LOGGER.error("clip unreachable for ACTIVE round: %s — %s - %s (%s.mp3) — storage blip? "
                     "board will keep retrying", t["id"], t["artist"], t["title"], kind)
        return Response(status_code=404)
    return FileResponse(path, media_type="audio/mpeg",
                        headers={"Cache-Control": "no-store"})


def _resolve_join_url(request: Request, url: str | None) -> str:
    """The address a phone should open to join, for the board's scan-to-join QR.

    Order of trust: an explicit JOIN_URL override; then the board's own origin
    (?url=, which is exactly how the board was actually reached); then BOARD_URL
    minus its /board suffix; finally the incoming request's own base.
    """
    if config.JOIN_URL:
        return config.JOIN_URL
    if url and url.startswith(("http://", "https://")):
        return url.rstrip("/")
    if board_cast.BOARD_URL:
        base = board_cast.BOARD_URL
        if base.endswith("/board"):
            base = base[: -len("/board")]
        return base.rstrip("/")
    return str(request.base_url).rstrip("/")


@app.get("/api/join-url")
def api_join_url(request: Request, url: str | None = None):
    """The resolved join address as text, so the board's caption matches its QR."""
    return {"url": _resolve_join_url(request, url)}


@app.get("/api/join-qr.svg")
def api_join_qr(request: Request, url: str | None = None):
    """An SVG QR of the join address for the cast board's 'scan to join' screen."""
    qr = segno.make(_resolve_join_url(request, url), error="m")
    buff = io.BytesIO()
    # dark modules on an explicit white field (+ quiet-zone border) so it scans
    # against the board's dark theme; CSS sizes it, so intrinsic scale is nominal.
    qr.save(buff, kind="svg", scale=6, border=2, dark="#000000", light="#ffffff")
    return Response(content=buff.getvalue(), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


def _wifi_escape(value: str) -> str:
    """Escape the WIFI: QR payload's special characters (backslash first)."""
    for ch in ("\\", ";", ",", ":", '"'):
        value = value.replace(ch, "\\" + ch)
    return value


def _wifi_payload() -> str:
    """The standard WIFI: join string phones auto-join from (#55)."""
    auth = config.GUEST_WIFI_AUTH.upper()
    if auth == "NOPASS":
        return f"WIFI:T:nopass;S:{_wifi_escape(config.GUEST_WIFI_SSID)};;"
    return (f"WIFI:T:{auth};S:{_wifi_escape(config.GUEST_WIFI_SSID)};"
            f"P:{_wifi_escape(config.GUEST_WIFI_PASSWORD)};;")


@app.get("/api/wifi-qr.svg")
def api_wifi_qr():
    """An SVG QR that joins guests to the guest WiFi (#55).

    404 when GUEST_WIFI_SSID is unset — the board's <img> onerror hides the
    card, so an unconfigured install shows nothing (same fail-safe as the
    join QR). NB: deliberately readable by anyone who can reach the app —
    document as guest-network-only credentials."""
    if not config.GUEST_WIFI_SSID:
        return Response(status_code=404)
    qr = segno.make(_wifi_payload(), error="m")
    buff = io.BytesIO()
    qr.save(buff, kind="svg", scale=6, border=2, dark="#000000", light="#ffffff")
    return Response(content=buff.getvalue(), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


KNOWN_PLAYERS = {}  # ip -> name, from env "Name=ip,Name=ip"
for _part in os.environ.get("KNOWN_PLAYERS", "").split(","):
    if "=" in _part:
        _n, _ip = _part.split("=", 1)
        KNOWN_PLAYERS[_ip.strip()] = _n.strip()


@app.get("/api/whoami")
def api_whoami(request: Request):
    """Suggest a player name from the caller's IP (family phones have fixed IPs)."""
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if request.client else ""))
    return {"name": KNOWN_PLAYERS.get(ip), "ip": ip}


@app.post("/api/board/beat")
async def api_board_beat(request: Request):
    """The board's diagnostic trail (#35).

    The receiver's death is SILENT — when it dies it cannot report that it died, so we
    have only ever inferred the crash from the outside. Each beat says what the board was
    DOING; when the beats stop, the LAST one is the evidence:

      - last beat in a no-audio phase (lobby/reveal/break/scoreboard) -> idle timeout
      - last beat at a consistent page uptime, whatever the phase   -> the session rots with age
      - neither pattern                                             -> the Shield's receiver itself

    `up` is the page's own uptime: if it RESETS, the board reloaded or was recast.
    """
    try:
        b = await request.json()
        LOGGER.warning("BEAT n=%s up=%ss phase=%s round=%s audio=%s ws=%s",
                       b.get("n"), b.get("up"), b.get("phase"), b.get("round"),
                       b.get("audio"), b.get("ws"))
    except Exception:  # noqa: BLE001 - a diagnostic must never break the board
        pass
    return {"ok": True}


@app.post("/api/board/log")
async def api_board_log(request: Request):
    """The TV board reports playback failures here — it has no other voice."""
    try:
        body = await request.json()
        LOGGER.warning("BOARD: %s", str(body.get("msg", ""))[:300])
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/board/reloading")
async def api_board_reloading():
    """The board is about to reload ITSELF. Hold the watchdog off.

    A reload silences the heartbeat for a couple of seconds — longer than the 12s death
    threshold — so the watchdog read a deliberate refresh as a dead receiver and fired a
    full DashCast recast on top of it. That double hit is what dropped the cast at the
    start of the second and third games. A reload announced in advance is not a death.
    """
    import time as _t
    hub.board_reload_until = _t.time() + 30
    LOGGER.warning("BOARD: reloading on purpose — watchdog held off for 30s")
    return {"ok": True}


@app.get("/api/artists/wall")
def api_artists_wall(limit: int = 60):
    """Popular artists with enough quizzable clips — the pick-3 wall."""
    conn = db.connect()
    try:
        # fresh random selection from the top pool each load — the wall varies per game
        rows = conn.execute(
            f"SELECT * FROM (SELECT artist, COUNT(*) n, MAX(global_listeners) pop FROM tracks "
            f"WHERE {game.QUIZZABLE} GROUP BY artist HAVING n >= 3 "
            f"ORDER BY pop DESC LIMIT ?) ORDER BY RANDOM() LIMIT ?", (max(limit * 3, 200), limit)).fetchall()
        return [{"artist": r["artist"], "tracks": r["n"]} for r in rows]
    finally:
        conn.close()


@app.post("/api/ban/album", dependencies=ADMIN)
def api_ban_album(pattern: str):
    """Ban every track whose album matches the SQL LIKE pattern (case-insensitive)."""
    conn = db.connect()
    try:
        n = conn.execute("UPDATE tracks SET banned=1, ban_reason='album' WHERE album LIKE ? AND banned=0",
                         (pattern,)).rowcount
        conn.commit()
        return {"banned": n, "pattern": pattern}
    finally:
        conn.close()


@app.post("/api/leaderboard/reset", dependencies=ADMIN)
def api_leaderboard_reset(confirm: str = ""):
    """Wipe the all-time leaderboard (games + results). Deliberately API-only —
    no button in the family UI. Requires ?confirm=yes."""
    if confirm != "yes":
        return Response(status_code=400,
                        content="add ?confirm=yes to wipe the all-time leaderboard")
    conn = db.connect()
    try:
        games = conn.execute("SELECT COUNT(*) c FROM games").fetchone()["c"]
        conn.execute("DELETE FROM results")
        conn.execute("DELETE FROM games")
        conn.commit()
        return {"reset": True, "games_removed": games}
    finally:
        conn.close()


@app.get("/api/quality")
def api_quality():
    """Tracks that probably failed Last.fm matching (mangled titles)."""
    conn = db.connect()
    try:
        return {"floor": quality.LISTENER_FLOOR, "artist_ceiling": quality.ARTIST_CEILING,
                "suspects": quality.find_suspects(conn)}
    finally:
        conn.close()


@app.post("/api/quality/check", dependencies=ADMIN)
def api_quality_check(push: bool = True):
    """Run the match-quality check; HA-push any fresh suspects and mark them.

    ?push=false baselines the current library without notifying."""
    conn = db.connect()
    try:
        return quality.check(conn, push=push)
    finally:
        conn.close()


@app.post("/api/trivia/topup", dependencies=ADMIN)
def api_trivia_topup():
    """Seed the bundled trivia pack and top up T/F from Open Trivia DB if low."""
    conn = db.connect()
    try:
        seeded = trivia.ensure_seeded(conn)
        return {"seeded": seeded, **trivia.topup_tf(conn)}
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
