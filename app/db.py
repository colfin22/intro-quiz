import os
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY,          -- Navidrome/Subsonic song id
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    album TEXT,
    album_artist TEXT,           -- 'Various Artists' = compilation, excluded from the quiz
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
    clipped_at TEXT,              -- when intro clips were last cut (NULL = no clips)
    banned INTEGER DEFAULT 0,     -- never picked again
    ban_reason TEXT,              -- 'flag' (in-game) / 'decode' (cutter) / 'album' (pattern ban)
    active INTEGER DEFAULT 1      -- still present in the library on last sync
);
CREATE INDEX IF NOT EXISTS idx_tracks_tier ON tracks(tier);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    rounds INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS trivia (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,           -- 'fact' (read-aloud) | 'tf' (true/false)
    text TEXT NOT NULL UNIQUE,
    answer INTEGER,               -- tf only: 1 = true
    source TEXT NOT NULL,         -- 'seed' | 'opentdb'
    used_at TEXT                  -- last picked for a game (NULL = fresh)
);
CREATE TABLE IF NOT EXISTS results (
    game_id INTEGER NOT NULL REFERENCES games(id),
    player TEXT NOT NULL,
    score INTEGER NOT NULL,
    correct INTEGER NOT NULL,
    fastest_ms INTEGER,
    PRIMARY KEY (game_id, player)
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    p = path or config.DB_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    conn = sqlite3.connect(p, timeout=15)
    conn.row_factory = sqlite3.Row
    # WAL + busy_timeout: the Last.fm sweep writes continuously in the
    # background; without these any concurrent write hits "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tracks)")}
    for ddl in ("ALTER TABLE tracks ADD COLUMN clipped_at TEXT",
                "ALTER TABLE tracks ADD COLUMN banned INTEGER DEFAULT 0",
                "ALTER TABLE tracks ADD COLUMN album_artist TEXT",
                "ALTER TABLE tracks ADD COLUMN ban_reason TEXT",
                "ALTER TABLE tracks ADD COLUMN quality_notified_at TEXT"):
        col = ddl.split(" ADD COLUMN ")[1].split()[0]
        if col not in cols:
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:  # concurrent connect won the race
                pass
    return conn


def get_setting(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()
