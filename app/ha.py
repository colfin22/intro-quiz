"""Home Assistant integration: cast clips to a speaker, and group speakers.

Two things to keep straight, because getting them backwards fails silently:

- **Playback** goes through `music_assistant.play_media` on a **Music Assistant**
  entity (`..._music_assistant`, or the `_2` twin). Casting a plain URL at the
  native entity gives a chime and no audio.
- **Grouping** goes through `media_player.join`/`unjoin` on the **native** entity —
  the one that actually reports `group_members`. MA entities report an empty list
  and joining them does nothing.

The playback target is resolved at CALL time (`resolve_target`), so the /admin
Speakers tab can change it without a container restart. `MEDIA_PLAYER` from .env
is the fallback default.
"""
import logging
import os

import httpx

HA_URL = os.environ.get("HA_URL", "").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
MEDIA_PLAYER = os.environ.get("MEDIA_PLAYER", "")
MEDIA_VOLUME = float(os.environ.get("MEDIA_VOLUME", "0.45"))
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")
CAST_ENABLED = os.environ.get("CAST_ENABLED", "true").lower() == "true"
NOTIFY_SERVICE = os.environ.get("HA_NOTIFY_SERVICE", "")  # e.g. notify.mobile_app_myphone

# HA's MediaPlayerEntityFeature.GROUPING — speakers that can be joined into a
# multi-room group. Everything else in the house (TVs, Plex clients) lacks it.
FEATURE_GROUPING = 524288
# DB keys for the admin overrides (see db.get_setting/set_setting)
SETTING_TARGET = "speaker_target"
SETTING_GROUP = "speaker_group"

LOGGER = logging.getLogger(__name__)


def _call(service: str, data: dict) -> None:
    domain, name = service.split(".")
    r = httpx.post(f"{HA_URL}/api/services/{domain}/{name}",
                   headers={"Authorization": f"Bearer {HA_TOKEN}"},
                   json=data, timeout=30)
    r.raise_for_status()


def _get(path: str, timeout: int = 10):
    r = httpx.get(f"{HA_URL}{path}",
                  headers={"Authorization": f"Bearer {HA_TOKEN}"}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def available() -> bool:
    """Enough config to talk to HA at all (weaker than configured())."""
    return bool(HA_URL and HA_TOKEN)


def resolve_target(conn=None) -> str:
    """The entity clips play on: the admin override if set, else .env.

    Read per call rather than cached, so a change on the Speakers tab takes
    effect on the next round instead of the next restart.
    """
    if conn is not None:
        from . import db
        try:
            chosen = db.get_setting(conn, SETTING_TARGET)
            if chosen:
                return chosen
        except Exception as e:  # noqa: BLE001 — a DB hiccup must not mute the game
            LOGGER.warning("speaker override unreadable, using MEDIA_PLAYER: %s", e)
    return MEDIA_PLAYER


def _with_conn(fn):
    """Run fn(conn) on a short-lived DB connection. The HA helpers are called
    from an executor thread, and sqlite connections aren't shareable across
    threads, so each call opens its own."""
    from . import db
    conn = db.connect()
    try:
        return fn(conn)
    finally:
        conn.close()


def configured() -> bool:
    """Enough config to cast. The speaker itself is resolved per call, so it isn't
    part of this check — a target chosen on the Speakers tab counts too."""
    return bool(HA_URL and HA_TOKEN and APP_BASE_URL)


def play_clip(track_id: str, kind: str) -> bool:
    """kind: '5', '10', '20' or 'payoff'. Returns whether a cast happened."""
    return play_url(f"{APP_BASE_URL}/clips/{track_id}/{kind}.mp3", f"{track_id}/{kind}")


def play_url(url: str, label: str = "", target: str | None = None) -> bool:
    if not (CAST_ENABLED and configured()):
        LOGGER.info("cast skipped (disabled/unconfigured): %s", label or url)
        return False
    entity = target or _with_conn(lambda c: resolve_target(c))
    if not entity:
        LOGGER.info("cast skipped (no speaker chosen): %s", label or url)
        return False
    try:
        _call("media_player.volume_set",
              {"entity_id": entity, "volume_level": MEDIA_VOLUME})
        # Music Assistant streams the URL through its own pipeline — direct
        # casting of plain-HTTP URLs silently fails on these speakers
        # (chime, no audio; found 07-07-2026). Target the _ma entities.
        _call("music_assistant.play_media", {
            "entity_id": entity,
            "media_id": url,
            "media_type": "track"})
        return True
    except httpx.HTTPError as e:
        LOGGER.error("cast to %s failed: %s", entity, e)
        return False


# --- speaker discovery and grouping ----------------------------------------

def _ma_entities() -> set[str]:
    """Which media_players belong to the Music Assistant integration — asked of HA,
    not guessed from the entity_id.

    The naming is not a usable signal in a real house. MA entities turn up as
    `_music_assistant`, as `_2`, and (when MA wins the name race) as a plain
    `media_player.bathroom`; meanwhile a native Sonos speaker can itself end up as
    `..._2`. Probed live against 20 entities, a suffix guess got 7 wrong — including
    promoting a native speaker into the "plays audio" list, which is the
    chime-and-no-music failure this module exists to avoid.

    Returns an empty set if the template API can't be reached, which callers read as
    "unknown" and fall back to the suffix guess.
    """
    try:
        r = httpx.post(f"{HA_URL}/api/template",
                       headers={"Authorization": f"Bearer {HA_TOKEN}"},
                       json={"template":
                             "{{ integration_entities('music_assistant') | join(',') }}"},
                       timeout=10)
        r.raise_for_status()
        return {e for e in r.text.split(",") if e.startswith("media_player.")}
    except httpx.HTTPError as e:
        LOGGER.warning("could not ask HA which entities are Music Assistant's: %s", e)
        return set()


def list_players() -> list[dict]:
    """Every usable media_player in HA, for the /admin Speakers dropdown.

    Filters the noise a real house accumulates: `unavailable`/`unknown` entities
    and the ~40 `plex_*` clients that exist only while something is casting.
    """
    if not available():
        return []
    ma = _ma_entities()
    out = []
    for st in _get("/api/states"):
        eid = st.get("entity_id", "")
        if not eid.startswith("media_player."):
            continue
        if st.get("state") in ("unavailable", "unknown"):
            continue
        if eid.startswith("media_player.plex_"):
            continue
        attrs = st.get("attributes") or {}
        members = attrs.get("group_members") or []
        out.append({
            "entity_id": eid,
            "name": attrs.get("friendly_name") or eid.split(".", 1)[1].replace("_", " "),
            "state": st.get("state"),
            # can be joined into a multi-room group — and actually reports members,
            # which is what distinguishes a native entity from its MA twin
            "can_group": bool(int(attrs.get("supported_features") or 0) & FEATURE_GROUPING)
                         and bool(members),
            # MA entities are the ones that can play a URL for us. HA's own answer
            # when we have it; the suffix guess only as a fallback (see _ma_entities).
            "is_ma": eid in ma if ma else
                     (eid.endswith("_music_assistant") or eid.endswith("_2")),
            "group_members": list(members),
            "volume": attrs.get("volume_level"),
        })
    out.sort(key=lambda p: p["name"].lower())
    return out


def group_state(entities: list[str]) -> dict[str, list[str]]:
    """Current group membership, so game-start grouping can be undone afterwards.

    Best-effort: an entity we can't read just isn't restored, which is better
    than refusing to group at all.
    """
    if not (available() and entities):
        return {}
    want = set(entities)
    snap: dict[str, list[str]] = {}
    try:
        for st in _get("/api/states"):
            if st.get("entity_id") in want:
                snap[st["entity_id"]] = list((st.get("attributes") or {}).get("group_members") or [])
    except httpx.HTTPError as e:
        LOGGER.warning("could not snapshot group state: %s", e)
    return snap


def join_group(leader: str, members: list[str]) -> bool:
    """Join `members` to `leader` — native entities, not the MA twins."""
    others = [m for m in members if m != leader]
    if not (available() and leader and others):
        return False
    try:
        _call("media_player.join", {"entity_id": leader, "group_members": others})
        LOGGER.info("grouped %s -> %s", ", ".join(others), leader)
        return True
    except httpx.HTTPError as e:
        LOGGER.error("grouping %s failed: %s", leader, e)
        return False


def unjoin_group(members: list[str]) -> bool:
    """Drop `members` out of whatever group they're in."""
    if not (available() and members):
        return False
    try:
        _call("media_player.unjoin", {"entity_id": members})
        LOGGER.info("ungrouped %s", ", ".join(members))
        return True
    except httpx.HTTPError as e:
        LOGGER.error("ungrouping failed: %s", e)
        return False


def restore_groups(snap: dict[str, list[str]]) -> None:
    """Put grouping back as group_state() found it. Never raises — the game is
    over by the time this runs, and a failure here must not surface as an error."""
    if not (available() and snap):
        return
    try:
        # first break up everything we touched, then rebuild what was there
        unjoin_group(list(snap))
        for leader, members in snap.items():
            others = [m for m in members if m != leader]
            if others:
                join_group(leader, others)
    except Exception as e:  # noqa: BLE001 — cleanup is strictly best-effort
        LOGGER.warning("restoring speaker groups failed: %s", e)


def notify(title: str, message: str) -> bool:
    """Push a notification via HA. Opt-in: needs HA_NOTIFY_SERVICE set."""
    if not (HA_URL and HA_TOKEN and NOTIFY_SERVICE):
        return False
    try:
        _call(NOTIFY_SERVICE, {"title": title, "message": message})
        return True
    except httpx.HTTPError as e:
        LOGGER.error("notify failed: %s", e)
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
