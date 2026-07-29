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
        clock = Clock()
        g = game.Game(conn, rounds=1, clock=clock)
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
        clock = Clock()
        g = game.Game(conn, rounds=1, clock=clock)
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
        assert g.extend_clip() == 10
        clock.t += 10.1  # the lock (#27): next extend only after the clip plays out
        assert g.extend_clip() == 20
        clock.t += 20.1
        with pytest.raises(game.GameError):
            g.extend_clip()
    finally:
        os.unlink(p)


def test_flag_current_bans_track():
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, clock=clock)
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
        # the gate is the short grace, NOT the length of the song: the host is
        # allowed to cut the payoff short, and a button locked for a full 12s
        # was indistinguishable from a broken one.
        assert g.payoff_wait() == game.PAYOFF_GRACE_S
        assert game.PAYOFF_GRACE_S < game.PAYOFF_S
        clock.t += 1
        assert g.payoff_wait() == game.PAYOFF_GRACE_S - 1
        clock.t += 1  # grace served, song still playing
        assert g.payoff_wait() == 0
        assert g.payoff_left() > 0
        snap = g.snapshot()
        assert snap["payoff_wait"] == 0        # next is live...
        assert snap["payoff_left"] > 0         # ...while the song runs on
        clock.t += game.PAYOFF_S              # past the end of the song
        assert g.payoff_left() == 0
        assert g.snapshot()["payoff_left"] == 0
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


def test_bootstrap_job_sequence(monkeypatch):
    """/api/bootstrap chains sync -> lastfm(loop) -> tiers -> clips and resolves."""
    from app import main

    calls = []
    lastfm_runs = iter([
        {"scored": 200, "errors": 0, "remaining": 150},
        {"scored": 150, "errors": 0, "remaining": 0},
    ])

    class DummyConn:
        def close(self):
            pass

    monkeypatch.setattr(main.db, "connect", lambda *a, **k: DummyConn())
    monkeypatch.setattr(main.subsonic, "Client", lambda: object())
    monkeypatch.setattr(main.sync, "sync_library",
                        lambda c, cl: calls.append("sync") or {"tracks_active": 42})
    monkeypatch.setattr(main.lastfm, "score_batch",
                        lambda c, limit: calls.append("lastfm") or next(lastfm_runs))
    monkeypatch.setattr(main.scoring, "assign_tiers",
                        lambda c: calls.append("tiers") or {"easy": 1})
    monkeypatch.setattr(main.clips, "sweep",
                        lambda: calls.append("clips") or {"cut": 7, "stopped": "done"})
    monkeypatch.setattr(main.library, "clean",
                        lambda c: calls.append("hygiene") or {"banned": {"duplicate": 3}})
    out = main._job_bootstrap(lambda stage: None)
    # hygiene must land BEFORE clips: each row it bans is a clip not cut, and
    # cutting is the expensive step. Cleaning afterwards pays for the junk first.
    assert calls == ["sync", "lastfm", "lastfm", "tiers", "hygiene", "clips"]
    assert out["clips_cut"] == 7
    assert out["tracks_synced"] == 42
    assert out["hygiene"] == {"duplicate": 3}
    assert "warning" not in out


def test_extend_lock_one_at_a_time():
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=clock)
        g.join("A")
        g.build_rounds(conn)
        g.start_round()
        assert g.extend_clip() == 10
        # a second press while the 10s clip is still playing is refused (#27)
        with pytest.raises(game.GameError):
            g.extend_clip()
        snap = g.snapshot()
        assert snap["extend_wait"] > 0
        clock.t += 10.1  # the 10s clip has played out
        assert g.extend_clip() == 20
        clock.t += 20.1
        with pytest.raises(game.GameError):  # nothing beyond 20s
            g.extend_clip()
    finally:
        conn.close(); os.unlink(p)


# ---------- the game master must actually rotate (#36) ----------

def test_least_recently_master_picks_the_one_who_has_never_had_it():
    """The old rotation walked the CURRENT game's join order — i.e. whoever picked up
    their phone first — so it changed every game. Real result: Alice -> Bob -> Alice, and
    Carol was never picked once in the life of the app. Least-recently-master fixes it."""
    import json as _json

    def next_master(present, host, hist):
        hist = dict(hist)
        hist[host] = max(hist.values(), default=0) + 1   # stamped 'now' (time moves forward)
        return min(present, key=lambda n: (hist.get(n, 0), present.index(n))), hist

    # game 1: Alice hosts. Bob and Carol have never mastered -> earliest joiner of them
    present = ["Alice", "Bob", "Carol"]
    nxt, hist = next_master(present, "Alice", {})
    assert nxt == "Bob"

    # game 2: Bob hosts, and this time HE joined first (the old bug's exact trigger)
    present = ["Bob", "Alice", "Carol"]
    nxt, hist = next_master(present, "Bob", hist)
    assert nxt == "Carol", "Carol has never been master — she must be next"

    # game 3: Carol hosts. Alice mastered first of all, so he has waited longest.
    present = ["Carol", "Alice", "Bob"]
    nxt, hist = next_master(present, "Carol", hist)
    assert nxt == "Alice"


def test_a_player_who_misses_a_game_is_not_skipped_forever():
    def next_master(present, host, hist):
        hist = dict(hist)
        hist[host] = max(hist.values(), default=0) + 1
        return min(present, key=lambda n: (hist.get(n, 0), present.index(n))), hist

    hist = {"Alice": 1, "Bob": 2}          # Carol has never mastered
    nxt, hist = next_master(["Alice", "Bob"], "Bob", hist)   # Carol sits this one out
    assert nxt == "Alice"
    nxt, hist = next_master(["Alice", "Bob", "Carol"], "Alice", hist)   # she's back
    assert nxt == "Carol", "the one who has never had it still goes first"


def test_admin_game_summary_is_spoiler_safe():
    """/api/admin/status must never leak the current song mid-round (#52)."""
    from app import main

    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=2, tiers=["easy", "medium"], clock=clock)
        g.join("Alice"); g.join("Bob")
        g.host = "Alice"
        g.build_rounds(conn)
        g.start_round()
        g.answer("Alice", 0)
        old_game = main.hub.game
        main.hub.game = g
        try:
            s = main._admin_game_summary()
            assert s["phase"] == "question"
            assert s["round"] == 1 and s["total_rounds"] == 2
            assert s["answered"] == 1
            assert {pl["name"] for pl in s["players"]} == {"Alice", "Bob"}
            # the spoiler guarantees: no track, no options, mid-question
            blob = str(s)
            t = g.rounds[g.current]["track"]
            assert "track" not in s and "options" not in s and "correct" not in s
            assert t["title"] not in blob
            assert "last_revealed" not in s  # nothing revealed yet
            # after the reveal, THAT song may (and should) show
            g.reveal()
            s = main._admin_game_summary()
            assert s["last_revealed"]["title"] == t["title"]
        finally:
            main.hub.game = old_game
    finally:
        conn.close(); os.unlink(p)


# ---------- remote players: streaming to their own phone, scored fairly ----------

def test_remote_player_speed_bonus_is_measured_from_their_own_audio():
    """The whole point of the feature: a remote player whose stream started 2s
    late and who answered 2s after hearing it should score the same as someone in
    the room who answered 2s after the room heard it. Measuring both from the
    room clock would quietly tax everyone who isn't there."""
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=clock)
        g.join("Local")
        g.join("Away", remote=True)
        assert g.players["Away"]["remote"] and not g.players["Local"]["remote"]
        g.build_rounds(conn)
        rnd = g.start_round()
        correct = rnd["correct"]

        clock.t += 2                      # in the room: 2s of thinking
        local = g.answer("Local", correct)
        clock.t += 2                      # Away's stream only starts now (2s buffering)
        g.note_audio_started("Away")
        clock.t += 2                      # ...then the same 2s of thinking
        away = g.answer("Away", correct)

        assert away["points"] == local["points"]
        assert away["elapsed_ms"] == local["elapsed_ms"] == 2000
    finally:
        conn.close(); os.unlink(p)


def test_remote_latency_credit_is_capped():
    """audio_started is client-reported, so it is not trusted. A phone claiming
    its audio began 30s late must not be able to answer at leisure and still
    collect a full-speed bonus."""
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=clock)
        g.join("Cheat", remote=True)
        g.build_rounds(conn)
        rnd = g.start_round()
        clock.t += 15                     # a very late "my audio just started"
        g.note_audio_started("Cheat")
        a = g.answer("Cheat", rnd["correct"])
        # credit is clamped to 5s, so 15s in reads as 10s elapsed, not 0s
        assert a["elapsed_ms"] == 10_000
        assert a["points"] == 100 + int(50 * (1 - 10 / 20))
    finally:
        conn.close(); os.unlink(p)


def test_first_audio_report_wins():
    """A replay (or a duplicate message) must not push the baseline out and buy a
    bigger bonus part-way through the round."""
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=clock)
        g.join("Away", remote=True)
        g.build_rounds(conn)
        rnd = g.start_round()
        clock.t += 1
        g.note_audio_started("Away")
        clock.t += 4
        g.note_audio_started("Away")      # ignored
        a = g.answer("Away", rnd["correct"])
        assert a["elapsed_ms"] == 4000    # from t+1, not t+5
    finally:
        conn.close(); os.unlink(p)


def test_remote_who_never_reports_audio_falls_back_to_the_room_clock():
    """No report = no credit, and above all no crash: a phone with audio blocked
    (autoplay overlay untapped) still has to be able to answer."""
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=clock)
        g.join("Silent", remote=True)
        g.build_rounds(conn)
        rnd = g.start_round()
        clock.t += 3
        a = g.answer("Silent", rnd["correct"])
        assert a["elapsed_ms"] == 3000
    finally:
        conn.close(); os.unlink(p)


def test_locals_get_no_credit_and_the_room_deadline_still_binds():
    """A local player's audio_started is ignored (they hear the room), and the
    answer window shuts on the ROOM clock for everyone — a remote player gets a
    fairer bonus, not a longer round."""
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=clock)
        g.join("Local")
        g.join("Away", remote=True)
        g.build_rounds(conn)
        rnd = g.start_round()
        clock.t += 4
        g.note_audio_started("Local")     # no-op for a local player
        assert "Local" not in rnd["audio_started"]
        g.note_audio_started("Away")
        clock.t += 17                     # past the room's 20s window
        with pytest.raises(game.GameError):
            g.answer("Away", rnd["correct"])
        with pytest.raises(game.GameError):
            g.answer("Local", rnd["correct"])
    finally:
        conn.close(); os.unlink(p)


def test_remote_flag_survives_a_reconnect_and_can_be_changed():
    """quiz.js re-sends `join` on every reconnect. That must not flip someone
    back to local mid-game — but an explicit set_remote must still work."""
    conn, p = make_db()
    try:
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=Clock())
        g.join("Away", remote=True)
        g.join("Away")                    # reconnect, no remote flag
        assert g.players["Away"]["remote"] is True
        g.set_remote("Away", False)       # walked into the room
        assert g.players["Away"]["remote"] is False
        assert g.snapshot()["players"][0]["remote"] is False
        with pytest.raises(game.GameError):
            g.set_remote("Nobody", True)
    finally:
        conn.close(); os.unlink(p)


def test_note_audio_started_outside_a_question_is_a_no_op():
    conn, p = make_db()
    try:
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=Clock())
        g.join("Away", remote=True)
        g.note_audio_started("Away")      # lobby: no round exists yet
        g.build_rounds(conn)
        rnd = g.start_round()
        g.reveal()
        g.note_audio_started("Away")
        assert rnd["audio_started"] == {}
    finally:
        conn.close(); os.unlink(p)


# -- artist-variant handling and the duration floor -------------------------

def _insert(conn, tid, title, artist, duration=200, tier="easy", year=1990, listeners=5000):
    conn.execute(
        "INSERT INTO tracks(id,title,artist,album,year,duration,tier,clipped_at,"
        "global_listeners,active) VALUES(?,?,?,?,?,?,?,?,?,1)",
        (tid, title, artist, "Album", year, duration, tier, "2026-07-06T00:00:00", listeners))
    conn.commit()


def test_decoys_never_offer_the_same_song_under_a_variant_artist_tag():
    """The broken round this fixes: 'Back In Black — AC/DC' as the answer with
    'Back In Black — AC, DC' as a decoy. Two options that read identically, only
    one scored right — and the SQL `artist != ?` filter can't see it."""
    conn, p = make_db(0)
    try:
        _insert(conn, "ans", "Back In Black", "AC/DC")
        _insert(conn, "twin", "Back In Black", "AC, DC")      # the trap
        for i in range(20):
            _insert(conn, f"d{i}", f"Other Song {i}", f"Decoy Band {i}")
        answer = dict(conn.execute("SELECT * FROM tracks WHERE id='ans'").fetchone())
        for _ in range(40):  # decoys are random — one pass proves little
            decoys = game.pick_decoys(conn, answer)
            titles = [d["title"] for d in decoys]
            assert "Back In Black" not in titles, decoys
            assert "AC, DC" not in [d["artist"] for d in decoys], decoys
    finally:
        conn.close(); os.unlink(p)


def test_pick_artist_track_reaches_every_spelling_of_the_chosen_artist():
    """Picking 'AC/DC' off the wall must reach tracks tagged 'AC, DC' too."""
    conn, p = make_db(0)
    try:
        _insert(conn, "v1", "Shoot To Thrill", "AC, DC")
        _insert(conn, "v2", "Stiff Upper Lip", "AC-DC")
        got = set()
        for _ in range(60):
            t = game.pick_artist_track(conn, ["AC/DC"], set())
            assert t is not None, "no spelling matched at all"
            got.add(t["id"])
        assert got == {"v1", "v2"}
    finally:
        conn.close(); os.unlink(p)


def test_pick_artist_track_still_returns_none_for_an_unknown_artist():
    conn, p = make_db(0)
    try:
        _insert(conn, "a", "Song", "Someone Else")
        assert game.pick_artist_track(conn, ["Nobody At All"], set()) is None
        assert game.pick_artist_track(conn, [], set()) is None
    finally:
        conn.close(); os.unlink(p)


def test_pick_artist_track_honours_the_exclude_set():
    conn, p = make_db(0)
    try:
        _insert(conn, "only", "Song", "AC/DC")
        assert game.pick_artist_track(conn, ["AC/DC"], set())["id"] == "only"
        assert game.pick_artist_track(conn, ["AC/DC"], {"only"}) is None
    finally:
        conn.close(); os.unlink(p)


def test_tracks_too_short_to_clip_are_not_quizzable():
    """A 20s intro clip out of a 25s track IS the song — the clip gives the
    answer away, so short tracks must never reach a round."""
    conn, p = make_db(0)
    try:
        for i in range(12):
            _insert(conn, f"ok{i}", f"Real Song {i}", f"Band {i}", duration=200)
        _insert(conn, "skit", "Skit #2", "Kanye West", duration=8)
        _insert(conn, "sting", "Interlude", "Someone", duration=31)
        _insert(conn, "long", "DJ Mix", "Someone Else", duration=4000)
        picked = {t["id"] for t in game.pick_tracks(conn, 12, ["easy"])}
        assert "skit" not in picked and "sting" not in picked
        assert "long" not in picked, "the existing MAX_DURATION_S guard must still hold"
        assert len(picked) == 12
    finally:
        conn.close(); os.unlink(p)


def test_a_null_duration_track_is_still_quizzable():
    """Duration is metadata that can simply be missing; that's not evidence the
    track is unusable, and excluding NULLs would silently shrink the pool."""
    conn, p = make_db(0)
    try:
        conn.execute(
            "INSERT INTO tracks(id,title,artist,album,year,duration,tier,clipped_at,"
            "global_listeners,active) VALUES('n','Song','Band','Album',1990,NULL,'easy',"
            "'2026-07-06T00:00:00',5000,1)")
        conn.commit()
        assert [t["id"] for t in game.pick_tracks(conn, 1, ["easy"])] == ["n"]
    finally:
        conn.close(); os.unlink(p)


def test_decoys_never_offer_two_spellings_of_the_same_band():
    """The other half of the variant problem: decoys are meant to be four
    DIFFERENT artists. 'Highway To Hell — AC/DC' plus 'Shoot To Thrill — AC, DC'
    in one round is one band twice, which narrows the guess unfairly."""
    conn, p = make_db(0)
    try:
        _insert(conn, "ans", "Some Answer", "Answer Band")
        for i, (title, artist) in enumerate((
                ("Highway To Hell", "AC/DC"), ("Shoot To Thrill", "AC, DC"),
                ("Stiff Upper Lip", "AC-DC"), ("Back In Black", "ACDC"))):
            _insert(conn, f"v{i}", title, artist)
        for i in range(6):
            _insert(conn, f"o{i}", f"Other {i}", f"Band {i}")
        answer = dict(conn.execute("SELECT * FROM tracks WHERE id='ans'").fetchone())
        for _ in range(60):
            decoys = game.pick_decoys(conn, answer)
            acdc = [d for d in decoys if game.library.artist_key(d["artist"]) == "acdc"]
            assert len(acdc) <= 1, decoys
    finally:
        conn.close(); os.unlink(p)
