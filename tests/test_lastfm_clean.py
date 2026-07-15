from app import lastfm


def test_clean_title_strips_variant_markers():
    cases = {
        "What A Fool Believes (orig)": "What A Fool Believes",
        "(You're The) Devil In Disguise (remastered)": "(You're The) Devil In Disguise",
        "Will You Still Love Me (remastered)": "Will You Still Love Me",
        "My Way (Live)": "My Way",
        "Let's Hear It for the Boy (Single Version)": "Let's Hear It for the Boy",
        "You Belong to Me (with Amanda Sudano Ramirez)": "You Belong to Me",
        "01 Rape Me": "Rape Me",
        "02 - Been A Son": "Been A Son",
        "Crazy (remastered) (live)": "Crazy",
        "Teenage Kicks (PMEDIA)": "Teenage Kicks",
    }
    for raw, want in cases.items():
        assert lastfm.clean_title(raw) == want, raw


def test_clean_title_keeps_real_names():
    keep = [
        "Song 2 (Woo Hoo)",              # parenthetical IS the hook
        "(I Can't Get No) Satisfaction",  # leading parenthetical
        "Don't Stop Me Now",
        "1979",                           # a year, not a track number prefix
    ]
    for t in keep:
        assert lastfm.clean_title(t) == t, t


class FakeHttp:
    """Answers a canned table keyed by the track param."""
    def __init__(self, table):
        self.table = table
        self.calls = []

    def get(self, url, params):
        self.calls.append(params["track"])
        listeners = self.table.get(params["track"], 0)

        class R:
            def raise_for_status(self):
                pass

            def json(self, _l=listeners):
                if not _l:
                    return {"error": 6, "message": "Track not found"}
                return {"track": {"listeners": _l, "playcount": _l * 10}}
        return R()


def test_lookup_best_retries_cleaned_and_keeps_better():
    http = FakeHttp({"What A Fool Believes (orig)": 2, "What A Fool Believes": 900000})
    assert lastfm.lookup_best(http, "The Doobie Brothers", "What A Fool Believes (orig)")[0] == 900000
    assert http.calls == ["What A Fool Believes (orig)", "What A Fool Believes"]


def test_lookup_best_no_retry_when_there_is_nothing_to_clean():
    """RETRY_BELOW may only short-circuit a tag the cleaner would not touch."""
    http = FakeHttp({"Don't Stop Me Now": 50000})
    assert lastfm.lookup_best(http, "Queen", "Don't Stop Me Now")[0] == 50000
    assert http.calls == ["Don't Stop Me Now"]  # clean tag, strong hit — one call only


def test_lookup_best_retries_even_when_the_wrong_hit_looks_healthy():
    """#40 — the bug this replaces. lookup_best used to return any hit above
    RETRY_BELOW (1000) without ever trying the cleaned form. But a mangled title
    routinely DOES resolve — to a junk entry or a soundtrack listing — with a few
    thousand listeners, which clears that bar. The real song was never looked up:

        'Ticket to Ride [from the Film "Help! "]'  ->   4,299   (kept)
        'Ticket to Ride'                           -> 771,304   (never tried)

    4,299 lands a famous song in the hard/tiebreak tier, so it is never asked.
    A plausible wrong answer is still a wrong answer: if the cleaner changes
    anything, we look the cleaned form up too."""
    raw = 'Ticket to Ride [from the Film "Help! "]'
    http = FakeHttp({raw: 4299, "Ticket to Ride": 771304})
    assert lastfm.lookup_best(http, "The Beatles", raw)[0] == 771304
    assert http.calls == [raw, "Ticket to Ride"]


def test_lookup_best_still_keeps_the_better_exact_hit():
    """Keep-the-highest is preserved: a cleaned form that scores LOWER never wins,
    even though we now always try it."""
    http = FakeHttp({"Song (Live)": 9000, "Song": 12})
    assert lastfm.lookup_best(http, "Band", "Song (Live)")[0] == 9000


def test_lookup_best_keeps_original_when_retry_worse():
    http = FakeHttp({"Willow's Song (Bury version)": 8, "Willow's Song": 0})
    assert lastfm.lookup_best(http, "Doves", "Willow's Song (Bury version)")[0] == 8


# ---------- a featured credit in the ARTIST tag hid the song entirely (#34) ----------

def test_clean_artist_strips_a_featured_credit():
    assert lastfm.clean_artist("Charlie Puth Feat. Sabrina Carpenter") == "Charlie Puth"
    assert lastfm.clean_artist("Zedd ft. Jasmine Thompson") == "Zedd"
    assert lastfm.clean_artist("Calvin Harris featuring Rihanna") == "Calvin Harris"
    assert lastfm.clean_artist("Eminem (feat. Dido)") == "Eminem"
    assert lastfm.clean_artist("Addison Rae Feat. Charli Xcx") == "Addison Rae"


def test_clean_artist_never_invents_a_different_act():
    """'&', '+' and 'and' are NOT featured credits. Stripping them would score a
    completely different artist's song — worse than the miss it was fixing."""
    for whole in ("Simon & Garfunkel", "Hall & Oates", "Florence + The Machine",
                  "Earth, Wind & Fire", "Crosby, Stills, Nash & Young",
                  "Nick Cave and the Bad Seeds", "Sam & Dave"):
        assert lastfm.clean_artist(whole) == whole


def test_clean_artist_leaves_a_plain_artist_alone():
    assert lastfm.clean_artist("Teddy Swims") == "Teddy Swims"
    assert lastfm.clean_artist("") == ""


def test_lookup_best_finds_the_song_hiding_behind_a_featured_credit():
    """The whole point: tagged 'X Feat. Y', Last.fm knows no such artist, so the exact
    lookup returns 0 — and a 0 means no tier, no clip, and a track that never appears
    in the quiz at all. It must retry under the primary artist."""
    calls = []

    class Fake:
        def get(self, url, params):
            calls.append((params["artist"], params["track"]))

            class R:
                @staticmethod
                def raise_for_status():
                    pass

                @staticmethod
                def json():
                    # Last.fm only knows the song under its PRIMARY artist
                    if params["artist"] == "Charlie Puth":
                        return {"track": {"listeners": "820000", "playcount": "5000000"}}
                    return {"error": 6, "message": "Track not found"}
            return R()

    listeners, _ = lastfm.lookup_best(
        Fake(), "Charlie Puth Feat. Sabrina Carpenter", "That's Not How This Works")
    assert listeners == 820000, "the featured-credit artist hid a very famous song"
    assert ("Charlie Puth Feat. Sabrina Carpenter", "That's Not How This Works") in calls
    assert ("Charlie Puth", "That's Not How This Works") in calls


def test_clean_title_strips_soundtrack_markers():
    """#38 — a film marker in the title does not zero the score, it gets the WRONG one:
    Last.fm files these under the bare title, so the song scores a few thousand instead
    of millions, lands in the hard/tiebreak tier, and is never asked. Nothing errors."""
    cases = {
        'A Hard Day\'s Night [from the Film "A Hard Day\'s Night"]': "A Hard Day's Night",
        "Help! [from the Film \"Help! \"]": "Help!",
        "What Was I Made For? (From The Motion Picture 'Barbie')": "What Was I Made For?",
        "Axel F (From ‘Beverly Hills Cop’ Soundtrack)": "Axel F",
        "Miami Vice Theme (From ‘Miami Vice’ Soundtrack)": "Miami Vice Theme",
        "Going Home (Theme From 'Local Hero')": "Going Home",
        "Star Walkin' (League Of Legends Worlds Theme)": "Star Walkin'",
        "Somewhere (From “West Side Story”)": "Somewhere",
        "The Host of the Seraphim [From the Baraka Soundtrack]": "The Host of the Seraphim",
    }
    for raw, want in cases.items():
        assert lastfm.clean_title(raw) == want, raw


def test_clean_title_does_not_strip_a_remix():
    """A remix is a DISTINCT WORK, not a variant. Stripping it would score an obscure
    remix as the famous original and promote it into the pool the quiz draws from —
    1,209 dance/trance tracks in a real 47k library. Same narrowness rule as #34."""
    keep = [
        "Colours (Humate Remix)",
        "Breathe (Dawnseekers Remix)",
        "Viola (Armin van Buuren Rising Star Remix)",
    ]
    for t in keep:
        assert lastfm.clean_title(t) == t, t


def test_clean_title_keeps_a_theme_that_is_the_song():
    """'theme' only counts inside a trailing parenthetical — a bare title survives."""
    assert lastfm.clean_title("Miami Vice Theme") == "Miami Vice Theme"
    assert lastfm.clean_title("Theme From Shaft") == "Theme From Shaft"


# --- #43: reversed / comma artist tags -----------------------------------------

def test_clean_artist_un_reverses_surname_first_and_article():
    """'Oakenfold, Paul' and 'Beatles, The' are filed under a name Last.fm can't match,
    so the track scores as obscure and drops out of the pool. Un-reverse them (#43)."""
    assert lastfm.clean_artist("Oakenfold, Paul") == "Paul Oakenfold"
    assert lastfm.clean_artist("Angello, Steve") == "Steve Angello"
    assert lastfm.clean_artist("Beatles, The") == "The Beatles"
    assert lastfm.clean_artist("La’s, The") == "The La’s"
    # feat is stripped FIRST, then the primary is un-reversed
    assert lastfm.clean_artist("Saunderson, Kevin feat. Inner City") == "Kevin Saunderson"


def test_clean_artist_never_mangles_a_real_band_name():
    """The narrowness rule: anything with '&'/'and', or a multi-name credit, is left
    exactly as it is — reversing it would invent a different act (#43, same rule as #34)."""
    keep = [
        "Crosby, Stills, Nash & Young",
        "Earth, Wind & Fire",
        "Simon & Garfunkel",
        "Post Malone, Louis Bell, Ty Dolla $ign",   # multi-name credit, not a reversal
        "Blood, Sweat and Tears",
        "Florence + The Machine",
        "Radiohead",                                 # no comma at all
    ]
    for a in keep:
        assert lastfm.clean_artist(a) == a, a
