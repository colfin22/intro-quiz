"""Artist-tag repair. The write path touches real files, so it's tested on real
(generated) MP3s rather than mocks — a tag write that silently no-ops would pass
any mock-based test and fail on the library.
"""
import os
import shutil
import subprocess
import tempfile

import pytest

from app import retag

pytest.importorskip("mutagen")
HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def make_mp3(path: str, artist: str, title: str = "Song", album_artist: str | None = None):
    """A one-second real MP3 with real ID3 tags."""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "anullsrc=r=44100:cl=mono", "-t", "1", path], check=True)
    import mutagen
    a = mutagen.File(path, easy=True)
    a["artist"] = artist
    a["title"] = title
    if album_artist:
        a["albumartist"] = album_artist
    a.save()


# -- plan (pure, no files) -------------------------------------------------

def test_plan_prefers_album_artist_over_the_majority_vote():
    """A correct album_artist is direct evidence from the file itself; the
    majority vote is only a fallback guess."""
    tracks = [
        # 'a' disagrees with the majority, but its own album_artist knows better
        {"path": "a", "artist": "AC, DC", "album_artist": "AC/DC"},
        {"path": "b", "artist": "ACDC", "album_artist": None},
        {"path": "c", "artist": "ACDC", "album_artist": None},
        {"path": "d", "artist": "ACDC", "album_artist": None},
        # 'e' has no album_artist to go on, so it falls back to the majority
        {"path": "e", "artist": "AC DC", "album_artist": None},
    ]
    by_path = {c["path"]: c for c in retag.plan(tracks)}
    assert by_path["a"]["target"] == "AC/DC" and by_path["a"]["why"] == "album_artist"
    assert by_path["e"]["target"] == "ACDC", "majority wins where there's no album_artist"
    assert by_path["e"]["why"] == "majority"
    assert "b" not in by_path, "already the majority spelling — not a change"


def test_plan_ignores_an_album_artist_for_a_different_act():
    """'Nothing Else Matters' on The Metallica Blacklist has artist=Phoebe
    Bridgers, album_artist=Metallica. Trusting album_artist there would retag
    every cover as Metallica — the worst outcome available to this script."""
    tracks = [{"path": "a", "artist": "Phoebe Bridgers", "album_artist": "Metallica"}]
    assert retag.plan(tracks) == [], "a compilation's album_artist is not the track artist"


def test_plan_leaves_a_single_spelling_alone():
    tracks = [{"path": "a", "artist": "Blondie", "album_artist": None},
              {"path": "b", "artist": "Blondie", "album_artist": None}]
    assert retag.plan(tracks) == []


def test_plan_majority_breaks_ties_on_the_longer_string():
    """'AC/DC' and 'ACDC' with one file each: prefer the punctuated form."""
    tracks = [{"path": "a", "artist": "AC/DC", "album_artist": None},
              {"path": "b", "artist": "ACDC", "album_artist": None}]
    changes = retag.plan(tracks)
    assert [c["target"] for c in changes] == ["AC/DC"]
    assert changes[0]["path"] == "b"


def test_plan_override_beats_everything():
    tracks = [{"path": "a", "artist": "AC, DC", "album_artist": "AC, DC"}]
    changes = retag.plan(tracks, overrides={"AC-DC": "AC/DC"})
    assert changes[0]["target"] == "AC/DC" and changes[0]["why"] == "override"


def test_plan_never_targets_the_spelling_it_already_has():
    tracks = [{"path": "a", "artist": "AC/DC", "album_artist": "AC/DC"},
              {"path": "b", "artist": "AC, DC", "album_artist": "AC/DC"}]
    assert [c["path"] for c in retag.plan(tracks)] == ["b"]


# -- run guards ------------------------------------------------------------

def test_run_refuses_without_music_dirs(monkeypatch):
    """This writes to the music library — it must never run on a default path."""
    monkeypatch.setattr(retag, "MUSIC_DIRS", [])
    with pytest.raises(retag.RetagError) as e:
        retag.run()
    assert "MUSIC_DIRS" in str(e.value)


def test_run_refuses_a_missing_root(monkeypatch):
    monkeypatch.setattr(retag, "MUSIC_DIRS", ["/nope/not/here"])
    with pytest.raises(retag.RetagError) as e:
        retag.run()
    assert "not found" in str(e.value)


# -- the real thing --------------------------------------------------------

@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg to generate test MP3s")
def test_dry_run_changes_no_tag_on_disk(monkeypatch, tmp_path):
    import mutagen
    root = tmp_path / "lib"; root.mkdir()
    make_mp3(str(root / "1.mp3"), "AC/DC", "Back In Black")
    make_mp3(str(root / "2.mp3"), "AC/DC", "Highway To Hell")
    make_mp3(str(root / "3.mp3"), "AC, DC", "Thunderstruck")
    monkeypatch.setattr(retag, "MUSIC_DIRS", [str(root)])
    monkeypatch.setattr(retag, "STATE_DIR", str(tmp_path))

    out = retag.run(write=False)
    assert out["dry_run"] is True and out["changes"] == 1 and out["written"] == 0
    assert out["plan"] == {"AC/DC": {"AC, DC (by majority)": 1}}
    assert mutagen.File(str(root / "3.mp3"), easy=True)["artist"] == ["AC, DC"]


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg to generate test MP3s")
def test_write_fixes_the_tag_and_leaves_the_path_alone(monkeypatch, tmp_path):
    """The path must not change: Navidrome ids tracks by path, so a rename
    re-ids every track and orphans its clips."""
    import mutagen
    root = tmp_path / "lib"; root.mkdir()
    make_mp3(str(root / "a.mp3"), "AC/DC", "Back In Black")
    make_mp3(str(root / "b.mp3"), "AC/DC", "Highway To Hell")
    bad = root / "AC+DC" / "c.mp3"
    bad.parent.mkdir()
    make_mp3(str(bad), "AC, DC", "Thunderstruck")
    monkeypatch.setattr(retag, "MUSIC_DIRS", [str(root)])
    monkeypatch.setattr(retag, "STATE_DIR", str(tmp_path))

    out = retag.run(write=True)
    assert out["written"] == 1 and out["dry_run"] is False
    assert mutagen.File(str(bad), easy=True)["artist"] == ["AC/DC"]
    assert bad.exists(), "the file must not be renamed or moved"
    assert bad.parent.name == "AC+DC", "the mangled FOLDER name is left alone"
    # the other two were already right and must be untouched
    assert mutagen.File(str(root / "a.mp3"), easy=True)["artist"] == ["AC/DC"]


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg to generate test MP3s")
def test_write_is_journalled_so_a_rerun_resumes(monkeypatch, tmp_path):
    root = tmp_path / "lib"; root.mkdir()
    make_mp3(str(root / "a.mp3"), "AC/DC", "One")
    make_mp3(str(root / "b.mp3"), "AC/DC", "Two")
    make_mp3(str(root / "c.mp3"), "AC, DC", "Three")
    monkeypatch.setattr(retag, "MUSIC_DIRS", [str(root)])
    monkeypatch.setattr(retag, "STATE_DIR", str(tmp_path))

    assert retag.run(write=True)["written"] == 1
    assert os.path.exists(retag._journal_path())
    # second pass: the tag is already right AND journalled — no work, no error
    assert retag.run(write=True)["written"] == 0


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg to generate test MP3s")
def test_a_write_updates_the_cache_not_just_the_journal(monkeypatch, tmp_path):
    """Rewriting 'AC, DC' -> 'AC/DC' changes neither the file size nor the
    whole-second mtime, so the (mtime, size) cache key does NOT invalidate. If the
    write doesn't also refresh the cache entry, the cache keeps serving the old
    artist and every rerun re-plans the same writes — invisible while the journal
    survives, but a whole-library rewrite on a fresh container with empty /data.
    """
    root = tmp_path / "lib"; root.mkdir()
    make_mp3(str(root / "a.mp3"), "AC/DC", "One")
    make_mp3(str(root / "b.mp3"), "AC/DC", "Two")
    make_mp3(str(root / "c.mp3"), "AC, DC", "Three")
    monkeypatch.setattr(retag, "MUSIC_DIRS", [str(root)])
    monkeypatch.setattr(retag, "STATE_DIR", str(tmp_path))

    assert retag.run(write=True)["written"] == 1
    # drop the journal — the cache alone must now know the file is already right
    os.remove(retag._journal_path())
    assert retag.run(write=False)["changes"] == 0, \
        "cache still holds the pre-write artist; a rerun would rewrite the library"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg to generate test MP3s")
def test_scan_cache_survives_a_restart_and_invalidates_on_edit(monkeypatch, tmp_path):
    """The cache is what makes a 22k-file scan interruptible. It must also not
    go stale: an edited file has to be re-read, or a fix would be planned from
    the tag the file used to have."""
    import mutagen
    root = tmp_path / "lib"; root.mkdir()
    make_mp3(str(root / "a.mp3"), "AC/DC", "One")
    make_mp3(str(root / "b.mp3"), "AC, DC", "Two")
    monkeypatch.setattr(retag, "MUSIC_DIRS", [str(root)])
    monkeypatch.setattr(retag, "STATE_DIR", str(tmp_path))

    assert retag.run(write=False)["changes"] == 1
    cache = retag._load_cache()
    assert len(cache) == 2, "the scan must be cached for a restart to be cheap"

    # a genuinely cached second run reads nothing new and agrees
    assert retag.run(write=False)["changes"] == 1

    # now edit b.mp3 behind the cache's back: mtime+size change, so it re-reads
    a = mutagen.File(str(root / "b.mp3"), easy=True)
    a["artist"] = "AC/DC"
    a["title"] = "Two But Longer Title To Change The Size"
    a.save()
    os.utime(str(root / "b.mp3"), (0, 0))
    assert retag.run(write=False)["changes"] == 0, "stale cache entry was not invalidated"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg to generate test MP3s")
def test_only_filter_limits_the_writes(monkeypatch, tmp_path):
    import mutagen
    root = tmp_path / "lib"; root.mkdir()
    make_mp3(str(root / "a.mp3"), "AC/DC", "One")
    make_mp3(str(root / "b.mp3"), "AC/DC", "Two")
    make_mp3(str(root / "c.mp3"), "AC, DC", "Three")
    make_mp3(str(root / "d.mp3"), "Coldplay", "Yellow")
    make_mp3(str(root / "e.mp3"), "Coldplay", "Clocks")
    make_mp3(str(root / "f.mp3"), "coldplay", "Trouble")
    monkeypatch.setattr(retag, "MUSIC_DIRS", [str(root)])
    monkeypatch.setattr(retag, "STATE_DIR", str(tmp_path))

    assert retag.run(write=False)["changes"] == 2
    out = retag.run(write=True, only=["AC/DC"])
    assert out["written"] == 1
    assert mutagen.File(str(root / "f.mp3"), easy=True)["artist"] == ["coldplay"], \
        "--only must not touch other artists"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg to generate test MP3s")
def test_a_corrupt_file_does_not_abort_the_run(monkeypatch, tmp_path):
    root = tmp_path / "lib"; root.mkdir()
    make_mp3(str(root / "a.mp3"), "AC/DC", "One")
    make_mp3(str(root / "b.mp3"), "AC/DC", "Two")
    make_mp3(str(root / "c.mp3"), "AC, DC", "Three")
    (root / "junk.mp3").write_bytes(b"this is not an mp3")
    monkeypatch.setattr(retag, "MUSIC_DIRS", [str(root)])
    monkeypatch.setattr(retag, "STATE_DIR", str(tmp_path))

    out = retag.run(write=True)
    assert out["written"] == 1, "the corrupt file must be skipped, not fatal"


def test_empty_library_is_not_an_error(monkeypatch, tmp_path):
    root = tmp_path / "empty"; root.mkdir()
    monkeypatch.setattr(retag, "MUSIC_DIRS", [str(root)])
    monkeypatch.setattr(retag, "STATE_DIR", str(tmp_path))
    out = retag.run(write=True)
    assert out == {"files": 0, "changes": 0, "written": 0, "dry_run": False}
