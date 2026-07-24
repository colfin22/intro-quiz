"""Maintenance job registry for the admin page (#52).

One background job at a time, globally: every action contends for the same
sqlite DB (and clips for ffmpeg/CPU), and the full pipeline already chains
most of them — parallel runs would just fight. Status is in-memory only;
after a restart the page honestly says "no runs since restart".
"""
import logging
import threading
import time
from typing import Callable

from . import config

LOGGER = logging.getLogger(__name__)

LOG_LINES_MAX = 100

ACTIONS: dict[str, dict] = {}   # name -> {"label": str, "run": callable}
JOBS: dict[str, dict] = {}      # name -> status dict (see _fresh_status)
_LOCK = threading.Lock()
_CURRENT: list = [None]         # [name] of the running job, or [None]


def register(name: str, label: str, run: Callable[[Callable[[str], None]], dict]) -> None:
    """`run` receives a set_stage(str) callback and returns a summary dict."""
    ACTIONS[name] = {"label": label, "run": run}


def check_token(header_value: str) -> bool:
    """Unset ADMIN_PASSWORD = open (existing cron curls keep working)."""
    return not config.ADMIN_PASSWORD or header_value == config.ADMIN_PASSWORD


class RunLogHandler(logging.Handler):
    """Captures the run's log lines for the admin page. Attached to the root
    logger only while a job runs — safe because jobs never overlap."""

    def __init__(self, sink: list):
        super().__init__(level=logging.INFO)
        self.sink = sink
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                            datefmt="%H:%M:%S"))

    def emit(self, record):
        if len(self.sink) < LOG_LINES_MAX:
            try:
                self.sink.append(self.format(record))
            except Exception:  # noqa: BLE001 — logging must never break the job
                pass
        elif len(self.sink) == LOG_LINES_MAX:
            self.sink.append("… (output capped)")


def _fresh_status() -> dict:
    return {"running": True, "stage": "starting", "started_at": time.time(),
            "finished_at": None, "summary": None, "error": None, "log": []}


def start(name: str) -> tuple[bool, dict]:
    """Start a job. Returns (started, status) — status is the RUNNING job's
    record when refused, so the caller can say who's hogging the server."""
    if name not in ACTIONS:
        raise KeyError(name)
    with _LOCK:
        if _CURRENT[0] is not None:
            return False, {"busy_with": _CURRENT[0], **JOBS.get(_CURRENT[0], {})}
        _CURRENT[0] = name
        JOBS[name] = _fresh_status()
    threading.Thread(target=_run_wrapped, args=(name,), daemon=True).start()
    return True, JOBS[name]


def _run_wrapped(name: str) -> None:
    job = JOBS[name]

    def set_stage(stage: str) -> None:
        job["stage"] = stage
        LOGGER.info("%s: %s", name, stage)

    handler = RunLogHandler(job["log"])
    root = logging.getLogger()
    old_level = root.level
    if root.getEffectiveLevel() > logging.INFO:
        root.setLevel(logging.INFO)  # capture INFO progress lines even if root is quieter
    root.addHandler(handler)
    try:
        job["summary"] = ACTIONS[name]["run"](set_stage)
        job["stage"] = "done"
    except Exception as e:  # noqa: BLE001 — status must always resolve
        LOGGER.exception("%s failed", name)
        job["error"] = str(e)
        job["stage"] = "failed"
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
        job["running"] = False
        job["finished_at"] = time.time()
        with _LOCK:
            _CURRENT[0] = None


def status() -> dict:
    return {"current": _CURRENT[0],
            "actions": {n: {"label": a["label"]} for n, a in ACTIONS.items()},
            "jobs": {n: dict(j) for n, j in JOBS.items()}}


def bootstrap_compat() -> dict:
    """The old BOOTSTRAP dict shape for /health — external watchers rely on it."""
    j = JOBS.get("bootstrap")
    if not j:
        return {}
    out = {"running": j["running"], "stage": j["stage"]}
    if j["error"]:
        out["error"] = j["error"]
    for k, v in (j["summary"] or {}).items():
        out[k] = v
    return out
