# Nostr Web-of-Trust Relay

PosterChanAI ships a **self-contained Nostr relay** (NIP-01/02/09/11/17/22/23/40/44/45/50/59/65/77)
built natively into the
app — no external relay software (strfry/khatru), no extra daemon, no new dependencies. It
runs in **its own thread + asyncio loop** (isolated from the web request loop), stores into
its own **PostgreSQL** database, and is configured entirely from **Admin → Relay**.

> **The relay is also the app's datastore.** PosterChanAI is Nostr-native: settings, accounts,
> API keys, bot config, and AI chats are stored as **NIP-44-encrypted `kind-30078` events**
> in this relay (the Postgres tables are just a hydrated read-cache). There is no separate
> app DB and no SQLite. See [NOSTR_DATASTORE.md](NOSTR_DATASTORE.md).

Its purpose is a **curated, spam-free feed**: it only ever accepts or syncs notes from a
**Web of Trust (WoT)**, automatically completes broken reply threads, and fetches profile
metadata so clients always render names and avatars. It can also act as your **outbox** —
point your bots/clients at it and it re-broadcasts what you publish to the wider network.

---

## How it works

```
          ┌──────────────────────── PosterChanAI ────────────────────────┐
 clients  │   relay thread (own asyncio loop, own port :3052)            │
   ⇄ wss ─┼─▶  NIP-01 WS ──▶ WoT gate ──▶ PostgreSQL (GIN/tsvector) ─┼─▶ durable
          │        ▲                          ▲     │                     │
          │        │  outbox (your writes)    │     └─▶ live fan-out ─────┼─▶ subscribers
          │        └──────────────┐           │                          │
          │   windowed sync ◀─────┼───────────┘   (paced, optional direct)│
          └────────────────┬──────┴──────────────── upstream public relays┘
```

### Web of Trust gate
The trust set = your **seed npubs** + the people they follow (their kind-3 contact lists),
plus your own bots/linked users (always trusted). A single `is_member()` check gates
**both** inbound writes and upstream sync, so a non-WoT note can never be stored.

- **Depth 1** — seeds + their direct follows.
- **Depth 2 — friends-of-friends.** Also includes follows-of-follows, but **pruned**: a FoF
  is included only if at least *N* of your follows also follow them (`min_followers`, default
  2), and the whole set is **capped** (`max members`, default 50 000 — the most-followed FoF
  are kept when over). This keeps a depth-2 graph (often tens of thousands of pubkeys) bounded
  and syncable.
- The graph is rebuilt **daily**, and you can force a rebuild with the **"↻ Refresh Web of
  Trust now"** button in Admin → Relay (runs in the background; a depth-2 build takes a few
  minutes).

### Windowed sync (never miss a note)
A poller pulls recent notes authored by WoT members from the upstream relays in **overlapping
time windows** (default: re-scan the last 10 min every 2 min, +2 min overlap) with a persisted
cursor. If the relay is down for a while, the next run looks back to the cursor and back-fills
the gap. De-duplication makes the overlap idempotent. Each tick is **time-budgeted**; with a
very large WoT it sweeps the author set over several ticks, and the overlap guarantees no gaps.

### Profile auto-download
Any WoT member without a stored kind-0 profile is fetched automatically, so clients get
names/avatars for everyone in the feed.

### Thread completion
When a synced note is a reply, its missing parent/root events are fetched by id and walked up
to the thread root (bounded). This is the one deliberate, narrow relaxation of WoT-only: a
parent may be outside the WoT, but it's stored only because a trusted member replied to it.

### Outbox (broadcast your posts)
Notes **written to** the relay over its WebSocket (e.g. your bots/users who set this relay as
their `nostr_relays`) are re-broadcast to the upstream public relays. Notes **pulled in** from
upstream are not re-broadcast (no loops). End-state: point all your bots + users at
`wss://relay.yourdomain/` and the relay becomes your single outbox.

### Post-history backfill
Pull your **own full post history** from the upstream relays into the relay:
- **User Settings → Nostr → "⭳ Sync my posts to the relay"** (any user, their own posts).
- **Admin → Relay → "⭳ Sync my post history"** (the admin's own key).

It pages back through time and writes **directly to the store** — your old posts are **not**
re-broadcast by the outbox. Safe to re-run (it dedupes).

### Search (NIP-50)
Clients can full-text **search** the relay's notes with a `{"search": "..."}` filter, backed
by a **PostgreSQL `tsvector`** index (GIN) over note content. Multiple words are AND-ed.
Advertised as NIP-50 in the relay info doc.

### Lookup relay (NIP-65 / outbox model)
Beyond the timeline, the relay stores and serves the **lookup metadata** for every WoT
member — **kind-0 profiles**, **kind-3 contact lists**, and **kind-10002 relay lists** — so
clients can use it to resolve who-is-who and *where each member posts*. Advertised as NIP-02
+ NIP-65.

### Private messages — DM inbox (NIP-17)
The relay doubles as a **DM inbox for its own users**. Gift-wrapped DMs (kind 1059) have a
random throwaway author, so the WoT gate can't judge them — instead the relay accepts a DM
(kind 1059, or legacy kind 4) when it's **addressed (p-tag) to one of your linked users/bots**,
and serves it back on `{kinds:[1059], "#p":[you]}`. DMs are stored privately and **never
re-broadcast** by the outbox.

### Content & account filters
Three independent filters, all in Admin → Relay, all applied to **both writes and sync**:
- **Language blocking** — reject kind-1 notes in chosen scripts (Cyrillic/CJK/Arabic/…),
  toggled per-language with clickable chips. Detection is by Unicode script (dependency-free),
  targeting non-Latin spam. (Latin-script languages can't be told apart by script.)
- **Word/phrase blocking** — reject notes whose text contains any banned word/phrase
  (case-insensitive substring; one per line).
- **Account blocklist** — a hard denylist of pubkeys (npub/hex), rejected **even if they're in
  the Web of Trust**; their stored notes are purged on restart.

### Rate limiting (don't get blocked)
Upstream requests are **paced** (a configurable delay between author batches during sync /
profile fetch), and the **outbox is a bounded paced queue** (a minimum interval between
broadcasts; drops on overflow) — so a big sync or a post-blasting bot won't trip relay rate
limits.

### Tor proxy (proxy-first, direct fallback)
By default the relay routes its upstream connections through the app's built-in Tor proxy (like
the bots) and **falls back to a direct connection if the proxy connect fails** — so a flaky/down
Tor proxy degrades gracefully instead of dropping federation. `Bypass Tor proxy` forces direct
only (never attempt Tor). This only affects the relay, not the bots.

**Two Tor daemons (load-balanced).** When the second Tor daemon is enabled (Admin → Network →
*Second Tor Daemon*, on by default whenever Tor is on), PosterChanAI runs **two** managed `tor`
instances with different exit regions — `{us}` (SOCKS 9052) and `{ca}` (SOCKS 9062, geographically
close so minimal latency cost). The built-in HTTP proxy **round-robins across both circuits**, giving
two independent exit IPs that spread upstream load and dodge the per-IP rate limits (HTTP 429 storms)
busy relays apply to a single Tor exit. Each daemon has its own ports + data dir (`…/tor`, `…/tor2`);
they start only when Tor itself is enabled. The proxy is **Tor-only** — if every circuit is down it
fails the connection rather than going direct, so proxied torrent traffic can never leak the real IP
(the relay's own direct-fallback above is separate, and applies only to relay federation).

### Web of Trust on/off — full node vs processing node
`Enforce Web of Trust` (Admin → Relay, default **on**) is the master switch for whether this node
does relay/social work:
- **On** — publishing is gated to the trust set, and the relay runs the trust-graph build, metadata
  backfill, firehose sync, and **NIP-05** serving. The full primary node.
- **Off** — publishing is **open** (bridge/language/word filters still apply) and **all** cross-node
  background work stops (no trust-graph build, metadata backfill, sync, firehose; NIP-05 serving is
  forced off too). The relay becomes a pure local store. Use this on a secondary/processing node
  whose relay is internal, so it doesn't duplicate the primary's WoT + NIP-05 work.

### Send-only (broadcast, don't mirror)
`Send-only` (under **Upstream relays**) keeps **broadcasting** this node's own events to the upstream
relays via the outbox, but skips **all receive-direction** work (firehose, sync, metadata backfill) —
so a secondary node's local DB never fills with a mirror of upstream content. It's the *direction*
control for the upstream list (which is just *where* to send). Pair it with a primary relay in the
upstream list to push a node's events up without pulling everything back down.

### Operator key (auto-minted)
The relay signs its datastore docs (settings/users/etc.) with an **operator key**. A fresh node
**auto-mints** one on first use and stores it in the local keyfile (`data/keys.json`, gitignored) —
so "relay-backed by default" works out of the box. To let another node accept this node's
broadcasts, add this node's **operator npub** (Admin → Relay → *Relay identity → Copy npub*) to that
node's Web-of-Trust **seed npubs**.

---

## Storage (PostgreSQL)

The relay stores into **PostgreSQL** — the *same* database the app uses, since the relay **is**
the app's datastore. Events live in an `events` table with a **GIN `tsvector`** index for NIP-50
full-text search and proper row-level locking (no single-writer WAL lock — concurrent ingest and
client reads don't block each other). It scales to many GB (a depth-2 WoT is multi-GB), and a
fast **libsecp256k1** verify path (see below) keeps ingest CPU-cheap.

- **Connection:** passwordless localhost `trust` by default (`host=127.0.0.1 dbname=posterchan_relay
  user=posterchan`); override with the `NOSTR_RELAY_PG_DSN` / `DATABASE_URL` env for a password or
  a remote/Docker server. Provisioned by `install.sh` (or the `postgres` Docker service).
- **App tables vs relay events:** `Base.metadata` (the app's operational/cache tables) and the
  relay's raw `events`/`event_tags`/`wot` tables share one DB. The app's authoritative data is the
  encrypted `kind-30078` events; the SQL tables are a hydrated read-cache.

Size is **hard-bounded** by an event-count cap, a byte budget, and an **auto-cleaner** that
deletes notes/reactions older than *N* days (default 30) but **keeps profiles and contact lists
forever** — identities and follow graphs survive indefinitely while note volume stays bounded.
The same cleaner pass also runs the **NIP-40 expiration sweep** (purges any event past its
`expiration` timestamp, across all kinds) before the age-based prune.

### Fast signature verification
Mass-verifying synced events is the ingest bottleneck. The relay uses **libsecp256k1 via
`coincurve`** when available (~0.03 ms/sig vs ~67 ms pure-Python — ~2000× faster), gated by a
self-test so it's only used if it verifies a known good/bad signature correctly; otherwise it
falls back to the bundled pure-Python verify. `coincurve` is in `requirements.txt` (optional but
strongly recommended for a relay).

---

## Quick start

1. The relay **always runs** — it's the app's datastore, so there's no enable toggle. In
   **Admin → Relay**, set your **seed npubs** (a starter set ships by default), pick the
   **WoT depth**, **save**, and **restart the service**.
2. Verify it's up: `journalctl -u posterchanai.service | grep nostr-relay` should show
   `listening on ws://…:3052/relay`, `WoT rebuilt: N members`, and periodic `sync tick` lines.
3. Front it with TLS (see below) and connect a client to `wss://relay.yourdomain/`.
4. Visiting `https://relay.yourdomain/` in a browser shows a welcome page with the connect
   URL; `Accept: application/nostr+json` returns the NIP-11 document.

### Docker (turnkey)

```bash
POSTERCHANAI_NOSTR_RELAY=1 docker compose --profile cpu up
```

Binds the relay `0.0.0.0:3052` (published by compose) and stores events in the compose
`postgres` service. (The relay runs in every deployment regardless — `POSTERCHANAI_NOSTR_RELAY=1`
just also sets the public bind. **Use `docker compose`, not `docker run`** — the relay + app need
the `postgres` service.) Still front it with TLS for public use.

---

## Reverse proxy (TLS)

Nostr clients expect a relay at the root of a host over `wss://`. Map a subdomain (or a path)
to the internal relay port:

```nginx
server {
    listen 443 ssl;
    server_name relay.yourdomain;
    # ... ssl_certificate / ssl_certificate_key ...

    location / {
        proxy_pass http://127.0.0.1:3052;   # no URI — see below
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;   # the relay trusts this for the client address
        proxy_read_timeout 3600s;   # long-lived subscriptions
    }
}
```

A path also works (e.g. `location = /relay` on an existing host → `wss://yourdomain/relay`).
The same location serves the NIP-11 JSON (`Accept: application/nostr+json`) and the browser
welcome page — the relay decides from the request headers, and the welcome page advertises the
exact path it was reached on. That last part is why the `proxy_pass` above carries **no URI**: the
relay accepts the upgrade on any path, so passing `/` through unchanged makes it advertise
`wss://relay.yourdomain/`, while rewriting to `/relay` would advertise a path this hostname doesn't
serve. Full copy-and-edit configs (both layouts, plus NIP-05 and Blossom):
[`nginx/nostr-docker.conf.example`](../nginx/nostr-docker.conf.example) for a Nostr-only/Docker node,
[`nginx/posterchanai.conf.example`](../nginx/posterchanai.conf.example) for a full one — see
[NGINX.md](NGINX.md).

### NIP-05 identities (`/.well-known/nostr.json`)

The relay subprocess also answers **NIP-05** lookups, so `name@yourdomain` verifies without a
separate static file. Configure the names + advertised relays under **Admin → Relay → NIP-05
identity server**, then point your reverse proxy's well-known path at the relay port:

```nginx
# on the host that serves https://yourdomain
location = /.well-known/nostr.json {
    proxy_pass http://127.0.0.1:3052;   # the relay handles the ?name= query + CORS
    proxy_set_header Host $host;
}
```

The relay sets `Access-Control-Allow-Origin: *` itself, so cross-origin client fetches work.
Edits in the admin UI apply immediately (no restart).

---

## Settings reference (Admin → Relay)

| Setting | Default | Notes |
|---|---|---|
| _(no enable toggle)_ | — | The relay is the datastore — it **always runs**; there's no on/off switch |
| Bypass Tor proxy | off | Relay upstream connects directly (faster) |
| Listen port / Bind | 3052 / 127.0.0.1 | `/relay` path; `0.0.0.0` only if exposing directly |
| Seed npubs | starter set | Trust roots |
| **WoT depth** | 1 | 1 = follows; **2 = + friends-of-friends** |
| FoF threshold | 2 | Depth-2: min of your follows who must follow a FoF |
| Max WoT members | 50000 | Cap; most-followed FoF kept when over |
| WoT refresh | 86400s | Daily rebuild (+ manual button) |
| Blocked languages | none | Reject kind-1 notes in chosen scripts |
| Upstream relays | bots' defaults | Sync-from / broadcast-to |
| Sync window / interval / overlap | 600 / 120 / 120 s | Windowed, gap-free sync |
| Ingest kinds | 1,6,7 | 0 & 3 fetched automatically |
| Backfill ancestors / max | on / 20 | Complete reply threads |
| Delay between sync queries | 1.0 s | Pace upstream requests |
| Outbox min interval / queue | 1.0 s / 500 | Throttle + bound broadcasts |
| Max connections | 5000 | Concurrent client cap |
| Enforce Web of Trust | on | Off = open publishing + no WoT/firehose/sync/NIP-05 work (processing node) |
| Send-only | off | Broadcast to upstream but don't pull/store their events |
| PostgreSQL DSN | host=127.0.0.1 dbname=posterchan_relay | `NOSTR_RELAY_PG_DSN` / `DATABASE_URL` env; passwordless localhost trust by default |
| Auto-clean notes older than | 30 days (0=off) | Old notes only — **profiles & contacts kept forever** |
| Max events / Max DB MB | 500k / 1024 | Hard bounds (trim oldest notes first) |
| NIP-11 name/description/pubkey/contact | — | Relay info document |

---

## Supported NIPs

- **NIP-01** — events, REQ/EVENT/CLOSE subscriptions, filters (ids, authors, kinds, since,
  until, `#<tag>`), EOSE, live fan-out, and the full **event-class semantics**: **replaceable**
  (kind 0/3 and 10000–19999, newest-per-`(pubkey,kind)` kept), **addressable / parameterized
  replaceable** (30000–39999, newest-per-`(pubkey,kind,d-tag)`), and **ephemeral** (20000–29999,
  delivered to subscribers but **never persisted**).
- **NIP-02** — contact lists (kind-3) stored & served (lookup relay).
- **NIP-09** — event deletion (a kind-5 removes the author's own referenced events).
- **NIP-11** — relay information document (incl. relay `icon`).
- **NIP-17 / 44 / 59** — **private direct messages**: the relay acts as a **DM inbox** for its
  own users — gift-wrapped DMs (kind 1059) and legacy kind-4 are accepted when **addressed
  (p-tag) to a relay user**, even though the gift-wrap author is a random key (so the WoT gate
  can't apply). DMs are never re-broadcast by the outbox.
- **NIP-22 / 23** — comments (kind 1111) and **long-form articles** (kind 30023), synced + served.
- **NIP-40** — **event expiration.** An event carrying an `["expiration", <unix-ts>]` tag is
  honoured end-to-end: an already-expired write is rejected (`["OK", id, false, "invalid: event
  expired"]`), the expiry is stored alongside the event, **expired events are filtered out of
  every read** (REQ/COUNT/negentropy) the instant they pass their timestamp, and a periodic
  sweep purges them from disk. The expiration sweep is **unconditional** — it honours the
  author's intent across *all* kinds, even profiles/DMs/local-user events that the age-based
  cleaner would otherwise keep forever.
- **NIP-45** — `COUNT`.
- **NIP-50** — full-text **search** (`{"search": "..."}` filters), backed by a PostgreSQL
  `tsvector`/GIN index over note content.
- **NIP-65** — relay lists (kind-10002) stored & served — this relay works as an **outbox /
  lookup relay** so clients can resolve where each member posts.
- **NIP-77** — **negentropy** set reconciliation (`NEG-OPEN`/`NEG-MSG`) for efficient sync;
  falls back to `NEG-ERR` → normal REQ if a client's session can't be reconciled.

**Writes are restricted to the Web of Trust** (DMs excepted — see NIP-17); **reads are open.**
A blocked write gets `["OK", id, false, "blocked: not in web of trust"]` (or `"… language '…'"`,
`"… filtered text"`, `"… not a DM to a relay user"`).

## Notes & limits

- Self-contained: pure Python on FastAPI's `websockets`, no external relay binary, no new
  dependencies, runs/stops with the app under the port-3051 guard.
- Single-instance design: the subscription registry and sync cursor are per-process
  (consistent with the app's other pollers). Run the relay on one node.
- The DB (hot + snapshot/disk) is per-node app data and is **not** in git.
- Port scanners hitting the public port are silently ignored (handshake-failure logging is
  quieted).
