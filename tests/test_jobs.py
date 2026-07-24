import logging
import os
import subprocess
import time

import pytest

from app import config, jobs


@pytest.fixture(autouse=True)
def clean_registry():
    jobs.ACTIONS.clear()
    jobs.JOBS.clear()
    jobs._CURRENT[0] = None
    yield
    jobs.ACTIONS.clear()
    jobs.JOBS.clear()
    jobs._CURRENT[0] = None


def wait_done(name, timeout=5.0):
    t0 = time.time()
    while jobs.JOBS[name]["running"]:
        assert time.time() - t0 < timeout, f"{name} never finished"
        time.sleep(0.01)


def test_run_records_summary_and_times():
    jobs.register("ok", "OK", lambda set_stage: {"n": 3})
    started, st = jobs.start("ok")
    assert started
    wait_done("ok")
    j = jobs.JOBS["ok"]
    assert j["summary"] == {"n": 3}
    assert j["error"] is None
    assert j["stage"] == "done"
    assert j["finished_at"] >= j["started_at"]


def test_error_recorded_and_guard_released():
    def boom(set_stage):
        raise RuntimeError("kaput")
    jobs.register("bad", "Bad", boom)
    jobs.register("ok", "OK", lambda set_stage: {})
    started, _ = jobs.start("bad")
    assert started
    wait_done("bad")
    assert jobs.JOBS["bad"]["error"] == "kaput"
    assert jobs.JOBS["bad"]["stage"] == "failed"
    # the failure must release the global guard
    started, _ = jobs.start("ok")
    assert started
    wait_done("ok")


def test_global_one_at_a_time():
    import threading
    gate = threading.Event()
    jobs.register("slow", "Slow", lambda set_stage: (gate.wait(3), {})[1])
    jobs.register("other", "Other", lambda set_stage: {})
    started, _ = jobs.start("slow")
    assert started
    started, st = jobs.start("other")
    assert not started
    assert st["busy_with"] == "slow"
    gate.set()
    wait_done("slow")
    started, _ = jobs.start("other")
    assert started
    wait_done("other")


def test_unknown_action_raises():
    with pytest.raises(KeyError):
        jobs.start("nope")


def test_log_capture_attach_and_detach():
    def chatty(set_stage):
        set_stage("working")
        logging.getLogger("app.something").info("hello from the job")
        return {}
    jobs.register("chatty", "Chatty", chatty)
    jobs.start("chatty")
    wait_done("chatty")
    log = "\n".join(jobs.JOBS["chatty"]["log"])
    assert "hello from the job" in log
    assert "working" in log
    # handler detached: nothing new lands after the run
    n = len(jobs.JOBS["chatty"]["log"])
    logging.getLogger("app.something").info("after the run")
    assert len(jobs.JOBS["chatty"]["log"]) == n


def test_log_capped():
    def noisy(set_stage):
        for i in range(300):
            logging.getLogger("app.n").info("line %d", i)
        return {}
    jobs.register("noisy", "Noisy", noisy)
    jobs.start("noisy")
    wait_done("noisy")
    assert len(jobs.JOBS["noisy"]["log"]) == jobs.LOG_LINES_MAX + 1
    assert jobs.JOBS["noisy"]["log"][-1] == "… (output capped)"


def test_check_token(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    assert jobs.check_token("")
    assert jobs.check_token("anything")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "s3cret")
    assert jobs.check_token("s3cret")
    assert not jobs.check_token("")
    assert not jobs.check_token("wrong")


def test_bootstrap_compat_shape():
    jobs.register("bootstrap", "Full pipeline",
                  lambda set_stage: {"tracks_synced": 5, "clips_cut": 2})
    assert jobs.bootstrap_compat() == {}  # no run yet -> /health omits the key
    jobs.start("bootstrap")
    wait_done("bootstrap")
    compat = jobs.bootstrap_compat()
    assert compat["running"] is False
    assert compat["stage"] == "done"
    assert compat["tracks_synced"] == 5
    assert compat["clips_cut"] == 2
    assert "error" not in compat


def test_admin_smoke_js():
    """Drive admin.js in the stub DOM against idle/running/failed snapshots."""
    if not __import__("shutil").which("node"):
        if os.environ.get("CI"):
            pytest.fail("node required in CI — the admin render smoke must not silently skip")
        pytest.skip("node not installed")
    root = os.path.dirname(os.path.dirname(__file__))
    r = subprocess.run(["node", os.path.join(root, "tests", "js", "admin_smoke.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"admin smoke failed:\n{r.stdout}\n{r.stderr}"


def test_trivia_stats_and_reset_semantics(tmp_path):
    from app import db as adb, main
    conn = adb.connect(str(tmp_path / "t.db"))
    conn.executemany(
        "INSERT INTO trivia(kind, text, answer, source, used_at) VALUES(?,?,?,'seed',?)",
        [("fact", "f1", None, "2026-07-01"), ("fact", "f2", None, None),
         ("tf", "q1", 1, "2026-07-02"), ("tf", "q2", 0, None), ("tf", "q3", 1, None)])
    conn.commit()
    s = main._trivia_stats(conn)
    assert s["fact"] == {"total": 2, "played": 1, "left": 1}
    assert s["tf"] == {"total": 3, "played": 1, "left": 2}
    # the /admin reset: everything fresh, nothing deleted
    n = conn.execute("UPDATE trivia SET used_at=NULL WHERE used_at IS NOT NULL").rowcount
    conn.commit()
    assert n == 2
    s = main._trivia_stats(conn)
    assert s["fact"]["left"] == 2 and s["tf"]["left"] == 3
    assert s["fact"]["total"] == 2 and s["tf"]["total"] == 3


def test_llm_prompt_parameterised():
    from app import trivia
    p = trivia.llm_prompt("Canada", facts=10, tf=20)
    assert "10 facts and 20 true/false" in p
    assert "Canada" in p
    assert "<YOUR REGION" in trivia.llm_prompt("")  # blank keeps the placeholder
    assert "500 facts" in trivia.llm_prompt("X", facts=9999)  # clamped


def test_parse_pack_tolerates_llm_wrapping():
    from app import trivia
    items = [{"kind": "fact", "text": "A."}, {"kind": "tf", "text": "B?", "answer": 0}]
    import json as j
    raw = j.dumps(items)
    assert trivia.parse_pack(raw) == items
    assert trivia.parse_pack(f"Sure! Here you go:\n```json\n{raw}\n```\nEnjoy!") == items
    with pytest.raises(ValueError):
        trivia.parse_pack("no json here")
    with pytest.raises(ValueError):
        trivia.parse_pack('{"kind": "fact"}')  # object, not a list
    with pytest.raises(ValueError):
        trivia.parse_pack("")


def test_insert_items_reports_and_is_idempotent(tmp_path):
    from app import db as adb, trivia
    conn = adb.connect(str(tmp_path / "t.db"))
    pack = [{"kind": "fact", "text": "A."},
            {"kind": "tf", "text": "B?", "answer": 1},
            {"kind": "tf", "text": "no answer"},          # reject
            {"kind": "nope", "text": "bad kind"},          # reject
            "not an object"]                               # reject
    r = trivia.insert_items(conn, pack, "import")
    assert r["added"] == 2 and r["duplicates"] == 0 and len(r["rejects"]) == 3
    # imported items are never-played and source=import
    rows = conn.execute("SELECT source, used_at FROM trivia").fetchall()
    assert all(x["source"] == "import" and x["used_at"] is None for x in rows)
    # re-import: everything is a duplicate, nothing added
    r = trivia.insert_items(conn, pack[:2], "import")
    assert r["added"] == 0 and r["duplicates"] == 2
