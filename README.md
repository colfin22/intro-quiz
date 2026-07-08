# Intro Quiz

Self-hosted "guess the intro" music quiz for family game night, built on your own
[Navidrome](https://www.navidrome.org/) library. A song's **first 5 seconds** play
and everyone races to name it on their phones — fastest correct answer scores most.
Runs on a **cast display or Android TV** (scoreboard + album art + audio on the big
screen), or **just as happily on a cast speaker** — no scoreboard, phones carry the
questions, the speaker carries the music. All local — your music, your network, no
subscriptions.

## How a game works

1. Everyone opens the app on their phone (a plain LAN web page) and joins with a name.
   Whoever started the game is the **game master** — only their phone gets the
   start / next-song / half-time controls.
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
6. Games of 6+ rounds get a **half-time show**: every player's phone gets a music
   fact to read out to the table, then three quick **true-or-false** questions —
   answered on the phones, question up on the TV, +50 points each on the main
   scoreboard, auto-revealed once everyone's in.
7. Rubbish clip (applause intro, ambient noise)? The game master's reveal screen has
   a **🚫 bad clip** link — two taps to confirm — that bans the track forever.
8. Ten rounds a game, persistent all-time leaderboard.

## How it works under the hood

- **Library sync** — walks your whole Navidrome library over the Subsonic API into
  SQLite (tracks, artists, durations).
- **Recognisability scoring** — two signals per track: *family* (Navidrome play
  counts + stars) and *global* (Last.fm listeners via `track.getInfo`). Blended into
  difficulty tiers: your favourites are "easy"; world-famous songs you own but never
  play are "medium" — the sweet spot where everyone has a chance.
- **Clip cutting** — a background job downloads originals and cuts loudness-normalised
  MP3 clips with ffmpeg (5/10/20s intros + a payoff from ~40% in), working through the
  library in global-popularity order. **ID3 tags are stripped and re-titled** so a
  display's now-playing overlay can't leak the answer. Tracks over 12 minutes (DJ
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
- **Half-time trivia** — a curated seed pack ships in the repo (50 read-aloud music
  facts + 50 true/false questions) and lives in SQLite; the true/false pool tops
  itself up from [Open Trivia DB](https://opentdb.com/) whenever it runs low
  (`POST /api/trivia/topup`, also called automatically at game start). Picks prefer
  never-used items and recycle oldest-first, so repeats take months. Answers never
  ship to phones before the reveal.
- **Speaker-only mode** — pick "no scoreboard" at game start and clips cast to a
  speaker via Home Assistant + Music Assistant instead; the phones do the rest.
  A display isn't required to play.
- **Upkeep** — a nightly job re-syncs the library, refreshes scores, re-tiers and
  cuts new clips. Clips cost ~1.8 MB per track.

## Run

    docker compose up -d --build

Create a `.env` beside `docker-compose.yml`:

    NAVIDROME_URL=http://navidrome.local:4533
    NAVIDROME_USER=quiz                     # a dedicated non-admin Navidrome user
    NAVIDROME_PASSWORD=<its password>
    LASTFM_API_KEY=<free key from last.fm/api>
    DISPLAYS=Living Room TV=192.168.1.50    # Name=ip pairs, comma-separated (cast targets)
    BOARD_URL=https://quiz.example.com/board # HTTPS URL the display loads (cast devices refuse HTTP)
    # optional Home Assistant fallback (speaker audio when no display is used):
    HA_URL=http://homeassistant.local:8123
    HA_TOKEN=<long-lived token>
    MEDIA_PLAYER=media_player.living_room_speaker
    APP_BASE_URL=http://<this host>:8000
    CAST_ENABLED=true
    # optional: family devices with fixed IPs get their name prefilled at join
    KNOWN_PLAYERS=Alice=192.168.1.20,Bob=192.168.1.21

Then: `POST /api/sync` (library), `POST /api/score/lastfm?limit=...` (repeat until done),
`POST /api/score/tiers`, `POST /api/clips/cut?limit=...` — or just schedule them nightly.
Phones open `http://<host>:8000`; the board lives at `/board`.

Clips land in `CLIPS_DIR` (default `/clips` — bind-mount it somewhere roomy; the
mount in `docker-compose.yml` maps it).

## Notes

- Navidrome play counts are per-user; the family score aggregates the `annotation`
  table exported from Navidrome's DB (see `quiz-nightly.sh` for the pattern).
- The board URL must be HTTPS with a real certificate — put the app behind a reverse
  proxy (with websocket support) for that.
- Tests: `python -m pytest tests/` (includes a node-based smoke that renders every
  phone-UI phase — a thrown render fails CI instead of shipping a half-drawn screen).
- The all-time leaderboard can be wiped with `POST /api/leaderboard/reset?confirm=yes`.

## Licence

Built by Colm Finn — [MIT licensed](LICENSE).
