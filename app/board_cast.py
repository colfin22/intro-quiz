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
_shells: dict[str, object] = {}            # persistent receiver-app handler per display
_keepers: dict[str, threading.Event] = {}  # stop-event for each display's keepalive thread


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


def _make_shell():
    from pychromecast.controllers import BaseController

    class _Shell(BaseController):
        def __init__(self):
            super().__init__("urn:x-cast:com.introquiz.board", CAST_APP_ID)

        def receive_message(self, _message, _data) -> bool:
            return True

    return _Shell()


def _keepalive(name: str, stop_ev: threading.Event) -> None:
    """Hold a live Cast sender on the receiver so the platform never idle-reaps it.

    Proven root cause (#47, adb logcat): the board's clips play in an <audio> the
    CAF media pipeline can't see, so from Cast's view no media is playing; and a
    fire-and-forget launch that unregisters its handler drops the sender within
    ~10s (the transport's max_inactivity). With no media AND no sender, Cast stops
    the app 'In.Idle' ~5 min later — the deaths we chased for weeks. So we keep the
    handler registered and ping it every few seconds: a live, active sender is
    never idle-reaped. Reconnects itself if the socket blips.
    """
    while not stop_ev.wait(4):
        try:
            with _lock:
                shell = _shells.get(name)
                if shell is None:
                    return  # board was hidden / re-cast — this keeper is retired
                shell.send_message({"type": "ping"}, inc_session_id=True)
        except Exception as e:  # noqa: BLE001 — keepalive must never die
            LOGGER.warning("keepalive ping to %s failed, reconnecting: %s", name, e)
            try:
                with _lock:
                    shell = _shells.get(name)
                    if shell is not None:
                        _connect(name).register_handler(shell)
            except Exception as e2:  # noqa: BLE001
                LOGGER.warning("keepalive reconnect to %s failed: %s", name, e2)


def _start_keepalive(name: str) -> None:
    old = _keepers.get(name)
    if old is not None:
        old.set()  # retire any previous keeper for this display
    stop_ev = threading.Event()
    _keepers[name] = stop_ev
    threading.Thread(target=_keepalive, args=(name, stop_ev), daemon=True).start()


def _show_via_own_receiver(cast, name: str) -> bool:
    """Launch our registered receiver app and keep a live sender on it.

    The shell (static/receiver.html) self-loads /board from the same origin, so no
    "load" message is needed. What IS needed is to NOT unregister the handler and
    to keep pinging it — otherwise Cast idle-reaps the receiver (#47).
    """
    import threading as _th

    shell = _make_shell()
    cast.register_handler(shell)
    done = _th.Event()
    result = {"ok": False}

    def _launched(ok: bool, resp) -> None:
        result["ok"] = ok
        if not ok:
            LOGGER.warning("own receiver: launch failed: %s", resp)
        else:
            LOGGER.warning("own receiver: launched, holding sender open")
        done.set()

    shell.launch(callback_function=_launched)
    done.wait(timeout=15)
    if result["ok"]:
        _shells[name] = shell          # keep the handler alive (do NOT unregister)
        _start_keepalive(name)
    else:
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
                ok = _show_via_own_receiver(cast, name)
                LOGGER.warning("board cast to %s (%s) via own receiver: %s",
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
            keeper = _keepers.pop(name, None)
            if keeper is not None:
                keeper.set()          # stop the keepalive — we WANT it idle now
            _shells.pop(name, None)
            cast = _connect(name)
            cast.quit_app()
        return True
    except Exception as e:  # noqa: BLE001
        LOGGER.error("board hide failed: %s", e)
        return False
