# HTTPS on a home network

Cast devices are strict: a Chromecast/Android TV **display**, and a Google/Nest
**speaker**, both fetch the board and the clip audio over TLS and will silently
refuse to play if the URL is plain HTTP or uses a self-signed certificate. That
rules out the usual quick LAN tricks (`mkcert`, self-signed certs, raw IPs) — the
certificate has to be one a cast device already trusts, i.e. a real one from a
public certificate authority.

The good news: you don't need to expose the app to the internet to get one. This
guide covers the whole picture — a trusted cert **and** making the name resolve on
your LAN so casting actually works.

You need three things:

1. **A domain you own** (a cheap one is fine, e.g. `quiz.example.com`). You can't
   get a public cert for a made-up or `.local` name.
2. **A reverse proxy with WebSocket support** in front of the quiz app — the phone
   UI uses a live WebSocket, so the proxy must pass `Upgrade`/`Connection` headers.
   [Caddy](https://caddyserver.com/) (automatic HTTPS, simplest),
   [Nginx Proxy Manager](https://nginxproxymanager.com/) (web UI) and Traefik all work.
3. **A cert**, via Let's Encrypt — two ways, below. Pick by whether you're happy
   to forward a port:
   - **DNS-01** *(recommended)* — opens **no ports at all**; validates through your
     DNS provider. Best for a purely-internal box, and the only option if your ISP
     blocks port 80.
   - **HTTP-01** *(not recommended)* — simplest to configure, but needs port 80
     forwarded from the internet to your box, which you don't otherwise need for a
     LAN-only game. Only worth it if you're already exposing the machine.

---

## Step 1 — get the certificate

### DNS-01 — no ports opened *(recommended)*

With a **DNS-01** challenge the proxy proves you own the domain by creating a
temporary `TXT` record via your DNS provider's API — so **nothing needs to be
forwarded or exposed**. That makes it the natural fit for a box you only ever reach
on the LAN, and it's the only option that works if your ISP blocks inbound port 80.

You need your domain on a DNS provider with an API (Cloudflare, deSEC, Route 53,
DigitalOcean, Gandi, Njalla and dozens more are supported) and an API token scoped
to edit that zone.

Caddy needs a build that includes the **DNS plugin for your provider**, and the
`dns` directive names that provider. So the config is the same shape whatever you
use — just swap `<provider>` and the token variable. For example, with Cloudflare:

```caddy
quiz.example.com {
    reverse_proxy localhost:8000
    tls {
        dns cloudflare {env.CF_API_TOKEN}
    }
}
```

For another provider you'd use its plugin name and its own credentials (e.g.
`dns route53` with AWS keys, `dns digitalocean {env.DO_AUTH_TOKEN}`) — check your
provider's Caddy DNS module for the exact directive. Nginx Proxy Manager wraps all
of this in its UI: SSL tab → *Use a DNS Challenge* → pick your provider from the
list and paste its credentials. Renewals are automatic and never need a port open.

### HTTP-01 — forwards port 80 *(not recommended)*

HTTP-01 is the simplest to configure, but it needs **port 80 forwarded from the
internet** to your box — an exposure a LAN-only game doesn't otherwise need, so
prefer DNS-01 above. It's only worth it if you're already exposing the machine.

Point your domain's public DNS `A` record at your router, forward ports **80 and
443** to the machine running the proxy, and Caddy fetches and renews the cert on its
own (Let's Encrypt validates with a one-off request to your server on port 80). The
config is just two lines — no `tls` block needed:

```caddy
quiz.example.com {
    reverse_proxy localhost:8000
}
```

(Nginx Proxy Manager: add a Proxy Host → forward to the app's `host:8000`, tick
**Websockets Support**, then the SSL tab → *Request a new certificate* with *Force
SSL*.) Once issued you only need port 443 open, and only if you want off-LAN access.

---

## Step 2 — make the name resolve on your LAN

A public cert isn't enough: the phones **and** the cast devices have to resolve
`quiz.example.com` to the machine on your network. Two approaches.

**Simplest — point the public DNS record at the LAN IP.** Set the public `A`
record for `quiz.example.com` straight to the app's private address, e.g.
`192.168.1.10`. Everything on the LAN (including Google/Nest speakers, which use
their *own* public DNS and ignore local overrides) resolves it to that address and
connects directly over your network. Nothing is actually exposed — a `192.168.x.x`
/ `10.x.x.x` address isn't routable from the internet — so this is safe and it's
the option that reliably works with speakers.

**Cleaner — split-horizon DNS.** If you run local DNS (Pi-hole, your router's
local-DNS feature, etc.), override `quiz.example.com` → LAN IP internally while the
public record points elsewhere (or nowhere). This keeps your internal IP out of
public DNS. **Caveat:** Google/Nest speakers query external DNS directly, so a
split-horizon-only name won't resolve for **speaker** casting — for speakers you
still need the name resolvable publicly (i.e. the first approach). Split-horizon is
fine if you only ever cast to displays.

---

## Step 3 — point the app at it

In your `.env`, use the HTTPS name for the board (what cast devices fetch) and the
app base URL:

```env
BOARD_URL=https://quiz.example.com/board
APP_BASE_URL=https://quiz.example.com
```

Phones then open `https://quiz.example.com`; the board lives at `/board`. Cast a
clip — if it plays on both a display and a speaker, TLS, resolution and reachability
are all correct.

---

## Quick troubleshooting

- **Display casts, speaker doesn't** — the speaker can't resolve the name. It uses
  public DNS, so the name must resolve there (Step 2, first approach).
- **"Silently refuses to play", no error** — almost always HTTP, a self-signed
  cert, or an expired one. Load `https://quiz.example.com/board` in a normal browser;
  if you get a cert warning, a cast device will reject it too.
- **Phone UI connects but the game never advances / reconnects loop** — the proxy
  isn't forwarding WebSockets. Enable WebSocket support (Caddy does it automatically;
  NPM has an explicit toggle).
- **Cert won't issue on HTTP-01** — port 80 is probably blocked or not forwarded;
  switch to DNS-01.
