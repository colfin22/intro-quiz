"""Home Assistant speaker discovery, selection and grouping.

ha.py calls httpx directly (no injectable client), so these fake httpx.get/post
at the module and assert on the *requests made* — the payload shapes are the
whole risk here. HA accepts a wrong-but-well-formed call with a 200 and simply
does nothing, so a test that only checked "it didn't raise" would pass while the
speakers sat silent.
"""
import json
import os
import tempfile

import httpx
import pytest

from app import db, ha


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return db.connect(path), path


# A cut-down slice of the real house: entity PAIRS (a native one that reports
# group_members, and its Music Assistant twin that doesn't), a TV that can't be
# grouped, an unavailable speaker, and the plex_* clients that clutter a real HA.
#
# Two of these deliberately defeat the entity_id suffix guess, because the real
# house does: `media_player.craft_room` is a Music Assistant entity with no telltale
# suffix at all, and `media_player.unnamed_room_2` is a NATIVE Sonos speaker whose id
# ends in `_2`. Naming is not a signal — MA_ENTITIES below is HA's own answer.
STATES = [
    {"entity_id": "media_player.kitchen", "state": "playing",
     "attributes": {"friendly_name": "Kitchen", "supported_features": 1040319,
                    "group_members": ["media_player.kitchen"], "volume_level": 0.3}},
    {"entity_id": "media_player.kitchen_music_assistant", "state": "idle",
     "attributes": {"friendly_name": "Kitchen MA", "supported_features": 1040319,
                    "group_members": [], "volume_level": 0.3}},
    {"entity_id": "media_player.study", "state": "idle",
     "attributes": {"friendly_name": "Study", "supported_features": 1040319,
                    "group_members": ["media_player.study"]}},
    {"entity_id": "media_player.craft_room", "state": "idle",
     "attributes": {"friendly_name": "Craft Room", "supported_features": 1040319,
                    "group_members": []}},
    {"entity_id": "media_player.unnamed_room_2", "state": "idle",
     "attributes": {"friendly_name": "Spare Sonos", "supported_features": 1040319,
                    "group_members": ["media_player.unnamed_room_2"]}},
    {"entity_id": "media_player.living_room_tv", "state": "idle",
     "attributes": {"friendly_name": "Living Room TV", "supported_features": 21389}},
    {"entity_id": "media_player.spare_room", "state": "unavailable",
     "attributes": {"friendly_name": "Spare Room", "supported_features": 1040319}},
    {"entity_id": "media_player.plex_chrome", "state": "idle",
     "attributes": {"friendly_name": "Plex (Chrome)", "supported_features": 1040319,
                    "group_members": ["media_player.plex_chrome"]}},
    {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
]

# What `integration_entities('music_assistant')` returns for the states above.
MA_ENTITIES = ["media_player.kitchen_music_assistant", "media_player.craft_room",
               "button.kitchen_favourite_current_song"]


@pytest.fixture
def ha_env(monkeypatch):
    """Point ha.py at a fake HA and capture every service call it makes."""
    monkeypatch.setattr(ha, "HA_URL", "http://ha.test")
    monkeypatch.setattr(ha, "HA_TOKEN", "tok")
    monkeypatch.setattr(ha, "APP_BASE_URL", "http://quiz.test")
    monkeypatch.setattr(ha, "MEDIA_PLAYER", "media_player.kitchen_music_assistant")
    monkeypatch.setattr(ha, "CAST_ENABLED", True)
    calls = []

    def fake_get(url, **kw):
        assert kw["headers"]["Authorization"] == "Bearer tok"
        if url.endswith("/api/states"):
            return httpx.Response(200, json=STATES, request=httpx.Request("GET", url))
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, **kw):
        if url.endswith("/api/template"):
            # HA renders templates to plain text, not JSON
            assert "music_assistant" in kw["json"]["template"]
            return httpx.Response(200, text=",".join(MA_ENTITIES),
                                  request=httpx.Request("POST", url))
        calls.append((url.rsplit("/api/services/", 1)[-1], kw.get("json")))
        return httpx.Response(200, json=[], request=httpx.Request("POST", url))

    monkeypatch.setattr(ha.httpx, "get", fake_get)
    monkeypatch.setattr(ha.httpx, "post", fake_post)
    return calls


def test_list_players_filters_the_noise(ha_env):
    players = {p["entity_id"]: p for p in ha.list_players()}
    # non-media_player, unavailable and plex_* entities are all dropped
    assert "light.kitchen" not in players
    assert "media_player.spare_room" not in players
    assert "media_player.plex_chrome" not in players
    assert set(players) == {"media_player.kitchen", "media_player.kitchen_music_assistant",
                            "media_player.study", "media_player.craft_room",
                            "media_player.unnamed_room_2", "media_player.living_room_tv"}
    assert players["media_player.kitchen"]["name"] == "Kitchen"
    assert players["media_player.kitchen"]["volume"] == 0.3
    # sorted by display name, so the dropdown isn't in HA's arbitrary order
    assert [p["name"] for p in ha.list_players()] == sorted(
        p["name"] for p in ha.list_players())


def test_group_capability_needs_both_the_feature_bit_and_members(ha_env):
    """The distinction the whole feature turns on: group on the NATIVE entity.
    The MA twin has the GROUPING bit set too but reports no members — joining it
    silently does nothing, which is the exact bug class this guards."""
    players = {p["entity_id"]: p for p in ha.list_players()}
    assert players["media_player.kitchen"]["can_group"] is True
    assert players["media_player.kitchen_music_assistant"]["can_group"] is False
    # a TV lacks the GROUPING bit entirely
    assert players["media_player.living_room_tv"]["can_group"] is False
    # ...and the MA entities are the ones that can play a URL for us
    assert players["media_player.kitchen_music_assistant"]["is_ma"] is True
    assert players["media_player.kitchen"]["is_ma"] is False


def test_ma_entities_come_from_ha_not_from_the_entity_id(ha_env):
    """The entity_id is NOT a reliable signal, and getting this wrong is the
    chime-and-no-audio bug. Probed against the real house, a suffix guess got 7 of 20
    wrong, so HA is asked which entities Music Assistant owns.

    Both cases below defeat the guess: craft_room is MA with no suffix, and
    unnamed_room_2 ends in `_2` while being a native Sonos speaker.
    """
    players = {p["entity_id"]: p for p in ha.list_players()}
    assert players["media_player.craft_room"]["is_ma"] is True
    assert players["media_player.unnamed_room_2"]["is_ma"] is False
    # ...and the native speaker is still the one offered for grouping
    assert players["media_player.unnamed_room_2"]["can_group"] is True
    assert players["media_player.craft_room"]["can_group"] is False


def test_ma_lookup_falls_back_to_the_suffix_when_ha_wont_answer(ha_env, monkeypatch):
    """A dropdown with nothing marked as Music Assistant is worse than one marked by
    the old guess, so an unavailable template API degrades rather than blanks."""
    real_post = ha.httpx.post

    def no_template(url, **kw):
        if url.endswith("/api/template"):
            raise httpx.ConnectError("template API disabled")
        return real_post(url, **kw)
    monkeypatch.setattr(ha.httpx, "post", no_template)
    players = {p["entity_id"]: p for p in ha.list_players()}
    assert players["media_player.kitchen_music_assistant"]["is_ma"] is True
    assert players["media_player.kitchen"]["is_ma"] is False
    # the guess is wrong here — that's the known cost of the fallback, not a crash
    assert players["media_player.craft_room"]["is_ma"] is False


def test_feature_bit_is_the_real_ha_grouping_flag():
    assert ha.FEATURE_GROUPING == 524288
    assert 1040319 & ha.FEATURE_GROUPING          # a Sonos-style speaker
    assert not (21389 & ha.FEATURE_GROUPING)      # a TV


def test_resolve_target_prefers_the_db_over_env(ha_env):
    conn, path = make_db()
    try:
        # nothing saved -> the .env default
        assert ha.resolve_target(conn) == "media_player.kitchen_music_assistant"
        db.set_setting(conn, ha.SETTING_TARGET, "media_player.study_music_assistant")
        assert ha.resolve_target(conn) == "media_player.study_music_assistant"
        # cleared -> back to .env, so the admin page can undo an override
        db.set_setting(conn, ha.SETTING_TARGET, "")
        assert ha.resolve_target(conn) == "media_player.kitchen_music_assistant"
        # no connection at all -> env (the path used before the DB exists)
        assert ha.resolve_target(None) == "media_player.kitchen_music_assistant"
    finally:
        conn.close(); os.unlink(path)


def test_play_url_targets_the_chosen_speaker(ha_env, monkeypatch):
    """The override has to reach the actual play call, not just the admin page."""
    conn, path = make_db()
    try:
        db.set_setting(conn, ha.SETTING_TARGET, "media_player.study_music_assistant")
        monkeypatch.setattr(ha, "_with_conn", lambda fn: fn(conn))
        assert ha.play_url("http://quiz.test/clips/x/5.mp3", "x/5") is True
        services = [c[0] for c in ha_env]
        assert services == ["media_player/volume_set", "music_assistant/play_media"]
        # both calls go to the OVERRIDDEN entity, not MEDIA_PLAYER
        assert all(c[1]["entity_id"] == "media_player.study_music_assistant" for c in ha_env)
        assert ha_env[1][1] == {"entity_id": "media_player.study_music_assistant",
                                "media_id": "http://quiz.test/clips/x/5.mp3",
                                "media_type": "track"}
    finally:
        conn.close(); os.unlink(path)


def test_play_url_explicit_target_wins(ha_env, monkeypatch):
    """The Speakers tab's Test button passes a target directly — it must not need
    to save first to hear which speaker it picked."""
    monkeypatch.setattr(ha, "_with_conn", lambda fn: (_ for _ in ()).throw(
        AssertionError("should not touch the DB when a target is passed")))
    assert ha.play_url("http://quiz.test/static/fanfare.mp3", "test",
                       target="media_player.roam_music_assistant") is True
    assert all(c[1]["entity_id"] == "media_player.roam_music_assistant" for c in ha_env)


def test_join_and_unjoin_payloads(ha_env):
    assert ha.join_group("media_player.kitchen",
                         ["media_player.study", "media_player.kitchen"]) is True
    # HA wants the leader in entity_id and the rest in group_members; the leader
    # must not also appear in its own member list
    assert ha_env == [("media_player/join",
                       {"entity_id": "media_player.kitchen",
                        "group_members": ["media_player.study"]})]
    ha_env.clear()
    assert ha.unjoin_group(["media_player.study", "media_player.kitchen"]) is True
    assert ha_env == [("media_player/unjoin",
                       {"entity_id": ["media_player.study", "media_player.kitchen"]})]


def test_join_group_is_a_no_op_without_other_members(ha_env):
    assert ha.join_group("media_player.kitchen", ["media_player.kitchen"]) is False
    assert ha.join_group("media_player.kitchen", []) is False
    assert ha.unjoin_group([]) is False
    assert ha_env == []


def test_group_state_snapshots_only_what_was_asked_for(ha_env):
    snap = ha.group_state(["media_player.kitchen", "media_player.study",
                           "media_player.nonexistent"])
    assert snap == {"media_player.kitchen": ["media_player.kitchen"],
                    "media_player.study": ["media_player.study"]}


def test_restore_groups_breaks_up_then_rebuilds(ha_env):
    """A game leaves the speakers as it found them: unjoin everything we touched,
    then re-join whatever groups existed before."""
    ha.restore_groups({"media_player.kitchen": ["media_player.kitchen", "media_player.study"],
                       "media_player.study": []})
    assert ha_env[0] == ("media_player/unjoin",
                         {"entity_id": ["media_player.kitchen", "media_player.study"]})
    assert ("media_player/join",
            {"entity_id": "media_player.kitchen",
             "group_members": ["media_player.study"]}) in ha_env


def test_grouping_failures_are_survivable(monkeypatch):
    """An HA outage mid-game must degrade to "no grouping", never to an exception
    that aborts the game or leaves the caller with a broken snapshot."""
    monkeypatch.setattr(ha, "HA_URL", "http://ha.test")
    monkeypatch.setattr(ha, "HA_TOKEN", "tok")

    def boom(*a, **kw):
        raise httpx.ConnectError("HA is down")
    monkeypatch.setattr(ha.httpx, "get", boom)
    monkeypatch.setattr(ha.httpx, "post", boom)

    # list_players deliberately propagates: the /admin endpoint catches it and
    # shows the reason, which beats an empty dropdown with no explanation
    with pytest.raises(httpx.HTTPError):
        ha.list_players()
    # the grouping helpers must NOT propagate — they run mid-game
    assert ha.group_state(["media_player.kitchen"]) == {}
    assert ha.join_group("media_player.kitchen", ["media_player.study"]) is False
    assert ha.unjoin_group(["media_player.study"]) is False
    ha.restore_groups({"media_player.kitchen": ["media_player.study"]})  # must not raise


def test_helpers_are_inert_without_ha_config(monkeypatch):
    """No HA_URL/HA_TOKEN (the cast-display-only setup) — every helper stays quiet
    instead of firing requests at an empty URL."""
    monkeypatch.setattr(ha, "HA_URL", "")
    monkeypatch.setattr(ha, "HA_TOKEN", "")

    def boom(*a, **kw):
        raise AssertionError("must not call HA when unconfigured")
    monkeypatch.setattr(ha.httpx, "get", boom)
    monkeypatch.setattr(ha.httpx, "post", boom)

    assert ha.available() is False
    assert ha.list_players() == []
    assert ha.group_state(["media_player.kitchen"]) == {}
    assert ha.join_group("media_player.kitchen", ["media_player.study"]) is False
    assert ha.unjoin_group(["media_player.study"]) is False
    ha.restore_groups({"media_player.kitchen": []})


def test_cast_disabled_plays_nothing(ha_env, monkeypatch):
    monkeypatch.setattr(ha, "CAST_ENABLED", False)
    assert ha.play_url("http://quiz.test/x.mp3", "x") is False
    assert ha_env == []


def test_no_speaker_chosen_plays_nothing(ha_env, monkeypatch):
    """MEDIA_PLAYER unset and nothing saved — skip quietly rather than POST an
    empty entity_id, which HA answers 200 to while doing nothing."""
    monkeypatch.setattr(ha, "MEDIA_PLAYER", "")
    monkeypatch.setattr(ha, "_with_conn", lambda fn: "")
    assert ha.play_url("http://quiz.test/x.mp3", "x") is False
    assert ha_env == []


def test_settings_keys_are_stable():
    """These strings are persisted in the DB — renaming one silently orphans a
    user's saved speaker choice."""
    assert ha.SETTING_TARGET == "speaker_target"
    assert ha.SETTING_GROUP == "speaker_group"


# ---------- the /admin speaker endpoints ----------

def pin_db(monkeypatch, path):
    """Point db.connect at a scratch file. A FRESH connection per call, exactly
    as in production — the endpoints close theirs, and sqlite3.Connection.close
    can't be stubbed out."""
    from app import main
    real_connect = db.connect   # main.db IS this module — grab it before patching
    monkeypatch.setattr(main.db, "connect", lambda *a, **kw: real_connect(path))


def admin_client(monkeypatch, path):
    from fastapi.testclient import TestClient
    from app import main
    pin_db(monkeypatch, path)
    return TestClient(main.app)


def test_speakers_endpoint_lists_and_marks_the_selection(ha_env, monkeypatch):
    conn, path = make_db()
    try:
        c = admin_client(monkeypatch, path)
        r = c.get("/api/admin/speakers")
        assert r.status_code == 200
        body = r.json()
        assert body["configured"] is True
        assert body["target"] == "media_player.kitchen_music_assistant"
        assert body["env_default"] == "media_player.kitchen_music_assistant"
        assert body["overridden"] is False
        assert body["group"] == []
        eids = {p["entity_id"] for p in body["players"]}
        assert "media_player.kitchen" in eids
        assert "media_player.plex_chrome" not in eids   # filtered

        # save an override, then read it back as overridden
        r = c.post("/api/admin/speakers",
                   json={"target": "media_player.study_music_assistant",
                         "group": ["media_player.kitchen", "media_player.study"]})
        assert r.status_code == 200 and r.json()["saved"] is True
        body = c.get("/api/admin/speakers").json()
        assert body["target"] == "media_player.study_music_assistant"
        assert body["overridden"] is True
        assert body["group"] == ["media_player.kitchen", "media_player.study"]
    finally:
        os.unlink(path)


def test_saving_an_empty_target_restores_the_env_default(ha_env, monkeypatch):
    conn, path = make_db()
    try:
        c = admin_client(monkeypatch, path)
        c.post("/api/admin/speakers", json={"target": "media_player.study_music_assistant"})
        r = c.post("/api/admin/speakers", json={"target": ""})
        assert r.json()["target"] == "media_player.kitchen_music_assistant"
        assert c.get("/api/admin/speakers").json()["overridden"] is False
    finally:
        os.unlink(path)


def test_speakers_endpoint_rejects_junk(ha_env, monkeypatch):
    conn, path = make_db()
    try:
        c = admin_client(monkeypatch, path)
        assert c.post("/api/admin/speakers", json={"target": "light.kitchen"}).status_code == 400
        assert c.post("/api/admin/speakers", json={}).status_code == 400
        # non-media_player entries are dropped from a group rather than 400ing
        r = c.post("/api/admin/speakers",
                   json={"group": ["media_player.kitchen", "light.x", 7, None]})
        assert r.json()["group"] == ["media_player.kitchen"]
    finally:
        os.unlink(path)


def test_speakers_endpoint_survives_an_ha_outage(monkeypatch):
    """An unreachable HA shows an empty list plus the reason — not a 500 that
    makes the whole admin page look broken."""
    conn, path = make_db()
    try:
        monkeypatch.setattr(ha, "HA_URL", "http://ha.test")
        monkeypatch.setattr(ha, "HA_TOKEN", "tok")

        def boom(*a, **kw):
            raise httpx.ConnectError("HA is down")
        monkeypatch.setattr(ha.httpx, "get", boom)
        r = admin_client(monkeypatch, path).get("/api/admin/speakers")
        assert r.status_code == 200
        assert r.json()["players"] == [] and "HA is down" in r.json()["error"]
    finally:
        conn.close(); os.unlink(path)


def test_speakers_endpoint_without_ha_configured(monkeypatch):
    conn, path = make_db()
    try:
        monkeypatch.setattr(ha, "HA_URL", "")
        monkeypatch.setattr(ha, "HA_TOKEN", "")
        body = admin_client(monkeypatch, path).get("/api/admin/speakers").json()
        assert body == {"configured": False, "players": [], "target": "", "group": [],
                        "env_default": ha.MEDIA_PLAYER}
    finally:
        conn.close(); os.unlink(path)


def test_speaker_test_button_plays_the_fanfare_on_the_named_speaker(ha_env, monkeypatch):
    conn, path = make_db()
    try:
        c = admin_client(monkeypatch, path)
        r = c.post("/api/admin/speakers/test",
                   json={"target": "media_player.roam_music_assistant"})
        assert r.json() == {"played": True, "target": "media_player.roam_music_assistant"}
        assert [x[0] for x in ha_env] == ["media_player/volume_set", "music_assistant/play_media"]
        assert ha_env[1][1]["media_id"] == "http://quiz.test/static/fanfare.mp3"
        assert ha_env[1][1]["entity_id"] == "media_player.roam_music_assistant"
    finally:
        conn.close(); os.unlink(path)


# ---------- game-start grouping, and putting the speakers back ----------

def test_game_start_groups_and_game_end_restores(ha_env, monkeypatch):
    """The lifecycle the user actually cares about: the chosen speakers get joined
    when a game starts and handed back afterwards."""
    import asyncio

    from app import main

    conn, path = make_db()
    try:
        db.set_setting(conn, ha.SETTING_GROUP,
                       json.dumps(["media_player.kitchen", "media_player.study"]))
        conn.close()
        pin_db(monkeypatch, path)
        hub = main.Hub()

        asyncio.run(hub.group_speakers())
        # prior arrangement remembered, then the group formed on the NATIVE entities
        assert hub.group_snapshot == {"media_player.kitchen": ["media_player.kitchen"],
                                     "media_player.study": ["media_player.study"]}
        assert ("media_player/join",
                {"entity_id": "media_player.kitchen",
                 "group_members": ["media_player.study"]}) in ha_env

        ha_env.clear()
        asyncio.run(hub.ungroup_speakers())
        assert ha_env[0][0] == "media_player/unjoin"
        assert hub.group_snapshot is None      # nothing left to restore twice
        ha_env.clear()
        asyncio.run(hub.ungroup_speakers())    # idempotent
        assert ha_env == []
    finally:
        os.unlink(path)


def test_a_single_speaker_is_not_grouped(ha_env, monkeypatch):
    """One speaker is just the play target — no join call, and nothing to restore."""
    import asyncio

    from app import main

    conn, path = make_db()
    try:
        db.set_setting(conn, ha.SETTING_GROUP, json.dumps(["media_player.kitchen"]))
        conn.close()
        pin_db(monkeypatch, path)
        hub = main.Hub()
        asyncio.run(hub.group_speakers())
        assert hub.group_snapshot is None
        assert ha_env == []
    finally:
        os.unlink(path)


def test_a_grouping_failure_never_stops_a_game(monkeypatch):
    """HA down at game start: log it and play on one speaker. The alternative —
    an exception out of new_game — would mean no game at all."""
    import asyncio

    from app import main

    conn, path = make_db()
    try:
        db.set_setting(conn, ha.SETTING_GROUP,
                       json.dumps(["media_player.kitchen", "media_player.study"]))
        monkeypatch.setattr(ha, "HA_URL", "http://ha.test")
        monkeypatch.setattr(ha, "HA_TOKEN", "tok")
        conn.close()
        pin_db(monkeypatch, path)

        def boom(*a, **kw):
            raise httpx.ConnectError("HA is down")
        monkeypatch.setattr(ha.httpx, "get", boom)
        monkeypatch.setattr(ha.httpx, "post", boom)

        hub = main.Hub()
        asyncio.run(hub.group_speakers())       # must not raise
        asyncio.run(hub.ungroup_speakers())     # must not raise
    finally:
        conn.close(); os.unlink(path)
