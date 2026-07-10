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


def test_lookup_best_no_retry_on_strong_match():
    http = FakeHttp({"My Way (Live)": 50000})
    assert lastfm.lookup_best(http, "Tom Jones", "My Way (Live)")[0] == 50000
    assert http.calls == ["My Way (Live)"]  # strong enough — no second call


def test_lookup_best_keeps_original_when_retry_worse():
    http = FakeHttp({"Willow's Song (Bury version)": 8, "Willow's Song": 0})
    assert lastfm.lookup_best(http, "Doves", "Willow's Song (Bury version)")[0] == 8
