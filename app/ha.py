"""Home Assistant integration: cast clips to the kitchen speaker."""
import logging
import os

import httpx

HA_URL = os.environ.get("HA_URL", "").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
MEDIA_PLAYER = os.environ.get("MEDIA_PLAYER", "")
MEDIA_VOLUME = float(os.environ.get("MEDIA_VOLUME", "0.45"))
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")
CAST_ENABLED = os.environ.get("CAST_ENABLED", "true").lower() == "true"

LOGGER = logging.getLogger(__name__)


def _call(service: str, data: dict) -> None:
    domain, name = service.split(".")
    r = httpx.post(f"{HA_URL}/api/services/{domain}/{name}",
                   headers={"Authorization": f"Bearer {HA_TOKEN}"},
                   json=data, timeout=10)
    r.raise_for_status()


def configured() -> bool:
    return bool(HA_URL and HA_TOKEN and MEDIA_PLAYER and APP_BASE_URL)


def play_clip(track_id: str, kind: str) -> bool:
    """kind: '5', '10', '20' or 'payoff'. Returns whether a cast happened."""
    if not (CAST_ENABLED and configured()):
        LOGGER.info("cast skipped (disabled/unconfigured): %s/%s", track_id, kind)
        return False
    try:
        _call("media_player.volume_set",
              {"entity_id": MEDIA_PLAYER, "volume_level": MEDIA_VOLUME})
        # Music Assistant streams the URL through its own pipeline — direct
        # casting of plain-HTTP URLs silently fails on these speakers
        # (chime, no audio; found 07-07-2026). Target the _ma entities.
        _call("music_assistant.play_media", {
            "entity_id": MEDIA_PLAYER,
            "media_id": f"{APP_BASE_URL}/clips/{track_id}/{kind}.mp3",
            "media_type": "track"})
        return True
    except httpx.HTTPError as e:
        LOGGER.error("cast failed: %s", e)
        return False


def house_is_sleeping() -> bool:
    if not (HA_URL and HA_TOKEN):
        return False
    try:
        r = httpx.get(f"{HA_URL}/api/states/input_select.house_mode",
                      headers={"Authorization": f"Bearer {HA_TOKEN}"}, timeout=5)
        return r.json().get("state") == "Sleeping"
    except httpx.HTTPError:
        return False
