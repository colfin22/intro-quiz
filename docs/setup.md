# Setup details

Configuration beyond the quick-start in the [README](../README.md#run). For getting a
trusted HTTPS cert for casting, see [https-lan.md](https-lan.md).

## The one-time bootstrap, in detail

`POST /api/bootstrap` chains the whole first-time setup as a single background job:
library sync → Last.fm scoring → difficulty tiers → clip cutting. Watch progress in
`/health` (which reports `ready_to_play`) or in `docker logs`. It's resumable — if it
stops partway (a Last.fm hiccup, a restart), POST it again and it continues where it
left off rather than starting over.

## Ongoing clip cutting

With `CLIP_SWEEP_ON_START=true`, every container start cuts clips in the background
(batch by batch, visible in `docker logs`) until every *tiered* track has them — hours
for a big library, bottlenecked on the Navidrome download, not ffmpeg. Run sync →
scoring → tiers first (or the full bootstrap), then `docker compose restart` to kick off
a sweep.

It's safe to leave on permanently:

- a start with nothing to cut exits immediately;
- newly-scored tracks get swept up on the next restart;
- it backs off if Navidrome is unreachable rather than hammering it.

`CLIP_SWEEP_MAX_HOURS=8` caps a single session (finishing the batch in hand); the next
restart resumes where it left off. Set it to `0` (or leave it unset) to run until done.

## Running the setup steps individually

Instead of the one `POST /api/bootstrap` call, you can drive the stages separately —
handy for running them on a nightly schedule rather than one big bootstrap:

- `POST /api/sync` — pull the library from Navidrome
- `POST /api/score/lastfm` — score tracks by Last.fm listeners
- `POST /api/score/tiers` — sort scored tracks into difficulty tiers
- `POST /api/clips/cut` — cut clips for tiered tracks

## Navidrome user permissions

The Navidrome user needs the standard Subsonic permissions plus **download and streaming
enabled** — the clip cutter pulls originals via `download` and falls back to `stream`
(server transcode) for undecodable files. A default non-admin user works on stock
Navidrome; if clip cutting 403s, check those two toggles on the user.

## Clip storage and sizing

Clips land in `CLIPS_DIR` (container path `/clips`; the host side defaults to `./clips`
next to the compose file). Set `CLIPS_HOST_DIR` in `.env` to put them somewhere roomier,
e.g. `/mnt/tank/clips` or `C:\quiz-clips`.

As a real-world sizing example, a **565 GB / ~40,000-track library** cuts down to roughly
**80 GB of clips** — about **2 MB per track** (four loudness-normalised MP3s each: the
5s / 10s / 20s intro clips plus a payoff snippet).

## Windows

Works anywhere Docker runs, including Docker Desktop (WSL2). A few Windows-specific notes:

- Set `CLIPS_HOST_DIR=C:\quiz-clips` in `.env` (or keep the default `./clips`).
- Allow port 8000 through Windows Firewall so the phones can reach the app.
- Casting still works from Docker Desktop because displays are addressed by IP
  (`DISPLAYS=...`) — no mDNS discovery needed.
- Navidrome and the optional Home Assistant bits can live anywhere on the network; only
  the quiz app itself has to be reachable by the phones and the cast devices.
