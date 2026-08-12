# Mini apps (webxdc)

Games, polls and shared editors, posted as a file and played inside the client.

A [webxdc](https://webxdc.org/) app is a `.xdc` — a zip with an `index.html` in it. The spec gives
such an app exactly one capability, `sendUpdate()` / `setUpdateListener()`, a shared append-only log
scoped to the message it arrived in, and takes away everything else: **no network access at all**.
Every game and poll in that ecosystem is built on that one primitive, which is why the same file runs
in Delta Chat, in Ditto and here.

## What it looks like

A post carrying a `.xdc` renders as a **cartridge** with a Play button — never an auto-running frame,
because an app is code somebody else wrote and it starts when the reader says so. On the desktop it
opens in its own movable window; on a phone, a full-screen sheet. Every move any player makes is a
Nostr event, so two people with the same post in their timeline are playing the same game.

**To post one:** compose → 📎 → `🎮 Mini app (.xdc)`.

**Half-Life** is the showcase: [`content.hl2dm.org/xdc/hl.xdc`](https://content.hl2dm.org/xdc/hl.xdc)
(178 MB — it ships the three free demo campaigns; source [webXash](https://github.com/x8BitRain/webXash)).
It plays the demos immediately, takes your own bought copy of Half-Life or Counter-Strike for the full
game, and supports multiplayer through the realtime channel. **Quake III** is the other:
[`quake3.xdc`](https://github.com/WofWca/quake3.xdc/releases/latest/download/quake3.xdc) (4.7 MB, and
it asks you to supply the demo data yourself — which is how it publishes an id Software game legally).

**Where to get apps:** [webxdc.org/apps](https://webxdc.org/apps/) is the store;
[codeberg.org/webxdc](https://codeberg.org/webxdc) is where most of them are developed, and each
repo publishes the `.xdc` as a git release asset — download that file and attach it. Anything posted
this way is playable by anyone whose client understands the tags above, here or in Ditto.

## The protocol

Ditto's `NOSTR_WEBXDC` draft (NIP-DC), implemented verbatim so games are portable between the two:

| | |
|---|---|
| The app | An `imeta` tag (NIP-92) with `m application/x-webxdc`, `url`, `x` (sha256) and a `webxdc` identifier — or a kind **1063** file-metadata event with the same as flat tags |
| A move | Kind **4932**, the identifier in an `i` tag, the `sendUpdate()` payload as content, `alt` per NIP-31 |
| Realtime | Kind **20932**, ephemeral — relays forward to current subscribers and store nothing, which IS the channel's semantic |

**The identifier, not the event, is what makes two people the same game.** Posting the same file
again mints a new one and starts a fresh game. It is generated per post.

**Serials are local.** The spec has them ordered and increasing, with gaps allowed, and says nothing
about them being the same for everyone — so this client assigns them by `(created_at, id)` over what
it has seen. That is what lets an append-only log ride a network with no global ordering: two devices
can disagree about the numbers and still agree about the *set*, which is all
`setUpdateListener(cb, lastSerial)` needs.

The app's own bytes are fetched **directly from wherever they were posted**, exactly like a remote
image in the timeline — the node stores only the apps posted through it. The archive is **never
unzipped whole**: the central directory is read and one entry is inflated per request, because
unzipping Half-Life eagerly would cost 178 MB of memory on top of the 178 MB of bytes before a frame
is drawn.

The `x` hash is verified **when it is a hash** — without that, whoever hosts the file could swap the
app after it was posted and nothing about the post would change. But the published Half-Life carries
`["x", "hl"]`, a label rather than a digest, so a non-sha256 `x` is ignored instead of enforced: the
tag is advisory in the wild, and refusing an app while accusing its author of tampering is worse than
a missing check on a file the reader chose to open.

## Where an app runs, and why it matters

An app is untrusted code. If it ran on the instance's own origin it could read the `localStorage` and
`IndexedDB` this client keeps **your Nostr key and your session** in. So it runs on a different
origin: **`xdc.<instance>`** — for this deployment, `xdc.poster.place`.

That origin serves exactly two files (`static/webxdc-sandbox/`): a loader, and a service worker. The
app's bytes never touch the server — the client downloads the `.xdc`, unzips it in the browser, and
answers the worker's requests over `postMessage`. Having nothing there to fetch is what makes "no
network access" true by construction rather than by promise, and there are two independent answers to
it: the worker refuses every cross-origin request, and the CSP on every response names no host.

There are **two frames**, which is not an accident: a service worker serves files by asking a page for
them, and a navigation has no page yet to ask — so the app runs in a nested frame while the loader
stays alive as the client the worker talks to.

### Setup, once — already done for poster.place

1. **DNS** — `xdc.poster.place` pointing where `poster.place` points (a CNAME is fine, proxied is
   fine).
2. **TLS** — `sudo certbot --nginx -d xdc.poster.place`, which issues a certificate of its OWN and
   registers its renewal. That matches how every other subdomain here is done (`ai.`, `adguard.`
   each have their own) and, unlike `--expand`, never touches the production `poster.place`
   certificate.

No wildcard and no DNS API token. The vhost is `nginx/webxdc-sandbox.conf.example`, deployed on
router.lan as `/etc/nginx/sites-enabled/webxdc.conf` (certbot rewrote its `ssl_certificate` lines
when it installed the new cert).

## Deploying this on your own node

Everything else about mini apps ships in the code: the client, the loader, the sandbox service
worker, the relay's kind-20932 exemption, the two routes the app answers. **The only thing a fresh
node is missing is the second hostname**, and missing it is silent — the composer still offers
`🎮 Mini app`, the post publishes, the cartridge renders, and Play opens a window that stays blank
forever. Nothing is requested from your server, so nothing appears in any log. The app warns about
exactly this once at startup (`[webxdc] xdc.<host> does not resolve …`), which is the only signal
there is.

### Step 1 — DNS (every deployment, no exceptions)

```
xdc.your-domain.com    CNAME    your-domain.com      # or an A/AAAA with the same address
```

Proxied through Cloudflare is fine — this is ordinary HTTPS on 443. (A **port** instead of a
hostname does *not* survive Cloudflare; that is measured, and written up above.)

### Step 2 — route it to the app

The app serves both paths the sandbox origin needs, gated on the `Host` header, so this is purely
your reverse proxy. Two paths, no static files, no extra service.

**nginx (bare metal) — one command:**

```bash
./install.sh --webxdc
```

It works out your instance hostname, **refuses before writing anything if `xdc.<host>` does not
resolve yet** (printing the record to add), installs a temporary HTTP-only vhost so the ACME
challenge can be answered, offers `certbot --nginx -d xdc.<host>`, then installs the real vhost from
`nginx/webxdc-sandbox.conf.example` — running `nginx -t` before every reload and rolling back if it
fails — and finally curls `https://xdc.<host>/__sandbox__/` and tells you what it got. Safe to
re-run; it never re-requests a certificate it already has and leaves an unchanged config alone.

Useful overrides: `WEBXDC_DOMAIN=` (skip the prompt), `WEBXDC_UPSTREAM=` (default `127.0.0.1:3051`),
`WEBXDC_SKIP_CERTBOT=1`, `WEBXDC_SKIP_DNS=1` (split-horizon DNS), `WEBXDC_DRY_RUN=1` (print
everything, change nothing).

**Docker compose with the bundled `proxy` service:** the vhost is already in the seeded config and
the first-boot self-signed certificate already covers `xdc.<domain>`, so mini apps work behind the
browser warning immediately. For a real certificate:

```bash
docker compose exec proxy certbot --nginx -d xdc.your-domain.com
docker compose exec proxy nginx -s reload
```

(The config is seeded from `POSTERCHANAI_DOMAIN` on the proxy's **first** boot only. An existing
deployment owns its `/etc/nginx/conf.d/posterchanai.conf` — copy the `xdc.` blocks out of
`docker/proxy/posterchanai.conf` into it by hand.)

**Caddy** — one line, certificate included:

```
xdc.your-domain.com { reverse_proxy posterchanai:3051 }
```

**Traefik** — one more label on the app container, alongside whatever router you already have:

```yaml
- "traefik.http.routers.pcxdc.rule=Host(`xdc.your-domain.com`)"
- "traefik.http.routers.pcxdc.tls.certresolver=le"
- "traefik.http.services.pcxdc.loadbalancer.server.port=3051"
```

**nginx-proxy / acme-companion** — add the name to the app container's environment:
`VIRTUAL_HOST=your-domain.com,xdc.your-domain.com` and `LETSENCRYPT_HOST` likewise.

**Anything else:** send `xdc.<your-domain>` to the same container/port as the client, over HTTPS,
passing the `Host` header through. That last part is load-bearing — the app decides which of the two
service workers to serve at `/sw.js` from `Host`, so a proxy that rewrites it hands mini apps the
PWA's worker and the app never starts.

### What you do *not* have to do

- **No new Python packages.** The two routes use only the standard library and what FastAPI already
  imports; `requirements.txt` is unchanged.
- **No wildcard certificate, no DNS API token, no second port.** Both were tried; see above.
- **Nothing to enable in Admin, and no restart.** The client derives the sandbox origin itself
  (`xdc.` + whatever host it was loaded from), which is why there is no setting for it and nothing
  that can drift out of step with the server.
- **No static files to serve.** The sandbox origin proxies to the app like every other path; the
  `.xdc` bytes never touch your server at all.

### Checking it

```bash
curl -sI https://xdc.your-domain.com/__sandbox__/ | head -1     # HTTP/2 200
curl -sI https://xdc.your-domain.com/sw.js | head -1            # HTTP/2 200
```

A **404** means the request is reaching nginx but not the app (or the `Host` header is being
rewritten). A **connection failure** means DNS or the firewall. A **certificate warning** means
certbot has not run for that name — and that one is fatal to mini apps rather than merely ugly,
because a service worker needs a secure context and the frame has no way to show you the warning.

### Two designs that were tried first

Both look right, and both are recorded so nobody spends the afternoon again:

- **A subdomain per app** (what Ditto does, via the third-party [iframe.diy](https://iframe.diy))
  is *better* — it isolates apps from each other as well as from the client. It needs a **wildcard
  certificate**, which certbot cannot issue over HTTP-01: that means DNS-01, which means a DNS
  provider API token living on the web server. Too much standing credential for a game feature.
- **A port** (`https://poster.place:8443`) is also a distinct origin and needs no new certificate at
  all, which made it the obvious answer. **Measured: it does not survive Cloudflare.** CF accepts
  8443 from the browser and then connects to the *origin* on 443, so the request lands on the main
  vhost and the sandbox is never reached — proven with a marker header, present on a direct request
  and absent through the CDN. Do not re-attempt it.

#### One origin means one worker, so every run carries a token

Because every app shares `xdc.<instance>`, every app shares **one service worker**, and that worker
has to decide which open game each request belongs to. It used to answer from the first client whose
path looked like a loader — so with Half-Life open, pressing Play on Quake III **started Half-Life**.
That is not a mix-up but a leak: one app's bytes delivered into another app's frame.

Each session now mints a token (a uuid), passed to the loader in its URL and on to the app's frame
from there; the worker answers only the loader holding the same one. It rides in the **query**, not
the fragment, because a fragment never reaches a worker (`request.url` is serialised without it) and a
**navigation** — which has no client id to look anything up by — is exactly the case with nothing else
to go on. Four ways to learn it, in order of certainty: the remembered client id (survives an app
rewriting its own URL, which Quake III does on boot), the request URL, the requesting client's URL,
then the referrer (an in-app link to a second page inherits no query). No token and exactly one loader
open is unambiguous and still answered — that is also what keeps an older cached client working. No
token and two loaders is a guess, and it is **refused**, because guessing is the whole bug.

### When an app will not start: Reset

Every mini app card carries a **Reset** button beside Play, and it exists because there was no way
out of an app that had saved itself into a state it could not start from. A mini app keeps stored
state in two places, neither of which a reader can see or reach:

- the **archive**, cached here under `pc-webxdc-v1` so a 178 MB game is downloaded once per device;
- whatever the **app itself** wrote on `xdc.<instance>` — `localStorage` and `IndexedDB`. An
  emscripten game keeps its entire config there, which is where a persisted video mode or renderer
  choice lives.

When either goes bad the app fails identically on every launch, for ever, and the only remedy a
reader could reach was the browser's *"clear browsing data"* — which also signs them out of this
instance. Reset does exactly that, scoped: it drops the cached archive, then hands the loader
`?__reset=1`, which wipes the sandbox origin's caches, IndexedDB, `localStorage` and service worker
before navigating itself to a clean boot **without** the flag (a flag that survived would wipe on
every load, and re-registering a worker in the document that just unregistered it is a race). It is
confirmed first, and the confirmation says that **IndexedDB is not namespaced per app**, so a reset
clears what every mini app on this instance has saved, not only the one being reset.

Two things measured while chasing exactly this, both worth not re-deriving:

- **`zip.js` never returns a window onto the archive.** Both compression methods produce a private
  buffer (`raw.slice()` for stored, a fresh array out of `DecompressionStream` for deflate) — checked
  against the real 178 MB Half-Life archive, all 21 entries, byte-identical to `unzip` and every one
  owning its own buffer. That matters because the fetch reply **transfers** the entry's
  `ArrayBuffer`, and a transfer detaches *the whole buffer* in this realm: a shared view would empty
  the archive on the first file served and every file after it would be zero bytes — an app that
  boots into nothing, silently, on every launch. The reply site no longer depends on that invariant
  holding two directories away; it checks the view owns its buffer and copies if it does not.
  `tests/test_webxdc.py::ServingDoesNotConsumeTheArchive` asserts both halves.
- **A cached archive that will not open is deleted and refetched.** It used to throw out of `load()`
  with the bad entry still in place, so the next launch failed the same way with nothing to do about
  it. `bytes.length > 0` was the only check; the sha is now verified too, when the `x` tag is a real
  digest.

### Reading an app's own error message

Half-Life's *"Failed to start multiplayer game. Make sure this app is running inside a
WebXDC-compatible messenger"* is a **catch-all `alert()`** around its whole multiplayer start, not a
feature detection — it blames the messenger for any failure, including its own downloads. The line
before it, `console.error("Failed to start multiplayer game:", e)`, is the real diagnostic. Its
genuine API check is in `electHost()` and says something else entirely
(*"webxdc.joinRealtimeChannel is not available"*), as does Quake III's, which prints into the page
body. Worth knowing before taking an app's word for what is wrong with the host.

## The trade this leaves

Every app shares one origin, so **an app can read another app's leftovers in `localStorage`**. Keys
are namespaced per app in the injected bridge, which is a collision guard, not a security boundary.
What is *not* given up, and what actually matters: nothing there can reach the client's storage, where
the key and the session are.

An instance that does have a wildcard certificate can set `pc_webxdc_wildcard` to `1` in the client's
localStorage and get an origin per app, which closes that gap too.

## Known limits

- **Realtime runs over a relay, not peer-to-peer.** `joinRealtimeChannel` works (ephemeral kind
  20932), which is what a continuously-moving game needs — Quake III uses it. Delta Chat's
  implementation is direct P2P (iroh); this one is a round trip to a relay, so expect latency a
  turn-based game will never notice and a twitch shooter certainly will. Three consequences worth
  knowing, all measured rather than assumed:
    - **One relay, not the pool.** A packet goes only to this instance's relay (`Relay.publishFast`),
      never through `publish()`'s fan-out. 30 packets a second times however many relays somebody has
      configured is a flood aimed at strangers' infrastructure, and pointless — the other player is
      subscribed here. It does mean two players on *different* relays will not see each other in
      realtime; the turn-based channel (kind 4932) federates normally and is unaffected.
    - **Every packet is a signature, and it is signed by NOBODY.** The realtime channel uses a
      secp256k1 key minted per session, held in memory and never stored — never the reader's identity
      key. Signing is local and costs **1.77 ms** (~560/sec on a desktop core), against **658 bytes on
      the wire per 200 bytes of game data**.

      It used to be signed with the account's key through the ordinary signer, and a real game shows
      why that could not stand: a moving player sends 20-30 packets a *second*, and with an external
      signer each one is a round trip to another program. Measured, a NIP-07 browser extension answers
      that with *"extension declined"* — in Brave, on a build where everything else about mini apps
      worked — so Half-Life announced no LAN server and nobody saw anybody. Amber or a bunker would be
      a prompt storm instead. Nothing is given up by dropping the identity: the spec is explicit that
      **nothing inside a mini app is authenticated** (`selfAddr` proves nothing, and a player can
      already claim to be anyone within the app), so identity there is the app's own business, carried
      in its payload. What is gained beyond a working channel is that continuous movement telemetry is
      no longer a stream of events tied to your npub.

      **The relay has to be told**, and this is the trap: its publishing gate is by author, and this
      key is in nobody's web of trust — so kind 20932 is exempted in `nostr_relay/server.py` beside
      the other ephemeral transports (NIP-46 signer traffic, call signaling), requiring the `i` tag so
      it is not an open forwarding pipe. Without that exemption every packet comes back *"blocked: not
      in web of trust"* and multiplayer dies quietly while single-player looks perfect.

      **The turn-based channel keeps the real key.** Kind 4932 is a durable, attributable move that
      belongs to the account and is read back by everyone who opens the post later; only 20932 —
      ephemeral, never stored, delivered to whoever is connected now — is unattributed.
    - So sending is **newest-wins and never queued**: a movement packet is worthless once a newer one
      exists, and a slow relay or a busy tab must never build a backlog of stale positions.
    - **The subscription's `since` is backdated two minutes, and that is load-bearing.** It was `now`,
      which reads as "from here on" and actually means "from here on *by the other player's clock*":
      the relay compares it against the sender's `created_at`, so a peer whose clock is a couple of
      seconds behind has **every packet dropped for the whole session** — an OK on their side, silence
      on ours, nothing logged anywhere. Measured against the live relay: a packet stamped 3s early
      never arrives. Two browsers on one machine share a clock and hide it completely; a phone and a
      laptop do not. Backdating costs nothing because 20932 is ephemeral — there is no stored backlog
      to replay, so the window only decides how much skew the channel survives.
- **Firefox works, and the evening spent concluding it could not is worth reading.** The symptom was
  `SecurityError: The operation is insecure` from `serviceWorker.register()` inside the sandbox frame,
  and it was diagnosed four times as a platform limit: Enhanced Tracking Protection (it fails with ETP
  off), the `sandbox` attribute (it fails with none), the Storage Access API (granted, still refused),
  and finally "Firefox does not allow service workers in a cross-origin frame", which was written into
  this document as fact.

  It was **our own response headers**. `Service-Worker-Allowed: /` was set by nginx *and* by the app,
  and fetch combines duplicate headers into `"/, /"` — not a valid scope prefix. Chromium never
  notices, because the script is served from the origin root and `/` is already its maximum scope;
  Firefox parses it strictly and refuses, with an error that names neither headers nor scope. What
  broke the deadlock was one outside fact: **Ditto runs the same design in Firefox** through
  iframe.diy, from a *cross-site* frame — strictly more restricted than ours, which is same-site. A
  working reference implementation is worth more than any amount of reasoning about what a browser
  "does not allow". The header is now set in exactly one place (the app); do not add it back to the
  vhost "to be safe", which is the instinct that caused this.

  The second half was a **race, not a refusal**: the app frame navigates to `/`, and that request only
  reaches the server while the worker is not yet *controlling* — where this origin has nothing to
  serve. The loader now reloads itself once rather than framing an uncontrolled app (a timeout that
  proceeds anyway is a delay, not a wait), and the vhost's `location /` answers a miss with a page
  that reloads once instead of a 404. Both guards, deliberately: the loader's stops the race being
  entered, the vhost's stops it being fatal for a client that predates the fix.
- **The frame carries no `sandbox` attribute.** It was removed on the strength of the wrong diagnosis
  above — "Firefox refuses a worker in a sandboxed frame" was measured against the broken header, so
  it proves nothing. The security here never rested on the attribute: it rests on the app being on a
  different ORIGIN and having no network, both of which still hold. What is given up is that a frame
  may navigate the top-level page after a user activation, which is a phishing surface. **Worth
  re-testing now** — `allow-scripts allow-same-origin` should register a worker fine — but not on the
  same day the header fix ships, or a failure will be impossible to attribute.
- **The blob fallback cannot run an ES-module app.** When there is no worker at all (a Firefox private
  window disables them everywhere, first-party included) the loader hands the app blob: URLs instead.
  It works for a single HTML file with a couple of scripts, and it cannot work for a module:
  `import "./engine.js"` from a `blob:` URL has no path to resolve against and throws *Error resolving
  module specifier*, which is fatal to both Quake III and Half-Life. Import maps do not help —
  relative specifiers resolve before any map is consulted. It is a last resort, not a second design.
- **`sendToChat` and `importFiles` are not implemented** — same reason, same detection.
- **`selfAddr` is your npub and proves nothing.** Nothing inside a mini app is signed, so any player
  can claim to be anyone within the app. The NIP says so too; apps must not use it for trust. Signed
  out it is the realtime channel's ephemeral key instead of an empty string — the spec asks for an
  identifier unique in the chat, and Half-Life hashes `selfAddr` into the fake IP it routes
  multiplayer packets by, so two blank ones would collide on a single address and see nobody.
- **TWO WINDOWS ON ONE ACCOUNT ARE ONE PLAYER, and a LAN-emulating game will show you nothing.**
  This is the answer to "both browsers are in the same game, both are publishing, neither sees the
  other", and it is not a bug in the transport — measured with Half-Life's own shipped code:

  ```
  hashToUint24(npub) → idToFakeIp() :  npub1fdtthaq…  →  10.228.70.225
                                       npub1fdtthaq…  →  10.228.70.225   ← the same player twice
                                       a second npub  →  10.125.226.122
  ```

  Half-Life emulates a LAN by hashing `selfAddr` into an IP, and its receive path drops any broadcast
  whose source is its own address (`if (isBroadcast(dest) && ipEquals(src, this.myIp)) return`). So
  with one account signed in twice, each side hears the other perfectly and discards every packet as
  its own echo, while host election collapses onto a single id. **To test multiplayer you need two
  identities, not two windows** — and signing out of one is enough now that the realtime channel needs
  no signer: a signed-out player gets the ephemeral address above and plays fine.

  `selfAddr` stays the npub deliberately rather than being made unique per device. It is what the
  spec asks for (Delta Chat uses the email address, which is likewise one identity across a person's
  devices, and breaks the same way), and turn-based apps attribute moves with it — scoping it per
  device would make your phone a different player from your laptop in the middle of a chess game.
- **IndexedDB is not namespaced** the way localStorage is, so two apps on the shared origin can see
  each other's databases.

## Where the code is

| | |
|---|---|
| `static/js/client/webxdc.js` | the whole client half: sandbox host, fetch + verify, the `window.webxdc` bridge, the Nostr transport, the cartridge, posting |
| `static/js/client/zip.js` | the `.xdc` reader — no library; the browser's own `DecompressionStream` |
| `static/webxdc-sandbox/` | the two files the sandbox origin serves |
| `app/main.py` | `_is_sandbox_host` and the two routes it gates |
| `app/services/webxdc_service.py` | the one startup warning when `xdc.<host>` isn't deployed |
| `scripts/install/webxdc.sh` | `./install.sh --webxdc` — DNS check, certbot, the vhost |
| `nginx/webxdc-sandbox.conf.example` | the vhost, as deployed (the installer renders it) |
| `docker/proxy/posterchanai.conf` | the same vhost for the compose `proxy` service |
| `tests/test_webxdc.py` | the zip reader against real Python-built archives, and the attachment parser |
| `tests/test_webxdc_deploy.py` | the installer's refusals and rendering, run for real under bash |
