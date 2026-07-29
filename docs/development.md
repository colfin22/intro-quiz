# Running it locally

For working on the code. You don't need Navidrome, Home Assistant, a cast device or
Docker — a seeded database of ffmpeg tones stands in for the library.

**You need:** Python 3.12+ and ffmpeg (`brew install ffmpeg` / `apt install ffmpeg`).

    make setup     # .venv + requirements + pytest
    make seed      # ./devdata/quiz.db + ./devclips — 40 invented, pre-clipped tracks
    make dev       # uvicorn --reload on :8000 (seeds first if the DB is missing)

Then open `http://localhost:8000` in two browser windows to join as two players, and
`http://localhost:8000/board` for the TV board. `make dev` copies `.env.local.example`
to `.env.local` on first run; edit that for anything you want to change.

Everything dev lives outside the production paths and is gitignored: `QUIZ_DB` and
`CLIPS_DIR` point at `./devdata` and `./devclips` instead of the container's `/data`
and `/clips`, and config comes from **`.env.local`**, never `.env`. `make clean-dev`
deletes the lot. `CAST_ENABLED=false` is the default so a dev run can't blurt a clip
out of a real speaker.

The seeded "tracks" are pure tones, one pitch each (`scripts/seed_dev_db.py`). That's
deliberate — with silence, an audio-routing bug looks exactly like a working one,
whereas a wrong-clip bug is audible the moment every track has its own note. They're
inserted pre-tiered and pre-clipped, cut by the *real* clip cutter, so the dev clips
match production's shape.

## Tests

    make test      # pytest
    make test-js   # both node smokes

`make test-js` runs `tests/js/render_smoke.js` and `admin_smoke.js`, which execute the
phone UI's and admin page's `render()` against every phase/snapshot in a stub DOM. They
exist because UI regressions shipped past green python tests: a `render()` that throws
halfway leaves a half-drawn screen, and an element id referenced in JS but absent from
the HTML fails silently in a browser. Both are cheap and worth keeping green.

Timing is tested, not slept: `Game` takes an injectable `clock` (`app/game.py`), so
speed-bonus and deadline behaviour is asserted at whatever "time" the test likes.

## Testing the integrations

The seeded DB covers the game itself. For the parts that talk to something real, fill
in the optional blocks in `.env.local`:

- **Navidrome** — sync, Last.fm scoring and clip cutting against the real library. Safe
  (read-only from Navidrome's side) but it downloads real audio.
- **Home Assistant** — the `/admin` Speakers tab, and grouping. Note `APP_BASE_URL`
  must be an address **HA can reach**, i.e. your machine's LAN address; `localhost`
  means HA fetching itself and finding nothing. Casting also needs
  `CAST_ENABLED=true`, which is off by default here on purpose.

`tests/test_ha.py` fakes the HTTP layer, so the HA code paths are covered without a
live HA — including the entity-pair distinction (play on the Music Assistant entity,
group on the native one) that fails silently when it's the wrong way round.
