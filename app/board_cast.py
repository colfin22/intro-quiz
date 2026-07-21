"""Cast the /board page to a Google display via DashCast.

Runs in an executor thread (pychromecast is blocking). The display drops
back to ambient mode when the game finishes.
"""
import logging
import os
import threading

# DISPLAYS: comma-separated "Name=ip" pairs; first entry is the default.
# Legacy DISPLAY_HOST is honoured as a single unnamed display.
_raw = os.environ.get("DISPLAYS", "")
DISPLAYS: dict[str, str] = {}
for part in _raw.split(","):
    if "=" in part:
        name, ip = part.split("=", 1)
        DISPLAYS[name.strip()] = ip.strip()
if not DISPLAYS and os.environ.get("DISPLAY_HOST"):
    DISPLAYS["Display"] = os.environ["DISPLAY_HOST"]
BOARD_URL = os.environ.get("BOARD_URL", "")
# CAST_APP_ID: our own registered Cast receiver (#32 trial). When set, the board is
# cast via our receiver shell (static/receiver.html) instead of DashCast; unset means
# DashCast exactly as before — the flag exists so the trial is one env var away from
# rollback either direction.
CAST_APP_ID = os.environ.get("CAST_APP_ID", "")

LOGGER = logging.getLogger(__name__)
_lock = threading.Lock()
_casts: dict[str, object] = {}


def display_names() -> list[str]:
    return list(DISPLAYS)


def configured(name: str | None) -> bool:
    return bool(name and name in DISPLAYS and BOARD_URL)


def _connect(name: str):
    import pychromecast
    if name not in _casts:
        # pychromecast 14: connect by host tuple (ip, port, uuid, model, name)
        _casts[name] = pychromecast.get_chromecast_from_host(
            (DISPLAYS[name], 8009, None, name, name))
    _casts[name].wait(timeout=15)
    return _casts[name]


def _show_via_own_receiver(cast) -> bool:
    """Launch our registered receiver app and tell it which board URL to frame.

    No pre-emptive quit and no freshness dance: the shell never navigates, so it
    should not inherit DashCast's session-age rot (#35). Re-sending "load" to a
    running shell just re-points the iframe.
    """
    import threading as _th
    from pychromecast.controllers import BaseController

    class _Shell(BaseController):
        def __init__(self):
            super().__init__("urn:x-cast:com.introquiz.board", CAST_APP_ID)

        def receive_message(self, _message, _data) -> bool:
            return True

    shell = _Shell()
    cast.register_handler(shell)
    done = _th.Event()
    result = {"ok": False}

    def _sent(ok: bool, _resp) -> None:
        result["ok"] = ok
        done.set()

    def _launched(ok: bool, _resp) -> None:
        if not ok:
            done.set()
            return
        shell.send_message({"type": "load", "url": BOARD_URL},
                           inc_session_id=True, callback_function=_sent)

    shell.launch(callback_function=_launched)
    done.wait(timeout=15)
    cast.unregister_handler(shell)
    return result["ok"]


def show_board(name: str | None, fresh: bool = False) -> bool:
    """Cast the board. fresh=True quits the receiver app first so every game
    starts with a brand-new DashCast instance — the receiver degrades with
    session age and reliably crashed mid-game-2 when reused (#28)."""
    if not configured(name):
        LOGGER.info("board cast skipped (no display selected)")
        return False
    try:
        with _lock:
            import time as _t
            from pychromecast.controllers.dashcast import DashCastController
            cast = _connect(name)
            if CAST_APP_ID:
                ok = _show_via_own_receiver(cast)
                LOGGER.info("board cast to %s (%s) via own receiver: %s",
                            name, DISPLAYS[name], "ok" if ok else "FAILED")
                return ok
            if fresh:
                try:
                    cast.quit_app()
                    _t.sleep(1)  # a beat to tear down (was 2s — #29 speed-up)
                    cast.wait(timeout=15)
                except Exception as e:  # noqa: BLE001 - nothing running is fine
                    LOGGER.info("pre-cast quit skipped: %s", e)
            dc = DashCastController()
            cast.register_handler(dc)
            dc.load_url(BOARD_URL, force=True)  # top-level load: iframe autoplay policy blocks board audio
        LOGGER.info("board cast to %s (%s)%s", name, DISPLAYS[name], " [fresh]" if fresh else "")
        return True
    except Exception as e:  # noqa: BLE001 - board is cosmetic, never break the game
        LOGGER.error("board cast failed: %s", e)
        return False


def hide_board(name: str | None) -> bool:
    if not configured(name):
        return False
    try:
        with _lock:
            cast = _connect(name)
            cast.quit_app()
        return True
    except Exception as e:  # noqa: BLE001
        LOGGER.error("board hide failed: %s", e)
        return False
