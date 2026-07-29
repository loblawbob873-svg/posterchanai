# Tor / .onion

Two separate things share the word "Tor" in this app. Don't confuse them:

| | What it is | Where |
|---|---|---|
| **Managed Tor service** | An outbound SOCKS5 proxy the *server* uses to fetch things (news, RSS, feeds) through Tor. | Admin → Network, `tor_*` settings |
| **Onion site** | An inbound v3 hidden service that publishes *this deployment* at a `.onion` address. | Admin → Network → Onion Site |

This document is about the second one, and about reaching it from the Android app.

## Turning the onion on

Admin → Network → **Onion Site** → *Generate .onion site*. One click; it applies live via SIGHUP,
no restart. The keys live in `<tor_data_dir>/onion_service`, so the address survives restarts and
survives being disabled and re-enabled.

Requires the Managed Tor Service to be running (it's the same daemon) and the system `tor` binary. The
Docker image installs it; on a bare-metal node install it from your distro (`apt-get install tor`).

The hidden service publishes **two** ports:

```
HiddenServicePort 80   127.0.0.1:3051    # the app
HiddenServicePort 3052 127.0.0.1:3052    # the Nostr relay
```

The relay needs its own line because **Tor forwards TCP, not paths**. In production nginx routes
`/relay` to the relay process, but there is no nginx inside a hidden service — so without the second
line the onion would serve the client shell and then have no relay to talk to, and the client (which is
relay-first) would come up empty. Clients on the onion get `ws://<onion>:3052/relay`.

## What the server hands an onion visitor

An onion site that answers `/client/config` with the admin's clearnet relay and media host is a facade:
every socket and every image leaves Tor through an exit node the moment the page loads. So three URL
builders check whether the request arrived on our onion and answer in kind:

- `client._relay_url` → `ws://<onion>:3052/relay`
- `client._blossom_url` → `http://<onion>/blossom`
- `blossom._base_url` → `http://<onion>/blossom`

**Blossom is the one that matters most.** `_base_url` is what an upload's response URL is built from,
and that URL is what the client **embeds in the note the user publishes**. Returning
`https://media.poster.place/<sha>` there would stamp the instance's real domain permanently into an
onion user's posts, and every viewer would fetch the image off the clearnet. On the onion, uploads go
to `http://<onion>/blossom` and come back as `http://<onion>/blossom/<sha>` — Tor end to end.

`tor_service.request_onion_host(request)` is the shared check. `Host` is client-controlled, so it must
**match the address Tor actually generated for us** — an arbitrary `Host: evil.onion` is refused rather
than echoed back into a URL we hand out. With the hidden service off, no `.onion` Host is ever trusted.

Plain HTTP is correct here and not a downgrade: Tor provides the encryption and authenticates the
address (the onion name *is* the public key). That's also why the `upgrade-insecure-requests` CSP is
only emitted when the request itself arrived over HTTPS — over the onion it would break the page.

## Using the Android app with an onion instance

Settings → **Instance** → paste the `.onion` address → Connect. Or the same field on the login screen.

A bare host typed there gets `http://`, not `https://`, when it ends in `.onion` — our hidden service
publishes port 80 only, so defaulting to https would produce an address that can never connect. An
explicit scheme is always respected.

Settings also has a **🧅 Tor** panel: whether Orbot is installed, whether the current instance is an
onion, and buttons to start / open Orbot. It never claims traffic *is* on Tor — an app can't honestly
know that without making an external request, which defeats the point. The connection is the proof.

### Routing the app through Orbot

Install [Orbot](https://orbot.app). Either:

- turn on Orbot's full-device VPN mode, or
- turn on VPN mode and add **PosterChan** to Orbot's app list.

That's the whole integration. Orbot's VPN mode is transparent — including `.onion` resolution, which
Orbot answers itself — so there is nothing to configure in the app and nothing that can silently
half-apply. We deliberately do *not* proxy anything ourselves: the Android WebView has no per-app SOCKS
setting, so a half-built in-app proxy would be a leak waiting to happen.

`OrbotPlugin` only detects Orbot and asks it to start. Detection needs the `<package android:name=
"org.torproject.android" />` entry in `<queries>` — on API 30+ package visibility is opt-in, and without
it the panel reports "not installed" on a phone that has Orbot sitting right there.

### Why the app can't use cookies against an onion

This is the non-obvious part, and it's why several things in the client look the way they do.

The APK's page origin is `https://localhost` (Capacitor). Every API call is therefore **cross-origin**,
which means the session cookie must be `SameSite=None`, which browsers only honour together with
`Secure`, which they in turn refuse to set over a plain-HTTP connection. An `.onion` is plain HTTP by
design. So there is no cookie that can work — not a bug to fix, a dead end to route around:

- **API calls** carry `Authorization: Bearer <token>` instead. The token comes from
  `/api/auth/nostr-login` (`_setAiToken` → `window.__PC_TOKEN__`), and the bundled-mode fetch shim
  attaches it to every request it retargets at the instance — but never over an `Authorization` the
  caller already set, because Blossom and NIP-96 uploads carry their own `Nostr <base64>` header.
  It is held in **memory only**: persisting it would leave a working 30-day credential for the previous
  identity on a shared install, and buys nothing, since `ensureAiSession()` mints one on demand. That's
  why authed callers `await ensureAiSession()` first — an established pattern, not new.
- **`/client/file`** (decrypted AI-chat artifacts) can't send a header at all — those URLs sit in
  `<img src>`. `/client/file-auth` therefore also returns the token in its body, and `_absUrl` appends
  it as `?t=` when the instance is cleartext. Same token, same ownership proof, different envelope.
  The body only carries the token **on a cleartext request**: the cookie is HttpOnly on purpose
  ("script never needs to read it"), so exposing it to script on HTTPS too — where it is never used —
  would be a straight downgrade for the majority of traffic.

Anything that reaches the server by **top-level navigation** rather than `fetch` can carry neither the
bearer nor a cookie, so it stays broken over an onion in the APK. Today that's the **Admin panel**,
which is an `<iframe src>`. No cookie configuration fixes it — a cross-site iframe needs `SameSite=None`,
which needs `Secure`, which cleartext refuses. Use Tor Browser against `http://<onion>/admin` instead,
where the request is same-site and the normal cookie works.

The same reasoning applies to a plain-HTTP LAN instance (`http://nas.lan:3051`), which is why the client
gates on "is the instance cleartext" rather than "is it an onion".

### The CSP that had to go

`mobile/build-www.sh` downloads the live `/client` shell over HTTPS, so the server emits
`upgrade-insecure-requests` — correct for the web PWA, and **baked into every APK**. Inside the app that
meta is actively wrong: the page is `https://localhost` but the instance may legitimately be cleartext,
and the CSP silently rewrites every fetch / WebSocket / `<img>` to `https://<that host>`, which does not
exist. The build now strips it. The app is allowed to speak cleartext on purpose
(`usesCleartextTraffic` + `allowMixedContent`); this meta overrode that for no benefit.

## Reaching the onion from a desktop browser

Tor Browser → `http://<onion>/client`. Nothing special is needed: the request is same-site, so the
normal cookie path works (`Lax`, no `Secure`), and the client shell skips the HTTPS-only CSP.

## Known limits

- **Speed.** Everything is a hidden-service circuit. Large Blossom uploads and video are slow. This is
  Tor, not a regression.
- **The relay port is exposed as a port, not a path.** `ws://<onion>:3052/relay` — a client that
  hardcodes `/relay` on port 80 won't find it.
- **Verify on a real device** after changing the shim or the manifest: whether the WebView allows a
  `ws://` connection from an `https://localhost` page rests on `MIXED_CONTENT_ALWAYS_ALLOW`, which is
  set in `capacitor.config.json` but is worth re-confirming rather than assuming.
