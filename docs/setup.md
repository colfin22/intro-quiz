# Setup details

Configuration beyond the quick-start in the [README](../README.md#run). For getting a
trusted HTTPS cert for casting, see [https-lan.md](https-lan.md); for running the app on
a laptop to work on it, see [development.md](development.md).

## The one-time bootstrap, in detail

`POST /api/bootstrap` chains the whole first-time setup as a single background job:
library sync → Last.fm scoring → difficulty tiers → **library clean-up** → clip cutting.
The clean-up sits deliberately before the cutting: every row it bans is a clip not cut, and
cutting is the expensive step. Watch progress in
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
- `POST /api/admin/run/hygiene` — ban duplicates and mistagged rows (see below)
- `POST /api/clips/cut` — cut clips for tiered tracks

Or skip the curls entirely: the **`/admin` server control page** runs each of
these (and the full pipeline) from the browser, with live progress and each
run's log output. Set `ADMIN_PASSWORD` in `.env` to password-gate it — once
set, scheduled curls must send `-H "X-Admin-Token: $ADMIN_PASSWORD"` as well;
left unset, everything stays LAN-open as before.

## Scan-to-join QR

The cast board's waiting/lobby screens (and the phone lobby) show a QR of the
join address. The encoded address is inferred — the board's own origin, falling
back to `BOARD_URL` — which is right for most setups. If your players should use
a *different* address than the board (e.g. the board is served via a
reverse-proxy hostname that guests can't reach), set `JOIN_URL` in `.env` to the
address phones should open.

### Guest-WiFi QR (optional)

Set `GUEST_WIFI_SSID` / `GUEST_WIFI_PASSWORD` (and `GUEST_WIFI_AUTH`, default
WPA, `nopass` for open networks) and the board shows a second QR that joins
guests to the WiFi — step 1 before the scan-to-join QR. **Use a guest network's
credentials, never your main one**: anyone who can reach the app can read this
QR. Unset = no second QR, nothing changes.

**Check the join URL is reachable FROM the guest network.** Guest networks are
deliberately locked down: most isolate guests from the main LAN, and many block
guests from connecting to *any* other host at all (client/AP isolation). Either
way a guest can join the WiFi (scan 1) but the join address (scan 2) times out.
Test it once from a phone on the guest SSID; if it can't reach the app, add a
firewall exception on your router allowing the guest network to reach the quiz
host and port only.

## Choosing the speaker (and grouping several)

With `HA_URL`/`HA_TOKEN` set, the **Speakers tab on `/admin`** lists every media player
Home Assistant can see and remembers your choice in the database — so switching rooms
doesn't mean editing `.env` and restarting. It takes effect on the next round; the
target is resolved per call, not at startup. `MEDIA_PLAYER` from `.env` stays the
default, and picking *"use .env default"* clears the override.

**Pick a Music Assistant entity.** HA usually exposes each speaker twice: the native
entity (`media_player.kitchen`) and a Music Assistant one
(`media_player.kitchen_music_assistant`, or a `_2` twin). Clips are played with
`music_assistant.play_media`, which only works on the MA entity — aim it at the native
one and you get a chime and no music, with a `200 OK` and nothing in the log. The
dropdown lists MA entities first for that reason; the rest are still selectable but
labelled. **Test** plays the fanfare on whatever's currently selected (before you save),
which is the quickest way to confirm you picked the right room.

Tick **two or more speakers** in *Group for a game* and they're joined into one group
when a game starts (`media_player.join`) and put back exactly as they were when it
finishes. Notes:

- Grouping uses the **native** entities — the ones that report `group_members` — which
  is why the checkbox list and the dropdown show different entities. Only groupable
  speakers appear; TVs and one-off cast targets don't have the capability.
- The prior arrangement is snapshotted first, so a group you use for something else
  survives a quiz. Restore runs ~30s after the final scores, so it doesn't cut the
  fanfare off and Play Again keeps the group.
- Grouping is best-effort: if HA is unreachable at game start it's logged and the game
  plays on the single target rather than refusing to start.
- One ticked speaker means no grouping — it's just the play target.

## All-time top scores

Every finished game writes each player's score to the database, and the totals show up in
three places without any configuration: the TV board (lobby and final screen), and on the
phones — on the join screen and under the final scores. Your own row is highlighted, and
each row shows the total plus how many games it came from.

It's cumulative across every game ever played and survives restarts and redeploys. Wipe it
from the **Scores tab on `/admin`** (two clicks) or with
`POST /api/leaderboard/reset?confirm=yes`. Players are matched **by name**, so the same
person on a different phone still adds to their own total as long as they type the same
name.

## Remote players

Anyone not in the room picks **"somewhere else"** on the join screen (or flips it from
the lobby) and the round clips stream to their own phone instead of only playing in the
room. Nothing to configure; it needs no HA and no cast device.

Their **speed bonus is measured from their own audio start**, not the room's, so a
couple of seconds of buffering doesn't cost them points: the phone sends an
`audio_started` message when playback actually begins and the server scores the elapsed
time from there. That timestamp is client-reported, so the credit is capped at 5s — a
phone can't claim a very late start and take its time. The answer window itself still
closes on the room clock, so a round can't be stretched.

Two things worth knowing:

- **Phones in the room stay silent.** Only players marked remote play audio, otherwise
  the room is a mess of echoing phones a beat apart.
- **Phone browsers block autoplay** until the page has been touched, exactly like the
  cast board. A remote player gets a "tap for sound" overlay on the first clip; one tap
  covers the rest of the game.

Remote players are marked 🌐 on the scoreboard and in the lobby, so it's obvious who's
listening on their own phone.

### Hosting from another room

A remote player can **start and run the game**: nothing ties the game master's chair to
being in the room. The common shape is one person elsewhere driving the quiz on their own
phone audio while everyone else listens together on the house speaker:

- The remote host taps **Start a new game**, picks *"somewhere else"*, and gets the clips
  on their own phone.
- Everyone in the room joins as *"in the room"*, so their phones stay silent and the
  music comes out of the speaker chosen on `/admin`.
- The host keeps the start / next-song / half-time controls wherever they are.

**This needs no TV board — and in fact only works without one.** The room speaker is used
only when no cast board is present: if a board is up, it plays the audio itself and the
speaker is deliberately skipped, so a remote host with a board running would leave the
room hearing whatever the TV is connected to rather than the speaker. Either put the board
on the TV in the room *or* use the speaker, not both.

One rough edge if you were casting earlier in the same session: after a board disappears
the app keeps expecting it for **120 seconds** (so a websocket blip doesn't bounce audio
onto a speaker mid-round). Start a speaker-only game inside that window and the first
round or two can be silent in the room. Wait a couple of minutes after turning the TV
board off, or turn it off before starting.

## Library clean-up (duplicates and mistagged rows)

Real libraries carry junk that makes the quiz worse: the same song ripped twice, and rows
whose tags are damaged enough that the title no longer identifies the song. `GET
/api/library/audit` shows what a clean-up would do, without changing anything:

```bash
curl -s localhost:8000/api/library/audit | jq
curl -s 'localhost:8000/api/library/audit?detail=true' | jq   # the actual rows
```

Three categories, deliberately treated differently:

| Category | Rule | Action |
|---|---|---|
| `duplicate` | same title, artist **and exact duration** | keep one, ban the rest |
| `untrustworthy` | ≥3 rows sharing album + artist + title, differing durations | ban the whole group |
| `too_short` | shorter than `MIN_DURATION_S` (default 32s) | ban — too short to cut an intro from |

The exact-duration rule matters. A title+artist match looks like the obvious dedupe and
**would delete real music**: `The Metallica Blacklist` has 12 different "Nothing Else
Matters" covers by 12 artists, all under `album_artist=Metallica`. Conversely, a *spread* of
durations across one album+artist+title is the signature of a tagger that lost the
distinguishing part of the title (17 rows of "Fear and Loathing" ranging 154–367s), so there
is no "best" row to keep and the group goes.

Nothing is deleted — rows are flagged `banned` with a `ban_reason` naming the category. Sync
re-inserts by Navidrome id, so a delete would come straight back; a ban is also reversible
(`UPDATE tracks SET banned=0 WHERE ban_reason='duplicate'`). When picking which duplicate to
keep, a row that already has clips cut wins over a better-scored one that doesn't, so
cleaning never throws away work.

Run it with `POST /api/admin/run/hygiene`, or from `/admin`. It also runs inside
`POST /api/bootstrap`, before clip cutting.

Variant artist spellings are **not** in that table, because deleting them would be wrong —
see below.

## Artist-tag repair (optional)

`AC/DC`, `AC, DC`, `AC DC` and `ACDC` are one band, and the quiz already treats them as one:
an artist key normalises the spelling everywhere it matters, so the artist wall shows one
entry and two spellings can never appear as two answer options. That fix needs no
configuration and no writes.

What it can't fix is the *text*, which still reads `AC, DC` on the reveal screen. This job
rewrites the artist tag in the files themselves to the agreed spelling. It's **off by
default** — it's the only part of the app that writes to your music library.

To enable it, mount the library read-write and name the roots as seen inside the container:

```ini
# .env
MUSIC_HOST_DIR=/mnt/nas                 # host path, mounted at /music-src
MUSIC_DIRS=/music-src/music:/music-src/itunes/iTunes Music
```

`MUSIC_HOST_DIR` is a single mount, so point it at a parent of all your roots and list the
roots under `/music-src`. With `MUSIC_DIRS` unset the job refuses to run at all.

**If the library is on a network share and the host runs SELinux** (AlmaLinux/RHEL/Fedora,
`getenforce` says `Enforcing`), the container is denied access until you allow it:

```bash
sudo setsebool -P virt_use_samba on   # CIFS/SMB
sudo setsebool -P virt_use_nfs on     # NFS
```

Note this one mount deliberately has **no `:Z`** in `docker-compose.yml`, unlike the others:
`:Z` relabels the tree for SELinux, which requires xattrs, and a network share has none — so
`:Z` on a CIFS mount stops the container from starting at all.

Then preview, and only then apply:

```bash
curl -X POST 'localhost:8000/api/library/retag'             # dry run — writes nothing
curl -X POST 'localhost:8000/api/library/retag?write=true'  # applies it
```

The dry run returns the whole plan grouped by target spelling, with a per-file reason
(`'AC, DC' x14 (by album_artist)`, `'AC-DC' x12 (by majority)`) — worth reading before
applying. Per file it prefers, in order: the file's own `album_artist` when that folds to the
same artist; otherwise the most-used spelling across the scan, tie-broken toward the longer
string so `AC/DC` beats `ACDC`. A compilation's `album_artist` is ignored when it names a
different act, which is what stops all 12 Blacklist covers being retagged as Metallica.

Two things it will not do:

- **It never renames or moves a file.** Navidrome derives track ids from the path, so a
  rename re-ids the track and orphans every clip already cut for it. Mangled *folder* names
  (`AC+DC/`) are left alone: `/` is illegal in a path, and Navidrome reads embedded tags, not
  directory names, so they cost nothing.
- **It never touches a library it can't see**, and previews unless you pass `write=true`.

It's resumable in both phases — a scan cache keyed on mtime+size, and a write journal
flushed per file — so a container restart mid-run costs seconds, not the whole pass. A
22,000-file library scans in minutes from the server's local mount; the same scan over SMB
from a laptop runs at ~8 files/s, which is why this belongs on the server. For the laptop
equivalent, `scripts/retag_artists.py` takes `--root`, `--only`, `--map WRONG=RIGHT` and the
same `--dry-run` / `--write` split.

**After a write run, trigger a Navidrome rescan**, then check `GET /api/library/audit` — the
variant count should drop. Until the rescan, the app's DB still holds the old spellings.

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
