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
| `:Z` on both volume mounts | SELinux is enforcing on AlmaLinux/RHEL; without relabelling the container can't write `/data`. Harmless on other hosts. |
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
