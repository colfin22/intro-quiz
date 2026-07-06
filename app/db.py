import os
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY,          -- Navidrome/Subsonic song id
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    album TEXT,
    album_id TEXT,
    genre TEXT,
    year INTEGER,
    duration INTEGER,             -- seconds
    cover_art TEXT,               -- Subsonic coverArt id
    intro_offset REAL DEFAULT 0,  -- seconds to skip before the "intro" starts
    play_count INTEGER DEFAULT 0, -- family total, from the annotation export
    starred INTEGER DEFAULT 0,    -- how many family members starred it
    global_listeners INTEGER,     -- Last.fm listeners (NULL = not fetched yet)
    global_playcount INTEGER,
    tier TEXT,                    -- easy / medium / hard / tiebreak (NULL = unscored)
    active INTEGER DEFAULT 1      -- still present in the library on last sync
);
CREATE INDEX IF NOT EXISTS idx_tracks_tier ON tracks(tier);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    p = path or config.DB_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def get_setting(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()
