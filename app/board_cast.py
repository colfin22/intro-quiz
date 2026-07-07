"""Cast the /board page to a Google display via DashCast.

Runs in an executor thread (pychromecast is blocking). The display drops
back to ambient mode when the game finishes.
"""
import logging
import os
import threading

DISPLAY_HOST = os.environ.get("DISPLAY_HOST", "")
BOARD_URL = os.environ.get("BOARD_URL", "")

LOGGER = logging.getLogger(__name__)
_lock = threading.Lock()
_cast = None


def configured() -> bool:
    return bool(DISPLAY_HOST and BOARD_URL)


def _connect():
    global _cast
    import pychromecast
    if _cast is None:
        # pychromecast 14: connect by host tuple (ip, port, uuid, model, name)
        _cast = pychromecast.get_chromecast_from_host(
            (DISPLAY_HOST, 8009, None, "Kitchen Display", "Kitchen Display"))
    _cast.wait(timeout=15)
    return _cast


def show_board() -> bool:
    if not configured():
        LOGGER.info("board cast skipped (unconfigured)")
        return False
    try:
        with _lock:
            from pychromecast.controllers.dashcast import DashCastController
            cast = _connect()
            dc = DashCastController()
            cast.register_handler(dc)
            dc.load_url(BOARD_URL, force=True)  # top-level load: iframe autoplay policy blocks board audio
        LOGGER.info("board cast to %s", DISPLAY_HOST)
        return True
    except Exception as e:  # noqa: BLE001 - board is cosmetic, never break the game
        LOGGER.error("board cast failed: %s", e)
        return False


def hide_board() -> bool:
    if not configured():
        return False
    try:
        with _lock:
            cast = _connect()
            cast.quit_app()
        return True
    except Exception as e:  # noqa: BLE001
        LOGGER.error("board hide failed: %s", e)
        return False
