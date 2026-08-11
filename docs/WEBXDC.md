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
| Realtime | Kind **20932** (ephemeral) — *not implemented here yet; apps feature-detect it* |

**The identifier, not the event, is what makes two people the same game.** Posting the same file
again mints a new one and starts a fresh game. It is generated per post.

**Serials are local.** The spec has them ordered and increasing, with gaps allowed, and says nothing
about them being the same for everyone — so this client assigns them by `(created_at, id)` over what
it has seen. That is what lets an append-only log ride a network with no global ordering: two devices
can disagree about the numbers and still agree about the *set*, which is all
`setUpdateListener(cb, lastSerial)` needs.

The app's own bytes are fetched **directly from wherever they were posted**, exactly like a remote
image in the timeline — the node stores only the apps posted through it. The `x` hash is verified
before anything runs: without that, whoever hosts the file could swap the app after it was posted,
for one reader or for everybody, and nothing about the post would change.

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

### Setup, once

1. **DNS** — `xdc.poster.place` pointing where `poster.place` points (a CNAME is fine, proxied is
   fine).
2. **TLS** — `sudo certbot --nginx --expand -d poster.place -d xdc.poster.place`.

No wildcard and no DNS API token. The vhost is `nginx/webxdc-sandbox.conf.example`, deployed on
router.lan as `/etc/nginx/sites-enabled/webxdc.conf`.

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

### The trade this leaves

Every app shares one origin, so **an app can read another app's leftovers in `localStorage`**. Keys
are namespaced per app in the injected bridge, which is a collision guard, not a security boundary.
What is *not* given up, and what actually matters: nothing there can reach the client's storage, where
the key and the session are.

An instance that does have a wildcard certificate can set `pc_webxdc_wildcard` to `1` in the client's
localStorage and get an origin per app, which closes that gap too.

## Known limits

- **No realtime channel yet** (kind 20932). `joinRealtimeChannel` is deliberately undefined, which is
  how the spec tells apps to feature-detect it.
- **`sendToChat` and `importFiles` are not implemented** — same reason, same detection.
- **`selfAddr` is your npub and proves nothing.** Nothing inside a mini app is signed, so any player
  can claim to be anyone within the app. The NIP says so too; apps must not use it for trust.
- **IndexedDB is not namespaced** the way localStorage is, so two apps on the shared origin can see
  each other's databases.

## Where the code is

| | |
|---|---|
| `static/js/client/webxdc.js` | the whole client half: sandbox host, fetch + verify, the `window.webxdc` bridge, the Nostr transport, the cartridge, posting |
| `static/js/client/zip.js` | the `.xdc` reader — no library; the browser's own `DecompressionStream` |
| `static/webxdc-sandbox/` | the two files the sandbox origin serves |
| `app/main.py` | `_is_sandbox_host` and the two routes it gates |
| `tests/test_webxdc.py` | the zip reader against real Python-built archives, and the attachment parser |
