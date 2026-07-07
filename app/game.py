"""Game engine — pure logic, no I/O beyond the DB handle it's given.

One game at a time (it's a kitchen, not a casino). The websocket layer in
main.py drives this and broadcasts snapshots; timing uses an injectable
clock so tests don't sleep.
"""
import random
import time
from datetime import datetime, timezone

ANSWER_WINDOW_S = 20
MAX_DURATION_S = 720   # >12 min = DJ mix / live jam, not quizzable
# over-long DJ mixes / live jams never enter the quiz (compilations are fine)
QUIZZABLE = ("active=1 AND banned=0 AND clipped_at IS NOT NULL "
             f"AND (duration IS NULL OR duration <= {MAX_DURATION_S})")
BASE_POINTS = 100
SPEED_BONUS_MAX = 50  # linear decay to 0 across the window


class GameError(RuntimeError):
    pass


def pick_tracks(conn, rounds: int, tiers: list[str]) -> list[dict]:
    qmarks = ",".join("?" * len(tiers))
    rows = conn.execute(
        f"SELECT * FROM tracks WHERE {QUIZZABLE} AND tier IN ({qmarks}) "
        f"ORDER BY RANDOM() LIMIT ?", (*tiers, rounds)).fetchall()
    if len(rows) < rounds:
        raise GameError(f"only {len(rows)} clipped tracks in tiers {tiers} — need {rounds}")
    return [dict(r) for r in rows]


def pick_decoys(conn, track: dict, n: int = 3) -> list[dict]:
    """Plausible wrong answers: same tier, different artist, prefer same decade."""
    decade = (track["year"] or 0) // 10
    rows = conn.execute(
        "SELECT DISTINCT title, artist, year FROM tracks WHERE active=1 AND tier IS NOT NULL "
        "AND artist != ? AND title != ? ORDER BY RANDOM() LIMIT 60",
        (track["artist"], track["title"])).fetchall()
    same_decade = [r for r in rows if (r["year"] or 0) // 10 == decade]
    picked: list[dict] = []
    seen_artists = {track["artist"]}
    for pool in (same_decade, rows):
        for r in pool:
            if len(picked) == n:
                break
            if r["artist"] in seen_artists:
                continue
            picked.append({"title": r["title"], "artist": r["artist"]})
            seen_artists.add(r["artist"])
    if len(picked) < n:
        raise GameError("not enough tiered tracks for decoys")
    return picked


class Game:
    def __init__(self, conn, rounds: int = 10, tiers: list[str] | None = None,
                 clock=time.monotonic):
        tiers = tiers or ["easy", "medium"]
        self.clock = clock
        self.rounds: list[dict] = []
        for t in pick_tracks(conn, rounds, tiers):
            options = pick_decoys(conn, t) + [{"title": t["title"], "artist": t["artist"]}]
            random.shuffle(options)
            self.rounds.append({
                "track": t,
                "options": options,
                "correct": next(i for i, o in enumerate(options)
                                if o["title"] == t["title"] and o["artist"] == t["artist"]),
                "answers": {},        # player -> {choice, elapsed_ms, points}
                "started_at": None,   # clock() when the clip started
                "clip_len": 5,
            })
        self.players: dict[str, dict] = {}  # name -> {score, correct, fastest_ms}
        self.current = -1
        self.phase = "lobby"  # lobby | question | reveal | finished
        self.started_at = datetime.now(timezone.utc).isoformat()

    # -- lobby ---------------------------------------------------------------
    def join(self, name: str) -> None:
        name = name.strip()[:24]
        if not name:
            raise GameError("empty name")
        self.players.setdefault(name, {"score": 0, "correct": 0, "fastest_ms": None})

    # -- rounds --------------------------------------------------------------
    def start_round(self) -> dict:
        if self.phase not in ("lobby", "reveal"):
            raise GameError(f"cannot start a round from {self.phase}")
        if not self.players:
            raise GameError("no players")
        if self.current + 1 >= len(self.rounds):
            raise GameError("no rounds left")
        self.current += 1
        rnd = self.rounds[self.current]
        rnd["started_at"] = self.clock()
        self.phase = "question"
        return rnd

    def extend_clip(self) -> int:
        """Bump the current round to the next clip length (5 -> 10 -> 20)."""
        rnd = self._round("question")
        for length in (10, 20):
            if rnd["clip_len"] < length:
                rnd["clip_len"] = length
                return length
        raise GameError("already at the longest clip")

    def answer(self, name: str, choice: int) -> dict:
        rnd = self._round("question")
        if name not in self.players:
            raise GameError("join first")
        if name in rnd["answers"]:
            raise GameError("already answered")
        elapsed = self.clock() - rnd["started_at"]
        if elapsed > ANSWER_WINDOW_S:
            raise GameError("too late")
        points = 0
        if choice == rnd["correct"]:
            points = BASE_POINTS + int(SPEED_BONUS_MAX * max(0, 1 - elapsed / ANSWER_WINDOW_S))
            p = self.players[name]
            p["score"] += points
            p["correct"] += 1
            ms = int(elapsed * 1000)
            if p["fastest_ms"] is None or ms < p["fastest_ms"]:
                p["fastest_ms"] = ms
        rnd["answers"][name] = {"choice": choice, "elapsed_ms": int(elapsed * 1000),
                                "points": points}
        return rnd["answers"][name]

    def all_answered(self) -> bool:
        rnd = self._round("question")
        return set(rnd["answers"]) >= set(self.players)

    def flag_current(self, conn) -> str:
        """Ban the current round's track (bad clip — applause, silence, etc.)."""
        if self.current < 0:
            raise GameError("no round to flag")
        rnd = self.rounds[self.current]
        conn.execute("UPDATE tracks SET banned=1 WHERE id=?", (rnd["track"]["id"],))
        conn.commit()
        rnd["flagged"] = True
        return rnd["track"]["id"]

    def reveal(self) -> dict:
        rnd = self._round("question")
        self.phase = "reveal"
        return rnd

    def is_last_round(self) -> bool:
        return self.current + 1 >= len(self.rounds)

    def finish(self, conn) -> int:
        self.phase = "finished"
        cur = conn.execute("INSERT INTO games(started_at, finished_at, rounds) VALUES(?,?,?)",
                           (self.started_at, datetime.now(timezone.utc).isoformat(),
                            len(self.rounds)))
        game_id = cur.lastrowid
        for name, p in self.players.items():
            conn.execute(
                "INSERT INTO results(game_id, player, score, correct, fastest_ms) VALUES(?,?,?,?,?)",
                (game_id, name, p["score"], p["correct"], p["fastest_ms"]))
        conn.commit()
        return game_id

    # -- snapshots -----------------------------------------------------------
    def snapshot(self) -> dict:
        """State for clients. The correct answer only ships during reveal/finished."""
        s = {
            "phase": self.phase,
            "round": self.current + 1,
            "total_rounds": len(self.rounds),
            "players": [{"name": n, **p} for n, p in
                        sorted(self.players.items(), key=lambda kv: -kv[1]["score"])],
        }
        if self.current >= 0 and self.phase in ("question", "reveal"):
            rnd = self.rounds[self.current]
            s["options"] = [f'{o["title"]} — {o["artist"]}' for o in rnd["options"]]
            s["clip_len"] = rnd["clip_len"]
            s["flagged"] = bool(rnd.get("flagged"))
            s["answered"] = sorted(rnd["answers"])
            if self.phase == "reveal":
                t = rnd["track"]
                s["correct"] = rnd["correct"]
                s["track"] = {"id": t["id"], "title": t["title"], "artist": t["artist"],
                              "album": t["album"], "year": t["year"]}
                s["round_answers"] = rnd["answers"]
        return s

    def _round(self, want_phase: str) -> dict:
        if self.phase != want_phase or self.current < 0:
            raise GameError(f"not in {want_phase} phase")
        return self.rounds[self.current]


def all_time_leaderboard(conn, limit: int = 20) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT player, COUNT(*) games, SUM(score) total_score, SUM(correct) total_correct, "
        "MIN(fastest_ms) fastest_ms FROM results GROUP BY player "
        "ORDER BY total_score DESC LIMIT ?", (limit,))]
