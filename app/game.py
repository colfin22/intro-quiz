"""Game engine — pure logic, no I/O beyond the DB handle it's given.

One game at a time (it's a kitchen, not a casino). The websocket layer in
main.py drives this and broadcasts snapshots; timing uses an injectable
clock so tests don't sleep.
"""
import random
import time
from datetime import datetime, timezone

from . import trivia

ANSWER_WINDOW_S = 20
PAYOFF_S = 12          # mirrors clips.PAYOFF_LEN — the reveal payoff plays in full
TF_COUNT = 3           # true/false questions at half time
TF_POINTS = 50         # enough to shake the standings, not to decide the game
MAX_DURATION_S = 720   # >12 min = DJ mix / live jam, not quizzable
# over-long DJ mixes / live jams never enter the quiz (compilations are fine)
QUIZZABLE = ("active=1 AND banned=0 AND clipped_at IS NOT NULL "
             f"AND (duration IS NULL OR duration <= {MAX_DURATION_S})")
BASE_POINTS = 100
SPEED_BONUS_MAX = 50  # linear decay to 0 across the window


class GameError(RuntimeError):
    pass


def pick_tracks(conn, rounds: int, tiers: list[str], exclude: set | None = None) -> list[dict]:
    qmarks = ",".join("?" * len(tiers))
    ex = exclude or set()
    exq = f"AND id NOT IN ({','.join('?' * len(ex))}) " if ex else ""
    rows = conn.execute(
        f"SELECT * FROM tracks WHERE {QUIZZABLE} AND tier IN ({qmarks}) {exq}"
        f"ORDER BY RANDOM() LIMIT ?", (*tiers, *ex, rounds)).fetchall()
    if len(rows) < rounds:
        raise GameError(f"only {len(rows)} clipped tracks in tiers {tiers} — need {rounds}")
    return [dict(r) for r in rows]


def pick_artist_track(conn, artists: list[str], exclude: set) -> dict | None:
    """One quizzable track by any of the player's chosen artists (any tier)."""
    if not artists:
        return None
    aq = ",".join("?" * len(artists))
    ex = exclude or set()
    exq = f"AND id NOT IN ({','.join('?' * len(ex))}) " if ex else ""
    row = conn.execute(
        f"SELECT * FROM tracks WHERE {QUIZZABLE} AND artist IN ({aq}) {exq}"
        f"ORDER BY RANDOM() LIMIT 1", (*artists, *ex)).fetchone()
    return dict(row) if row else None


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
        self.tiers = tiers or ["easy", "medium"]
        self.n_rounds = rounds
        self.clock = clock
        self.rounds: list[dict] = []  # built lazily at first start_round, after artist picks
        # fail fast if the pool can't even fill a plain game
        pick_tracks(conn, rounds, self.tiers)
        self.players: dict[str, dict] = {}  # name -> {score, correct, fastest_ms, artists}
        self.current = -1
        self.host: str | None = None  # the player who started the game — runs the rounds
        self.phase = "lobby"  # lobby | question | reveal | break | finished
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.revealed_at: float | None = None  # clock() at reveal — gates "next" behind the payoff
        # half-time trivia (populated by start_break)
        self.break_facts: dict[str, str] = {}  # player -> fact to read aloud
        self.tf_qs: list[dict] = []
        self.tf_index = -1  # -1 = facts stage, else current T/F question

    def set_artists(self, name: str, artists: list[str]) -> None:
        if self.phase != "lobby":
            raise GameError("artists can only be picked in the lobby")
        if name not in self.players:
            raise GameError("join first")
        self.players[name]["artists"] = [a for a in artists if isinstance(a, str)][:3]
        self.players[name]["ready"] = True

    def set_ready(self, name: str) -> None:
        if name not in self.players:
            raise GameError("join first")
        self.players[name]["ready"] = True

    def waiting_on(self) -> list[str]:
        return [n for n, p in self.players.items() if not p.get("ready")]

    def _mk_round(self, conn, t: dict) -> dict:
        options = pick_decoys(conn, t) + [{"title": t["title"], "artist": t["artist"]}]
        random.shuffle(options)
        return {
            "track": t,
            "options": options,
            "correct": next(i for i, o in enumerate(options)
                            if o["title"] == t["title"] and o["artist"] == t["artist"]),
            "answers": {},        # player -> {choice, elapsed_ms, points}
            "started_at": None,   # clock() when the clip started
            "deadline_at": None,  # clock() when the answer window shuts (moves on extend)
            "clip_len": 5,
        }

    def build_rounds(self, conn) -> None:
        """One boost round per player from their chosen artists, rest from the pool."""
        if self.rounds:
            return
        picked: list[dict] = []
        ids: set = set()
        for p in self.players.values():
            if len(picked) >= self.n_rounds - 1:
                break  # keep at least one neutral round
            t = pick_artist_track(conn, p.get("artists") or [], ids)
            if t:
                picked.append(t)
                ids.add(t["id"])
        picked += pick_tracks(conn, self.n_rounds - len(picked), self.tiers, exclude=ids)
        random.shuffle(picked)  # boost rounds indistinguishable
        self.rounds = [self._mk_round(conn, t) for t in picked]

    # -- lobby ---------------------------------------------------------------
    def join(self, name: str) -> None:
        name = name.strip()[:24]
        if not name:
            raise GameError("empty name")
        self.players.setdefault(name, {"score": 0, "correct": 0, "fastest_ms": None, "artists": [], "ready": False})

    # -- rounds --------------------------------------------------------------
    def start_round(self) -> dict:
        if self.phase not in ("lobby", "reveal", "break"):
            raise GameError(f"cannot start a round from {self.phase}")
        if not self.players:
            raise GameError("no players")
        if self.current + 1 >= len(self.rounds):
            raise GameError("no rounds left")
        self.current += 1
        rnd = self.rounds[self.current]
        rnd["started_at"] = self.clock()
        rnd["deadline_at"] = rnd["started_at"] + ANSWER_WINDOW_S
        self.phase = "question"
        return rnd

    def extend_clip(self) -> int:
        """Bump the current round to the next clip length (5 -> 10 -> 20).

        The clip replays from the start, so the answer window moves out with
        it — long enough to hear the whole clip plus thinking time. Without
        this, extending to 20s near the deadline cut the clip off mid-play.
        """
        rnd = self._round("question")
        for length in (10, 20):
            if rnd["clip_len"] < length:
                rnd["clip_len"] = length
                rnd["deadline_at"] = self.clock() + max(ANSWER_WINDOW_S, length + 10)
                return length
        raise GameError("already at the longest clip")

    def window_left(self) -> float:
        """Seconds until the current round's answer window shuts."""
        rnd = self._round("question")
        return max(0.0, rnd["deadline_at"] - self.clock())

    def answer(self, name: str, choice: int) -> dict:
        rnd = self._round("question")
        if name not in self.players:
            raise GameError("join first")
        if name in rnd["answers"]:
            raise GameError("already answered")
        elapsed = self.clock() - rnd["started_at"]
        if self.clock() > rnd["deadline_at"]:
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
        conn.execute("UPDATE tracks SET banned=1, ban_reason='flag' WHERE id=?", (rnd["track"]["id"],))
        conn.commit()
        rnd["flagged"] = True
        return rnd["track"]["id"]

    def reveal(self) -> dict:
        rnd = self._round("question")
        self.phase = "reveal"
        self.revealed_at = self.clock()
        return rnd

    def payoff_wait(self) -> float:
        """Seconds until the payoff clip has played out — 'next' is locked until 0."""
        if self.phase != "reveal" or self.revealed_at is None:
            return 0.0
        return max(0.0, PAYOFF_S - (self.clock() - self.revealed_at))

    # -- half time -------------------------------------------------------------
    def start_break(self, conn) -> None:
        """Half-time show: a fact per player to read aloud, then T/F questions."""
        if self.phase != "reveal":
            raise GameError("half time only follows a reveal")
        facts = trivia.pick(conn, "fact", len(self.players))
        names = list(self.players)
        random.shuffle(names)
        self.break_facts = {n: f["text"] for n, f in zip(names, facts)}
        self.tf_qs = [{"text": q["text"], "answer": bool(q["answer"]),
                       "answers": {}, "revealed": False}
                      for q in trivia.pick(conn, "tf", TF_COUNT) if q["answer"] is not None]
        self.tf_index = -1
        self.phase = "break"

    def _tf_current(self) -> dict:
        if self.phase != "break" or self.tf_index < 0:
            raise GameError("no true/false question live")
        return self.tf_qs[self.tf_index]

    def tf_answer(self, name: str, val: bool) -> None:
        q = self._tf_current()
        if name not in self.players:
            raise GameError("join first")
        if q["revealed"]:
            raise GameError("answer's already out")
        if name in q["answers"]:
            raise GameError("already answered")
        q["answers"][name] = {"choice": bool(val), "points": 0}

    def tf_all_answered(self) -> bool:
        return self.tf_index >= 0 and set(self._tf_current()["answers"]) >= set(self.players)

    def _tf_reveal(self) -> None:
        q = self._tf_current()
        q["revealed"] = True
        for n, a in q["answers"].items():
            if a["choice"] == q["answer"]:
                a["points"] = TF_POINTS
                self.players[n]["score"] += TF_POINTS

    def advance_break(self) -> str:
        """Host 'next' during the break: facts -> T/F -> reveal -> ... -> resume."""
        if self.phase != "break":
            raise GameError("not at half time")
        if self.tf_index < 0:
            if not self.tf_qs:
                return "resume"  # bank empty — plain snacks break
            self.tf_index = 0
            return "tf"
        q = self.tf_qs[self.tf_index]
        if not q["revealed"]:
            self._tf_reveal()
            return "tf_reveal"
        if self.tf_index + 1 < len(self.tf_qs):
            self.tf_index += 1
            return "tf"
        return "resume"

    def is_last_round(self) -> bool:
        return self.current + 1 >= len(self.rounds)

    def is_halfway(self) -> bool:
        return len(self.rounds) >= 6 and self.current + 1 == len(self.rounds) // 2

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
            "host": self.host,
            "round": self.current + 1,
            "total_rounds": len(self.rounds),
            "players": [{"name": n, "score": p["score"], "correct": p["correct"],
                         "fastest_ms": p["fastest_ms"], "picked_artists": bool(p.get("artists")),
                         "ready": bool(p.get("ready"))}
                        for n, p in sorted(self.players.items(), key=lambda kv: -kv[1]["score"])],
        }
        if self.current >= 0 and self.phase in ("question", "reveal"):
            rnd = self.rounds[self.current]
            s["options"] = [f'{o["title"]} — {o["artist"]}' for o in rnd["options"]]
            s["clip_len"] = rnd["clip_len"]
            s["replay"] = rnd.get("replay", 0)
            if self.phase == "question":
                s["window_left"] = round(self.window_left(), 1)
            s["flagged"] = bool(rnd.get("flagged"))
            s["answered"] = sorted(rnd["answers"])
            if self.phase == "reveal":
                t = rnd["track"]
                s["correct"] = rnd["correct"]
                s["track"] = {"id": t["id"], "title": t["title"], "artist": t["artist"],
                              "album": t["album"], "year": t["year"]}
                s["round_answers"] = rnd["answers"]
                s["payoff_wait"] = round(self.payoff_wait(), 1)
        if self.phase == "break":
            s["break_stage"] = "facts" if self.tf_index < 0 else "tf"
            s["facts"] = self.break_facts  # phones show only their own; it's a kitchen
            if self.tf_index >= 0:
                q = self.tf_qs[self.tf_index]
                tf = {"num": self.tf_index + 1, "total": len(self.tf_qs), "text": q["text"],
                      "answered": sorted(q["answers"]), "revealed": q["revealed"],
                      "last": self.tf_index + 1 == len(self.tf_qs)}
                if q["revealed"]:  # the answer only ships once it's out
                    tf["answer"] = q["answer"]
                    tf["results"] = {n: a["points"] for n, a in q["answers"].items()}
                s["tf"] = tf
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
