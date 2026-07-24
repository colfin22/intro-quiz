"""Scan-to-join QR endpoints — URL resolution precedence and SVG output."""
from fastapi.testclient import TestClient

from app import board_cast, config, main


def client():
    return TestClient(main.app)


def test_qr_endpoint_returns_svg():
    r = client().get("/api/join-qr.svg?url=http://192.168.1.10:8000")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in r.content


def test_join_url_uses_board_origin_param(monkeypatch):
    monkeypatch.setattr(config, "JOIN_URL", "")
    monkeypatch.setattr(board_cast, "BOARD_URL", "")
    r = client().get("/api/join-url?url=http://192.168.1.10:8000/")
    # trailing slash trimmed, scheme preserved
    assert r.json() == {"url": "http://192.168.1.10:8000"}


def test_join_url_derives_from_board_url(monkeypatch):
    monkeypatch.setattr(config, "JOIN_URL", "")
    monkeypatch.setattr(board_cast, "BOARD_URL", "https://quiz.example.com/board")
    # no ?url — falls through to BOARD_URL minus its /board suffix
    r = client().get("/api/join-url")
    assert r.json() == {"url": "https://quiz.example.com"}


def test_join_url_override_wins(monkeypatch):
    monkeypatch.setattr(config, "JOIN_URL", "https://join.me")
    monkeypatch.setattr(board_cast, "BOARD_URL", "https://quiz.example.com/board")
    # explicit override beats both the board origin and BOARD_URL
    r = client().get("/api/join-url?url=http://192.168.1.10:8000")
    assert r.json() == {"url": "https://join.me"}


def test_join_url_falls_back_to_request_base(monkeypatch):
    monkeypatch.setattr(config, "JOIN_URL", "")
    monkeypatch.setattr(board_cast, "BOARD_URL", "")
    r = client().get("/api/join-url")
    assert r.json()["url"] == "http://testserver"


def test_qr_ignores_non_http_url(monkeypatch):
    """A junk ?url must not poison the QR — fall through to the next source."""
    monkeypatch.setattr(config, "JOIN_URL", "")
    monkeypatch.setattr(board_cast, "BOARD_URL", "https://quiz.example.com/board")
    r = client().get("/api/join-url?url=javascript:alert(1)")
    assert r.json() == {"url": "https://quiz.example.com"}


def test_wifi_qr_absent_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "GUEST_WIFI_SSID", "")
    assert client().get("/api/wifi-qr.svg").status_code == 404


def test_wifi_qr_svg_when_configured(monkeypatch):
    monkeypatch.setattr(config, "GUEST_WIFI_SSID", "GuestNet")
    monkeypatch.setattr(config, "GUEST_WIFI_PASSWORD", "pw123456")
    monkeypatch.setattr(config, "GUEST_WIFI_AUTH", "WPA")
    r = client().get("/api/wifi-qr.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in r.content


def test_wifi_payload_escaping(monkeypatch):
    monkeypatch.setattr(config, "GUEST_WIFI_AUTH", "WPA")
    monkeypatch.setattr(config, "GUEST_WIFI_SSID", 'Caf;e:Ne,t"1\\2')
    monkeypatch.setattr(config, "GUEST_WIFI_PASSWORD", "p;a,s:s")
    p = main._wifi_payload()
    assert p == 'WIFI:T:WPA;S:Caf\;e\\:Ne\\,t\\"1\\\\2;P:p\;a\\,s\\:s;;'


def test_wifi_payload_emoji_ssid_survives(monkeypatch):
    monkeypatch.setattr(config, "GUEST_WIFI_AUTH", "WPA")
    monkeypatch.setattr(config, "GUEST_WIFI_SSID", "🎵 Quiz Net")
    monkeypatch.setattr(config, "GUEST_WIFI_PASSWORD", "x")
    assert "🎵 Quiz Net" in main._wifi_payload()
    monkeypatch.setattr(config, "GUEST_WIFI_SSID", "GuestNet")
    assert client().get("/api/wifi-qr.svg").status_code == 200


def test_wifi_payload_nopass(monkeypatch):
    monkeypatch.setattr(config, "GUEST_WIFI_AUTH", "nopass")
    monkeypatch.setattr(config, "GUEST_WIFI_SSID", "OpenNet")
    monkeypatch.setattr(config, "GUEST_WIFI_PASSWORD", "ignored")
    p = main._wifi_payload()
    assert p == "WIFI:T:nopass;S:OpenNet;;"
    assert "P:" not in p
