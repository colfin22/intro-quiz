# Intro Quiz

[![CI](https://github.com/colfin22/intro-quiz/actions/workflows/ci.yml/badge.svg)](https://github.com/colfin22/intro-quiz/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/colfin22/intro-quiz)](https://github.com/colfin22/intro-quiz/releases)
[![License: MIT](https://img.shields.io/github/license/colfin22/intro-quiz)](LICENSE)

Self-hosted "guess the intro" music quiz for family game night, built on your own
[Navidrome](https://www.navidrome.org/) library. A song's **first 5 seconds** play
and everyone races to name it on their phones — fastest correct answer scores most.
Runs on a **cast display or Android TV** (scoreboard + album art + audio on the big
screen), or **just as happily on a cast speaker** — no scoreboard, phones carry the
questions, the speaker carries the music. All local — your music, your network, no
subscriptions.

<p align="center">
  <img src="docs/screenshots/board-question.png" width="90%"
       alt="The TV board mid-round — an animated wave and clip progress, no spoilers">
</p>
<p align="center">
  <img src="docs/screenshots/board-reveal.png" width="90%"
       alt="The TV board on a reveal — album art, who got it right, and the scores">
</p>
<p align="center">
  <img src="docs/screenshots/phone-artist-wall.png" width="24%"
       alt="The lobby artist wall — every player picks three artists they know">
  <img src="docs/screenshots/phone-question.png" width="24%"
       alt="A round on the phone — four choices and a countdown">
  <img src="docs/screenshots/phone-reveal.png" width="24%"
       alt="The reveal on the phone — album art, who scored, and the standings">
  <img src="docs/screenshots/phone-fact.png" width="24%"
       alt="Half time — this phone got a trivia fact to read out to the table">
</p>

## How a game works

1. Everyone opens the app on their phone (a plain LAN web page) and joins with a name.
   Whoever started the game is the **game master** — only their phone gets the
   start / next-song / half-time controls. A 🎤 banner on every phone shows who's
   in charge, and **the master's chair rotates each game**: the final screen
   announces who runs the next one (if they're not playing, anyone can take over
   from the lobby).
2. While waiting in the lobby, each player **picks 3 artists they know** from a wall
   of your library's most popular artists (freshly randomised each game) — one song
   per player's picks is shuffled invisibly into the rounds, so guests and kids
   aren't slaughtered by the host's record collection. The game won't start until
   everyone's locked in or skipped.
3. Each round: a clip plays on the TV (an animated wave with a live progress bar —
   no spoilers), four choices appear on the phones (decoys from the same era, never
   the same artist twice), 20-second window, speed bonus, early reveal when everyone
   has answered. Phones buzz at each round start; a round nobody answered replays
   once before revealing.
4. Stuck? Anyone can extend the clip (5 → 10 → 20 seconds).
5. The reveal shows album art and **who got it right** while a "payoff" chunk of
   the song plays — **in full**: the next-song button stays locked with a countdown
   until the music finishes. No skipping the good bit.
6. At the midpoint, the game breaks for a **half-time show**: every player's phone gets a music
   fact to read out to the table, then three quick **true-or-false** questions —
   answered on the phones, question up on the TV, +50 points each on the main
   scoreboard, auto-revealed once everyone's in.
7. Rubbish clip (applause intro, ambient noise)? The game master's reveal screen has
   a **🚫 bad clip** link — two taps to confirm — that bans the track forever.
8. Ten rounds a game, a 🎺 fanfare on the final scores, persistent all-time
   leaderboard.

<p align="center">
  <img src="docs/screenshots/board-halftime.png" width="90%"
       alt="Half time on the TV board — a true-or-false question for the table">
</p>

## How it works under the hood

- **Library sync** — walks your whole Navidrome library over the Subsonic API into
  SQLite (tracks, artists, durations).
- **Recognisability scoring** — two signals per track: *family* (Navidrome play
  counts + stars) and *global* (Last.fm listeners via `track.getInfo`). Blended into
  difficulty tiers: your favourites are "easy"; world-famous songs you own but never
  play are "medium" — the sweet spot where everyone has a chance.
- **Clip cutting** — a background job downloads originals and cuts loudness-normalised
  MP3 clips with ffmpeg (5/10/20s intros + a payoff from ~40% in), working through the
  library in global-popularity order. **Silence-aware**: if a track opens with a long
  quiet stretch (rain, feedback, ambience — looking at you, metal and post-rock), the
  intro clips start where the audible song does (`silencedetect`, capped at 60s; re-cut
  existing tracks via `POST /api/clips/recut?q=%pattern%`). **ID3 tags are stripped and
  re-titled** so a display's now-playing overlay can't leak the answer. Tracks over 12 minutes (DJ
  mixes) are excluded; whole albums can be banned by pattern (`POST /api/ban/album`).
  Undecodable originals retry via the music server's transcode before being banned,
  and a stream that returns an error document (stale index after files were renamed)
  is recognised rather than fed to ffmpeg.
- **The game engine** — one websocket hub (FastAPI), phases lobby → question → reveal.
  Rounds are built lazily at first start so artist picks land first. Answer timing is
  server-side; the correct answer never ships to clients before the reveal.
- **The TV board** — a second web page cast to the display via DashCast
  (pychromecast). The board **plays the round audio itself** through a hidden
  `<audio>` element — casting clips as media would evict the scoreboard, because a
  cast device runs one app at a time. Audio is served from an anonymous
  per-round endpoint so phones can't extract the track id mid-round. Android TV
  (e.g. Nvidia Shield) autoplays; touch displays (Nest Hub) need one tap to unlock
  sound — the board shows an overlay asking for it. If the cast session dies
  mid-game the app re-casts the board automatically, and the board reports playback
  failures back to the server log.
- **Half-time trivia** — a curated seed pack ships in the repo (~180 read-aloud music
  facts + ~215 true/false questions, **deliberately Irish/UK-centric** — Eurovision,
  Thin Lizzy and Westlife feature) and lives in SQLite; the true/false pool tops
  itself up from [Open Trivia DB](https://opentdb.com/) whenever it runs low
  (`POST /api/trivia/topup`, also called automatically at game start). Picks prefer
  never-used items and recycle oldest-first, so repeats take months. Answers never
  ship to phones before the reveal. Not your region? See
  [Make your own trivia pack](#make-your-own-trivia-pack) below.
- **Speaker-only mode** — pick "no scoreboard" at game start and clips cast to a
  speaker via Home Assistant + Music Assistant instead; the phones do the rest.
  A display isn't required to play.
- **Upkeep** — schedule the four maintenance endpoints nightly with whatever you
  like (cron, systemd timer): `POST /api/sync`, `/api/score/lastfm`,
  `/api/score/tiers`, `/api/clips/cut` — the library re-syncs, new tracks get
  scored, tiered and clipped. Clips cost ~2 MB per track.

## Run

**You need:** a [Navidrome](https://www.navidrome.org/) server, Docker, a free
[Last.fm API key](https://www.last.fm/api) — and **at least one audio output**:
a cast display / Android TV, **or** Home Assistant + Music Assistant for a
speaker. With neither, the game is silent and unplayable — the clips have to
play *somewhere*. (Running outside Docker? Python 3.12+ and ffmpeg required.)

    docker compose up -d --build

Copy [`.env.example`](.env.example) to `.env` beside `docker-compose.yml` and
fill it in — it marks which variables are required:

    NAVIDROME_URL=http://navidrome.local:4533
    NAVIDROME_USER=quiz                     # a dedicated non-admin Navidrome user
    NAVIDROME_PASSWORD=<its password>
    LASTFM_API_KEY=<free key from last.fm/api>
    DISPLAYS=Living Room TV=192.168.1.50    # Name=ip pairs, comma-separated (cast targets)
    BOARD_URL=https://quiz.example.com/board # MUST be https:// — cast devices silently refuse HTTP
    # optional Home Assistant fallback (speaker audio when no display is used):
    HA_URL=http://homeassistant.local:8123
    HA_TOKEN=<long-lived token>
    MEDIA_PLAYER=media_player.living_room_speaker
    APP_BASE_URL=http://<this host>:8000
    CAST_ENABLED=true
    # optional: family devices with fixed IPs get their name prefilled at join
    KNOWN_PLAYERS=Alice=192.168.1.20,Bob=192.168.1.21
    # first install: cut clips for the WHOLE library in one long session at startup
    CLIP_SWEEP_ON_START=true
    CLIP_SWEEP_MAX_HOURS=8    # optional cap per session (0/unset = run until done)

Then one call does the whole first-time setup:

    curl -X POST http://<host>:8000/api/bootstrap

It chains library sync → Last.fm scoring → difficulty tiers → clip cutting as a
background job; watch progress in `/health` (which also reports `ready_to_play`)
or the logs. It's resumable — if it stops (Last.fm hiccup, restart), POST it
again and it continues where it left off. The individual steps also exist for
nightly scheduling: `POST /api/sync`, `/api/score/lastfm`, `/api/score/tiers`,
`/api/clips/cut`. Phones open `http://<host>:8000`; the board lives at `/board`.

**Bootstrapping the clips:** with `CLIP_SWEEP_ON_START=true`, every container start
kicks off a background session that cuts clips until every tiered track has them —
for a big library that's hours (the download from Navidrome is the bottleneck, not
ffmpeg), and progress is logged batch by batch in `docker logs`. It only cuts
*tiered* tracks, so run the sync → Last.fm scoring → tiers steps first, then
`docker compose restart`. It's safe to leave enabled permanently: a start with
nothing to cut exits immediately, and newly-scored tracks get swept up on the next
restart. If Navidrome is unreachable it backs off and gives up after an hour
rather than hammering. Don't want it monopolising your music server all day?
`CLIP_SWEEP_MAX_HOURS=8` stops the session cleanly after 8 hours (finishing the
batch in hand) — the next restart picks up exactly where it left off.

The Navidrome user needs the standard Subsonic permissions plus **download and
streaming enabled** — the clip cutter pulls originals via `download` and falls
back to `stream` (server transcode) for undecodable files. A default non-admin
user works on stock Navidrome; if clip cutting 403s, check those two toggles
on the user.

Clips land in `CLIPS_DIR` (container path `/clips`; the host side defaults to
`./clips` next to the compose file — set `CLIPS_HOST_DIR` in `.env` to put them
somewhere roomier, e.g. `/mnt/tank/clips` or `C:\quiz-clips`). As a real-world
sizing example: a
**565 GB / ~40,000-track library** cuts down to roughly **80 GB of clips**
(~2 MB per track — four loudness-normalised MP3s each).

**Windows?** Yes — anywhere Docker runs, including Docker Desktop (WSL2).
Set `CLIPS_HOST_DIR=C:\quiz-clips` in `.env` (or keep the default `./clips`),
and allow port 8000 through Windows Firewall so the phones can reach it. Casting still works from Docker Desktop because displays
are addressed by IP (`DISPLAYS=...`) — no mDNS discovery needed. Navidrome and
the optional Home Assistant bits can live anywhere on the network.

## Make your own trivia pack

The shipped half-time pack is Irish/UK-centric. To localise it, put a
`trivia_custom.json` in the app's data directory — with the default
`docker-compose.yml` that's `./data/trivia_custom.json`, right beside `quiz.db`.
It's a flat JSON list of two kinds of item:

    [
      {"kind": "fact", "text": "Johnny Cash proposed to June Carter on stage in London, Ontario."},
      {"kind": "tf",   "text": "Gordon Lightfoot wrote 'Early Morning Rain'.", "answer": 1},
      {"kind": "tf",   "text": "Céline Dion is from Vancouver.", "answer": 0}
    ]

- `fact` items are read aloud by a player at half time — write them as spoken
  sentences, and only include things you'd defend at your own kitchen table.
- `tf` items need `"answer": 1` (true) or `0` (false). Keep a healthy share of
  falses (the shipped pack runs ~60/40) or the table learns to always guess true.
- The pack seeds automatically at the next game start (or `POST /api/trivia/topup`).
  Malformed items are skipped with a log warning, never fatally. Duplicate texts
  are ignored, so you can keep growing the file and re-seeding.
- Set `TRIVIA_BUILTIN_PACK=false` in `.env` **before your first game** to skip
  the shipped pack entirely — items already seeded stay in the bank (pruning
  after the fact is a `DELETE FROM trivia WHERE source='seed'` in `data/quiz.db`).
- An LLM drafts a regional pack in minutes. Copy this prompt, swap the region,
  and paste the output into `data/trivia_custom.json` — but **fact-check what it
  writes before your family reads it out with confidence.** LLMs state wrong
  "facts" fluently.

      Write me a music trivia pack for a family quiz night, as a single JSON
      list and nothing else. Two kinds of item:
        {"kind": "fact", "text": "..."}                 — a fun music fact one
          player reads aloud to the table
        {"kind": "tf", "text": "...", "answer": 1 or 0} — a true/false question
          (1 = true, 0 = false)

      Produce 60 facts and 80 true/false questions about popular music from
      <YOUR REGION/COUNTRY>, plus internationally famous acts as heard from
      there. Rules:
      - Only well-established, easily verifiable claims — no obscure trivia,
        no disputed stories. If a story is folklore, start it with "Legend has
        it" or "As the story goes".
      - Facts must read naturally when spoken aloud, one or two sentences.
      - False T/F statements must be plainly false, not technicalities.
      - Make roughly 40% of the T/F items false, and don't cluster them.
      - Family-friendly; span the 1960s to today; vary the artists — no more
        than three items about any one act.
      - Output raw JSON only: no markdown fences, no commentary.

## Notes

- Navidrome play counts are per-user; the family score aggregates the `annotation`
  table exported from Navidrome's DB and posted to `POST /api/ingest/annotations`
  (rows of `{"id", "play_count", "starred"}` summed across your users).
- The board URL must be HTTPS with a real certificate — put the app behind a reverse
  proxy (with websocket support) for that.
- Tests: `python -m pytest tests/` (includes a node-based smoke that renders every
  phone-UI phase — a thrown render fails CI instead of shipping a half-drawn screen).
- The all-time leaderboard can be wiped with `POST /api/leaderboard/reset?confirm=yes`.
- **Mis-tag detection:** scrappy rips sometimes carry junk in the *subtitle* tag
  ("Teenage Kicks (PMEDIA)"), which breaks Last.fm matching — the track scores zero
  and never gets picked. `GET /api/quality` lists tracks that score ~no listeners while
  their artist is clearly popular (the tell-tale of a mangled title); a periodic
  `POST /api/quality/check` pushes fresh suspects via Home Assistant if
  `HA_NOTIFY_SERVICE` is set. Run `POST /api/quality/check?push=false` once after
  install to baseline your library so only future misses alert. Fix = clean the tags,
  rescan your server, then re-sync and re-score (`POST /api/bootstrap` handles it).

## Licence

Built by Colm Finn — [MIT licensed](LICENSE).
