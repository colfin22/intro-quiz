import os
import tempfile

import pytest

from app import db, game


def make_db(n_tracks=30):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = db.connect(path)
    for i in range(n_tracks):
        conn.execute(
            "INSERT INTO tracks(id,title,artist,album,year,tier,clipped_at,global_listeners,active) "
            "VALUES(?,?,?,?,?,?,?,?,1)",
            (f"t{i}", f"Song {i}", f"Artist {i}", "Album", 1990 + (i % 4) * 10,
             "easy" if i % 2 == 0 else "medium", "2026-07-06T00:00:00", 1000 + i))
    conn.commit()
    return conn, path


class Clock:
    def __init__(self):
        self.t = 100.0
    def __call__(self):
        return self.t


def test_full_game_flow():
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=2, tiers=["easy", "medium"], clock=clock)
        g.join("Alice"); g.join("Bob")
        g.build_rounds(conn)
        rnd = g.start_round()
        assert g.phase == "question" and len(rnd["options"]) == 4
        correct = rnd["correct"]
        # correct answer at t+2s: base + speed bonus
        clock.t += 2
        a = g.answer("Alice", correct)
        assert a["points"] == 100 + int(50 * (1 - 2 / 20))
        # wrong answer scores nothing
        clock.t += 3
        assert g.answer("Bob", (correct + 1) % 4)["points"] == 0
        assert g.all_answered()
        g.reveal()
        snap = g.snapshot()
        assert snap["correct"] == correct and snap["track"]["title"]
        # round 2 → finish
        g.start_round()
        clock.t += 25  # window expired
        with pytest.raises(game.GameError):
            g.answer("Alice", 0)
        g.reveal()
        assert g.is_last_round()
        gid = g.finish(conn)
        rows = {r["player"]: r for r in conn.execute("SELECT * FROM results WHERE game_id=?", (gid,))}
        assert rows["Alice"]["score"] > 0 and rows["Alice"]["correct"] == 1
        assert rows["Bob"]["score"] == 0
        lb = game.all_time_leaderboard(conn)
        assert lb[0]["player"] == "Alice"
    finally:
        os.unlink(p)


def test_options_contain_answer_and_unique_artists():
    conn, p = make_db()
    try:
        g = game.Game(conn, rounds=5, clock=Clock())
        g.join("X"); g.build_rounds(conn)
        for rnd in g.rounds:
            t = rnd["track"]
            labels = [(o["title"], o["artist"]) for o in rnd["options"]]
            assert (t["title"], t["artist"]) in labels
            artists = [o["artist"] for o in rnd["options"]]
            assert len(set(artists)) == 4  # no duplicate artists among options
    finally:
        os.unlink(p)


def test_snapshot_hides_answer_during_question():
    conn, p = make_db()
    try:
        g = game.Game(conn, rounds=1, clock=Clock())
        g.join("X")
        g.build_rounds(conn)
        g.start_round()
        snap = g.snapshot()
        assert "correct" not in snap and "track" not in snap
        assert len(snap["options"]) == 4
    finally:
        os.unlink(p)


def test_guards():
    conn, p = make_db(4)
    try:
        with pytest.raises(game.GameError):  # not enough clipped tracks
            game.Game(conn, rounds=10, clock=Clock())
        g = game.Game(conn, rounds=1, clock=Clock())
        with pytest.raises(game.GameError):  # no players yet
            g.start_round()
        g.join("A")
        g.build_rounds(conn)
        g.start_round()
        with pytest.raises(game.GameError):  # stranger can't answer
            g.answer("B", 0)
        g.answer("A", 0)
        with pytest.raises(game.GameError):  # no double answer
            g.answer("A", 1)
        assert g.extend_clip() == 10 and g.extend_clip() == 20
        with pytest.raises(game.GameError):
            g.extend_clip()
    finally:
        os.unlink(p)


def test_flag_current_bans_track():
    conn, p = make_db()
    try:
        g = game.Game(conn, rounds=1, clock=Clock())
        g.join("A")
        g.build_rounds(conn)
        g.start_round()
        tid = g.flag_current(conn)
        assert conn.execute("SELECT banned FROM tracks WHERE id=?", (tid,)).fetchone()["banned"] == 1
        assert g.snapshot()["flagged"] is True
        # banned tracks never picked again
        ids = {r["track"]["id"] for r in game.Game(conn, rounds=5, clock=Clock()).rounds}
        assert tid not in ids
    finally:
        os.unlink(p)



def test_artist_boost_rounds():
    conn, p = make_db()
    try:
        g = game.Game(conn, rounds=4, clock=Clock())
        g.join("Bob")
        g.set_artists("Bob", ["Artist 7", "Artist 9"])
        g.join("Carol")  # no picks — no boost round for him
        g.build_rounds(conn)
        artists = [r["track"]["artist"] for r in g.rounds]
        assert any(a in ("Artist 7", "Artist 9") for a in artists), artists
        assert len(g.rounds) == 4
        assert len({r["track"]["id"] for r in g.rounds}) == 4  # no dupes
        snap = g.snapshot()
        by = {pl["name"]: pl for pl in snap["players"]}
        assert by["Bob"]["picked_artists"] is True
        assert by["Carol"]["picked_artists"] is False
        assert "artists" not in by["Bob"]  # picks never leak in snapshots
    finally:
        os.unlink(p)


def test_extend_clip_extends_the_window():
    """Extending to a 20s clip near the deadline must not cut the clip off —
    the answer window moves out with the replayed clip."""
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, clock=clock)
        g.join("A"); g.join("B"); g.build_rounds(conn)
        g.start_round()
        assert g.window_left() == game.ANSWER_WINDOW_S
        clock.t += 18  # extend just before the old deadline
        assert g.extend_clip() == 10
        assert g.window_left() == game.ANSWER_WINDOW_S  # fresh 20s
        clock.t += 15
        assert g.extend_clip() == 20
        assert g.window_left() == 30  # 20s clip + 10s thinking time
        clock.t += 28  # 61s after round start — old window long gone
        a = g.answer("A", g.rounds[0]["correct"])
        assert a["points"] == game.BASE_POINTS  # correct, but speed bonus decayed to 0
        clock.t += 5   # now past even the extended window
        with pytest.raises(game.GameError):
            g.answer("B", 0)
        assert g.snapshot().get("window_left") == 0
    finally:
        os.unlink(p)


def test_payoff_gates_next():
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, clock=clock)
        g.join("A"); g.build_rounds(conn)
        assert g.payoff_wait() == 0  # no gate outside reveal
        g.start_round()
        clock.t += 2
        g.answer("A", 0)
        g.reveal()
        assert g.payoff_wait() == game.PAYOFF_S
        clock.t += 5
        assert g.payoff_wait() == game.PAYOFF_S - 5
        clock.t += 10  # past the end
        assert g.payoff_wait() == 0
        assert g.snapshot()["payoff_wait"] == 0
    finally:
        os.unlink(p)


def test_half_time_trivia_flow():
    from app import trivia
    conn, p = make_db()
    try:
        trivia.ensure_seeded(conn)
        clock = Clock()
        g = game.Game(conn, rounds=6, clock=clock)
        g.join("A"); g.join("B")
        g.build_rounds(conn)
        for _ in range(3):  # play to halfway
            g.start_round()
            clock.t += 1
            g.answer("A", g.rounds[g.current]["correct"])
            g.answer("B", 0 if g.rounds[g.current]["correct"] else 1)
            g.reveal()
            clock.t += game.PAYOFF_S
        assert g.is_halfway()
        g.start_break(conn)
        snap = g.snapshot()
        assert snap["phase"] == "break" and snap["break_stage"] == "facts"
        assert set(snap["facts"]) == {"A", "B"} and all(snap["facts"].values())
        assert len(g.tf_qs) == game.TF_COUNT
        with pytest.raises(game.GameError):  # no T/F live yet
            g.tf_answer("A", True)
        scores = {n: pl["score"] for n, pl in g.players.items()}
        assert g.advance_break() == "tf"
        for i in range(game.TF_COUNT):
            q = g.tf_qs[i]
            snap = g.snapshot()
            assert snap["break_stage"] == "tf" and snap["tf"]["text"] == q["text"]
            assert "answer" not in snap["tf"]  # answer never ships early
            g.tf_answer("A", q["answer"])       # A always right
            g.tf_answer("B", not q["answer"])   # B always wrong
            with pytest.raises(game.GameError):  # no double answer
                g.tf_answer("A", True)
            assert g.tf_all_answered()
            assert g.advance_break() == "tf_reveal"
            snap = g.snapshot()
            assert snap["tf"]["revealed"] and snap["tf"]["results"]["A"] == game.TF_POINTS
            expected = "resume" if i + 1 == game.TF_COUNT else "tf"
            assert g.advance_break() == expected
        assert g.players["A"]["score"] == scores["A"] + game.TF_COUNT * game.TF_POINTS
        assert g.players["B"]["score"] == scores["B"]
        g.start_round()  # play on
        assert g.phase == "question"
    finally:
        os.unlink(p)


def test_trivia_seed_and_recycling():
    from app import trivia
    conn, p = make_db()
    try:
        # the pack must actually be shipped — a gitignore once ate app/data/
        import subprocess
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", trivia.SEED_PATH],
            capture_output=True, cwd=os.path.dirname(trivia.SEED_PATH))
        assert tracked.returncode == 0, "trivia_seed.json is not tracked by git"
        added = trivia.ensure_seeded(conn)
        assert added >= 80
        assert trivia.ensure_seeded(conn) == 0  # idempotent
        rows = conn.execute("SELECT * FROM trivia").fetchall()
        assert all(r["answer"] in (0, 1) for r in rows if r["kind"] == "tf")
        n_facts = sum(1 for r in rows if r["kind"] == "fact")
        # picking more than the bank holds recycles rather than starving
        first = trivia.pick(conn, "fact", n_facts)
        assert len(first) == n_facts
        again = trivia.pick(conn, "fact", 5)
        assert len(again) == 5  # recycled from used
    finally:
        os.unlink(p)


def test_trivia_custom_pack_and_builtin_optout(tmp_path, monkeypatch):
    import json

    from app import config, trivia
    conn, p = make_db()
    try:
        # custom pack sits beside the DB on the data volume
        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "quiz.db"))
        pack = [{"kind": "fact", "text": "A local fact"},
                {"kind": "tf", "text": "A local claim.", "answer": 1},
                {"kind": "tf", "text": "broken item, no answer"},   # skipped, not fatal
                {"kind": "nonsense", "text": "bad kind"}]           # skipped, not fatal
        (tmp_path / "trivia_custom.json").write_text(json.dumps(pack))
        # builtin off: only the two valid custom items land
        monkeypatch.setattr(config, "TRIVIA_BUILTIN_PACK", False)
        assert trivia.ensure_seeded(conn) == 2
        rows = conn.execute("SELECT source, COUNT(*) c FROM trivia GROUP BY source").fetchall()
        assert {r["source"]: r["c"] for r in rows} == {"custom": 2}
        # builtin back on: shipped pack joins the custom one, custom rows kept
        monkeypatch.setattr(config, "TRIVIA_BUILTIN_PACK", True)
        assert trivia.ensure_seeded(conn) >= 80
        assert conn.execute("SELECT COUNT(*) c FROM trivia WHERE source='custom'").fetchone()["c"] == 2
    finally:
        os.unlink(p)


def test_phone_ui_renders_every_phase():
    """Run the JS render smoke in node — catches thrown renders python tests can't see."""
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node not available")
    r = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "js", "render_smoke.js")],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
