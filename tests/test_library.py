import os
import tempfile

from app import db, library


def make_db(tracks):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = db.connect(path)
    for i, t in enumerate(tracks):
        conn.execute(
            "INSERT INTO tracks(id,title,artist,album,album_id,duration,global_listeners,"
            "clipped_at,tier,active,banned) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (t.get("id", f"t{i}"), t.get("title", "Song"), t.get("artist", "Artist"),
             t.get("album", "Album"), t.get("album_id", "al1"), t.get("duration", 200),
             t.get("global_listeners"), t.get("clipped_at"), t.get("tier", "easy"),
             t.get("active", 1), t.get("banned", 0)))
    conn.commit()
    return conn, path


# -- artist_key ------------------------------------------------------------

def test_artist_key_folds_real_variant_spellings():
    """Every group here is one act, measured from the real library."""
    for group in (["AC/DC", "AC, DC", "AC-DC", "ACDC", "AC DC"],
                  ["Coldplay", "coldplay"],
                  ["Jean-Michel Jarre", "Jean Michel Jarre", "Jean‐Michel Jarre"],
                  ["The Black Eyed Peas", "Black Eyed Peas"],
                  ["Mumford & Sons", "Mumford And Sons", "Mumford and Sons"],
                  ["Mark Ronson", "Mark  Ronson"],
                  ["The Beach Boys", "The Beach Boys "],
                  ["Marina and The Diamonds", "Marina And The Diamonds", "Marina & The Diamonds"]):
        keys = {library.artist_key(a) for a in group}
        assert len(keys) == 1, f"{group} folded to {keys}"


def test_artist_key_keeps_different_artists_apart():
    """The failure that matters: merging two acts makes one answer as the other.

    Each pair looks close by some fuzzy measure, so a Levenshtein-style match
    would collapse them. Exact-after-normalisation must not.
    """
    for a, b in (("The Beatles", "The Beatless"),
                 ("Queen", "Queens of the Stone Age"),
                 ("Prodigy", "The Prodigy Firestarter"),
                 ("Kiss", "Kisses"),
                 ("Marina and The Diamonds", "Marina Diamandis"),
                 ("Bon Jovi", "Jon Bon Jovi"),
                 ("Chopin", "Chopin Trio")):
        assert library.artist_key(a) != library.artist_key(b), (a, b)


def test_artist_key_survives_none_and_empty():
    assert library.artist_key(None) == ""
    assert library.artist_key("") == ""
    assert library.artist_key("   ") == ""


def test_artist_variants_ranks_primary_by_usage():
    conn, p = make_db(
        [{"artist": "AC/DC"}] * 5 + [{"artist": "AC, DC"}] * 2 + [{"artist": "ACDC"}]
        + [{"artist": "Blondie"}])
    try:
        groups = library.artist_variants(conn)
        assert len(groups) == 1, "Blondie has one spelling — not a variant group"
        g = groups[0]
        assert g["primary"] == "AC/DC"
        assert g["tracks"] == 8
        assert [s["artist"] for s in g["spellings"]] == ["AC/DC", "AC, DC", "ACDC"]
    finally:
        os.unlink(p)


# -- duplicates ------------------------------------------------------------

def test_duplicates_need_an_exact_duration_match():
    """The rule that protects real music.

    'Nothing Else Matters' appears 12 times on The Metallica Blacklist as 12
    DIFFERENT covers — same title, same album_artist, different artists and
    durations. Keying on title alone would delete 11 real songs.
    """
    conn, p = make_db([
        {"id": "a", "title": "Alone", "artist": "Heart", "album": "Greatest Hits",
         "duration": 218, "global_listeners": 652142},
        {"id": "b", "title": "Alone", "artist": "Heart", "album": "80s Classics",
         "duration": 218},
        {"id": "c", "title": "Alone", "artist": "Heart", "album": "Power Ballads",
         "duration": 218},
        # different SONG, same title+artist: a live take runs longer
        {"id": "d", "title": "Alone", "artist": "Heart", "album": "Live", "duration": 265},
    ])
    try:
        dupes = library.find_duplicates(conn)
        assert {d["id"] for d in dupes} == {"b", "c"}, "keeps the best-scored, drops its twins"
        assert all(d["keeping"] == "a" for d in dupes)
    finally:
        os.unlink(p)


def test_duplicates_prefer_keeping_an_already_clipped_row():
    """A clip is a Navidrome download plus four ffmpeg cuts — never throw one away
    to keep an unclipped row that merely scored higher."""
    conn, p = make_db([
        {"id": "cut", "title": "Call Me", "artist": "Spagna", "duration": 245,
         "global_listeners": 10, "clipped_at": "2026-07-01T00:00:00"},
        {"id": "fresh", "title": "Call Me", "artist": "Spagna", "duration": 245,
         "global_listeners": 90000},
    ])
    try:
        dupes = library.find_duplicates(conn)
        assert [d["id"] for d in dupes] == ["fresh"]
        assert dupes[0]["keeping"] == "cut"
    finally:
        os.unlink(p)


def test_duplicates_match_across_variant_artist_spellings():
    conn, p = make_db([
        {"id": "a", "title": "Back In Black", "artist": "AC/DC", "duration": 255,
         "global_listeners": 900000},
        {"id": "b", "title": "Back In Black", "artist": "AC, DC", "duration": 255},
    ])
    try:
        assert [d["id"] for d in library.find_duplicates(conn)] == ["b"]
    finally:
        os.unlink(p)


def test_duplicates_ignore_null_duration_rows():
    """No duration = no evidence of duplication, so hands off (find_untrustworthy
    is what catches the '[Unknown Artist]' rows those tend to be)."""
    conn, p = make_db([
        {"id": "a", "title": "Exit", "artist": "X", "duration": None},
        {"id": "b", "title": "Exit", "artist": "X", "duration": None},
    ])
    try:
        assert library.find_duplicates(conn) == []
    finally:
        os.unlink(p)


def test_duplicates_skip_inactive_and_already_banned():
    conn, p = make_db([
        {"id": "a", "title": "S", "artist": "A", "duration": 200},
        {"id": "gone", "title": "S", "artist": "A", "duration": 200, "active": 0},
        {"id": "banned", "title": "S", "artist": "A", "duration": 200, "banned": 1},
    ])
    try:
        assert library.find_duplicates(conn) == []
    finally:
        os.unlink(p)


# -- untrustworthy tags ----------------------------------------------------

def test_untrustworthy_catches_a_whole_mistagged_group():
    """17 rows titled 'Fear and Loathing' on ONE album, 154-367s: different songs
    wearing one name. No row is the 'right' one, so all of them go."""
    conn, p = make_db(
        [{"id": f"f{i}", "title": "Fear and Loathing", "artist": "Marina and The Diamonds",
          "album_id": "electra", "duration": d}
         for i, d in enumerate((154, 182, 202, 207, 216, 221, 226, 241, 367))]
        + [{"id": "ok", "title": "Primadonna", "artist": "Marina and The Diamonds",
            "album_id": "electra", "duration": 220}])
    try:
        junk = library.find_untrustworthy(conn)
        assert len(junk) == 9
        assert "ok" not in {j["id"] for j in junk}
    finally:
        os.unlink(p)


def test_untrustworthy_leaves_a_legitimate_pair_alone():
    """Two rows is normal — a compilation carrying a studio and a live take. The
    floor is JUNK_GROUP_MIN=3 for exactly this reason."""
    conn, p = make_db([
        {"id": "a", "title": "My Way", "artist": "Sinatra", "album_id": "best", "duration": 260},
        {"id": "b", "title": "My Way", "artist": "Sinatra", "album_id": "best", "duration": 295},
    ])
    try:
        assert library.find_untrustworthy(conn) == []
    finally:
        os.unlink(p)


def test_untrustworthy_catches_unknown_artist_rows_without_durations():
    conn, p = make_db([
        {"id": f"u{i}", "title": "Selection", "artist": "[Unknown Artist]",
         "album_id": "unknown", "duration": None} for i in range(5)])
    try:
        assert len(library.find_untrustworthy(conn)) == 5
    finally:
        os.unlink(p)


# -- too short ------------------------------------------------------------

def test_too_short_uses_the_clip_geometry():
    """A 20s intro plus a distinct 12s payoff can't come out of a 30s track."""
    conn, p = make_db([
        {"id": "skit", "duration": 8},
        {"id": "sting", "duration": 31},
        {"id": "edge", "duration": library.MIN_DURATION_S},
        {"id": "song", "duration": 200},
        {"id": "nodur", "duration": None},
    ])
    try:
        assert {r["id"] for r in library.find_too_short(conn)} == {"skit", "sting"}
    finally:
        os.unlink(p)


# -- clean ----------------------------------------------------------------

def test_clean_bans_and_records_why_so_it_is_reversible():
    conn, p = make_db([
        {"id": "a", "title": "S", "artist": "A", "duration": 200, "global_listeners": 5},
        {"id": "dup", "title": "S", "artist": "A", "duration": 200},
        {"id": "short", "title": "Skit", "artist": "B", "duration": 9},
    ])
    try:
        out = library.clean(conn)
        assert out["banned"] == {"duplicate": 1, "untrustworthy": 0, "too_short": 1}
        assert out["total"] == 2
        rows = {r["id"]: r for r in conn.execute("SELECT id,banned,ban_reason FROM tracks")}
        assert rows["dup"]["banned"] == 1 and rows["dup"]["ban_reason"] == "duplicate"
        assert rows["short"]["banned"] == 1 and rows["short"]["ban_reason"] == "too_short"
        assert rows["a"]["banned"] == 0, "the kept row must survive"
    finally:
        os.unlink(p)


def test_clean_dry_run_writes_nothing():
    conn, p = make_db([
        {"id": "a", "title": "S", "artist": "A", "duration": 200},
        {"id": "dup", "title": "S", "artist": "A", "duration": 200},
    ])
    try:
        out = library.clean(conn, dry_run=True)
        assert out["banned"]["duplicate"] == 1 and out["dry_run"] is True
        assert conn.execute("SELECT COUNT(*) FROM tracks WHERE banned=1").fetchone()[0] == 0
    finally:
        os.unlink(p)


def test_clean_is_idempotent():
    """Re-running must not cascade: once the twin is banned the survivor is
    unique, so a second pass has nothing left to ban. A rule that re-examined
    banned rows would eat the whole group one run at a time."""
    conn, p = make_db([
        {"id": "a", "title": "S", "artist": "A", "duration": 200},
        {"id": "b", "title": "S", "artist": "A", "duration": 200},
        {"id": "c", "title": "S", "artist": "A", "duration": 200},
    ])
    try:
        assert library.clean(conn)["total"] == 2
        assert library.clean(conn)["total"] == 0
        assert conn.execute("SELECT COUNT(*) FROM tracks WHERE banned=0").fetchone()[0] == 1
    finally:
        os.unlink(p)


def test_clean_rejects_an_unknown_reason():
    conn, p = make_db([{"id": "a"}])
    try:
        try:
            library.clean(conn, reasons=["nonsense"])
        except ValueError as e:
            assert "nonsense" in str(e)
        else:
            raise AssertionError("expected ValueError for an unknown clean-up")
    finally:
        os.unlink(p)


def test_audit_is_read_only():
    conn, p = make_db([
        {"id": "a", "title": "S", "artist": "A", "duration": 200},
        {"id": "dup", "title": "S", "artist": "A", "duration": 200},
        # a variant pair that is NOT also a duplicate — different songs
        {"id": "v", "title": "Back In Black", "artist": "AC, DC", "duration": 255},
        {"id": "v2", "title": "Highway To Hell", "artist": "AC/DC", "duration": 208},
    ])
    try:
        out = library.audit(conn)
        assert out["duplicate"] == 1 and out["variants"] == 1
        assert conn.execute("SELECT COUNT(*) FROM tracks WHERE banned=1").fetchone()[0] == 0
    finally:
        os.unlink(p)
