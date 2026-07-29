# What's different in this fork

This is a fork of [colfin22/intro-quiz](https://github.com/colfin22/intro-quiz) running
as `giantsystems/intro-quiz`. Everything upstream still works the same way; this page
lists only what diverges, so the rest of the docs can stay about the app itself.

Two kinds of change live here: **features** (worth upstreaming) and **deployment
adaptations** (specific to this server, and the reason you can't `git pull` upstream
over this checkout without conflicts).

---

## Features

### Remote players

Anyone not in the room picks **"somewhere else"** at join and the round clips stream to
their own phone instead of only playing in the room. Their speed bonus is measured from
when *their* audio actually began, not the room's, so buffering doesn't cost them points.
Full detail — including the 5s cap on that credit and why phones in the room stay silent
— is in [setup.md](setup.md#remote-players).

### Speaker picking and grouping from `/admin`

A **Speakers tab** lists every media player Home Assistant can see, remembers your choice
in the database, and takes effect on the next round rather than needing a restart. Tick
two or more and they're grouped for the game and restored afterwards. See
[setup.md](setup.md#choosing-the-speaker-and-grouping-several) — including the trap that
playing and grouping use *different* entities for the same speaker.

### All-time top scores on the phone

The persistent leaderboard already existed and was already on the TV board; the phones
now show it too, on the join screen and under the final scores, with your own row
highlighted. See [setup.md](setup.md#all-time-top-scores).

### The game master can skip the payoff song

Upstream locks the next-song button for the payoff clip's full 12 seconds every round
("no skipping the good bit"). Here it holds for a two-second grace — long enough that
nobody skips the answer before it's read — and then goes live, showing how much song is
left, so the master chooses whether to let it run.

That also fixed a real bug rather than just changing a preference. The base `button` CSS
rule sets `cursor:pointer`, and nothing overrode it for `:disabled`, so a locked button
showed a hand on hover and then swallowed the tap in silence — disabled buttons fire no
`onclick`, so there wasn't even an error banner. Locked and broken were indistinguishable,
and it got reported as broken. Disabled buttons across the phone UI are now greyed and
show `not-allowed`.

### Library hygiene and artist-variant folding

Upstream assumes reasonably clean tags. A 23,000-track library built up over two decades
across a NAS and an old iTunes library is not clean, and the damage showed up in the game:
the artist wall split `AC/DC` into four entries of a dozen tracks each (below the wall's
cutoff, so the band vanished), and two spellings of one band could be offered as two
separate answer options in the same question.

New `app/library.py` normalises artist spellings onto one key and finds the rows worth
banning. The design decision worth keeping on a merge is that **variant spellings are folded,
never deleted**, and duplicates require an **exact duration match** — a title+artist dedupe
looks obvious and deletes real music (`The Metallica Blacklist` has 12 different "Nothing
Else Matters" covers filed under `album_artist=Metallica`). Measured against a copy of
production: 233 variant groups, 816 true duplicates, 47 mistagged rows, 65 too short.

`app/retag.py` then writes the agreed spelling back into the files' tags as a background
job. It's the only part of the app that writes to the music library, so it's off unless
`MUSIC_DIRS` is set and previews unless asked to write, and it never renames a file
(Navidrome ids tracks by path, so a rename orphans the clips). See
[setup.md](setup.md#artist-tag-repair-optional).

### A local development environment

`make setup && make seed && make dev` runs the app on a laptop with **no Navidrome, no
Home Assistant and no Docker** — a seeded database of fake tracks with silent generated
clips. See [development.md](development.md).

---

## Deployment adaptations

These are specific to this deployment. They're the reason merging upstream needs care.

### `docker-compose.yml`

| Change | Why |
|---|---|
| Published port `8001:8000` instead of `8000:8000` | Port 8000 on the server is Portainer. **Worth knowing when debugging:** curling `localhost:8000/health` there returns a bare `OK` from Portainer, which looks like a healthy app but isn't. |
| `:Z` on the `/data` and `/clips` mounts | SELinux is enforcing on AlmaLinux/RHEL; without relabelling the container can't write `/data`. Harmless on other hosts. |
| `${MUSIC_HOST_DIR:-./data}:/music-src`, deliberately **without** `:Z` | The optional library mount for the retag job. `:Z` relabels the tree, which needs xattrs; the library is a CIFS share (`//10.0.1.68/nas-ssd` at `/mnt/nas`) and has none, so `:Z` there stops the container from starting. On this host the share also needs `sudo setsebool -P virt_use_samba on` — currently **off**, so the retag job can't see the library until it's set. |
| `image:` points at `ghcr.io/giantsystems/intro-quiz` | This fork's tag. Nothing is pulled by default — it's just the local tag `--build` produces. |
| `CLIPS_HOST_DIR` points at the server's local SSD | Clips are large and live on `/mnt/data`, not on the NAS share. Falls back to `./clips` so a fresh checkout still works. |

### No `.git` on the server

The deployed copy at `/home/steve/intro-quiz` is a plain rsync'd directory, so deploys
are file syncs rather than `git pull`. Exclude `.env`, `data/` and `clips/` when syncing
— `rsync --delete` without those exclusions would take out the config, the database and
the clip library.

### Version numbering

This fork's `app/__init__.py` version has moved ahead of upstream's independently. Expect
a conflict there on any merge; it isn't meaningful, just take the higher number.

---

## Merging from upstream

`upstream` is configured, so `git fetch upstream` works. Files most likely to conflict,
in rough order of likelihood: `docker-compose.yml` (every line above), `app/__init__.py`
(version), `README.md` (feature lists), and `app/static/quiz.js` / `app/game.py` where
the payoff-lock behaviour deliberately differs from upstream's "plays in full" design.

Upstream's badges and image references in the README still point at `colfin22`, which is
correct — the CI badge reflects upstream's build, not this fork's.

> **Note:** GitHub Actions is not enabled on this fork, so no CI has run on any of its
> pull requests. `make test` and the two node smokes only ever run locally. Worth
> enabling if this fork gets more contributors than one.
