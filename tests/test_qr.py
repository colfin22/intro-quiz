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
