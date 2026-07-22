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


def show_board(name: str | None, fresh: bool = False) -> bool:
    """Cast the board via DashCast. Top-level load (force=True) so the board's
    audio isn't muted by the iframe autoplay policy. fresh=True quits first."""
    if not configured(name):
        LOGGER.info("board cast skipped (no display selected)")
        return False
    try:
        with _lock:
            import time as _t
            from pychromecast.controllers.dashcast import DashCastController
            cast = _connect(name)
            if fresh:
                try:
                    cast.quit_app()
                    _t.sleep(1)
                    cast.wait(timeout=15)
                except Exception as e:  # noqa: BLE001 - nothing running is fine
                    LOGGER.info("pre-cast quit skipped: %s", e)
            dc = DashCastController()
            cast.register_handler(dc)
            dc.load_url(BOARD_URL, force=True)
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
