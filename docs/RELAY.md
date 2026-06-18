# Nostr Web-of-Trust Relay

PosterChanAI ships a **self-contained Nostr relay** (NIP-01/09/11) built natively into the
app — no external relay software (strfry/khatru), no extra daemon, no new dependencies. It
runs in **its own thread + asyncio loop** (isolated from the web request loop), stores into
its own SQLite database, and is configured entirely from **Admin → Relay**.

Its purpose is a **curated, spam-free feed**: it only ever accepts or syncs notes from a
**Web of Trust (WoT)**, automatically completes broken reply threads, and fetches profile
metadata so clients always render names and avatars. It can also act as your **outbox** —
point your bots/clients at it and it re-broadcasts what you publish to the wider network.

---

## How it works

```
          ┌──────────────────────── PosterChanAI ────────────────────────┐
 clients  │   relay thread (own asyncio loop, own port :3052)            │
   ⇄ wss ─┼─▶  NIP-01 WS ──▶ WoT gate ──▶ SQLite (tmpfs OR disk/WAL) ────┼─▶ durable
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

### Language blocking
Optionally reject text notes (kind 1) written in chosen scripts (Cyrillic/CJK/Arabic/…),
toggled per-language with clickable chips in Admin → Relay. Detection is by Unicode script
(dependency-free), targeting non-Latin spam. Applies to both writes and sync. (Latin-script
languages can't be told apart by script and aren't offered.)

### Rate limiting (don't get blocked)
Upstream requests are **paced** (a configurable delay between author batches during sync /
profile fetch), and the **outbox is a bounded paced queue** (a minimum interval between
broadcasts; drops on overflow) — so a big sync or a post-blasting bot won't trip relay rate
limits.

### Tor proxy (optional bypass)
By default the relay routes its upstream traffic through the app's built-in Tor proxy (like
the bots). You can **bypass it for the relay** (`Bypass Tor proxy`) so sync/outbox/WoT connect
directly — faster, and it avoids the proxy-startup error spam. This only affects the relay,
not the bots.

---

## Storage modes (small vs. many-GB)

Choose in Admin → Relay → **Storage mode**:

### `tmpfs` (default — small, fast, bounded)
The hot DB is a SQLite file on **tmpfs** (`/tmp`, RAM-backed) for fast write churn with zero
SSD wear. It's **snapshotted to a persistent disk file every 10 min** via SQLite's online
backup API (stepped copy on its own thread so it never blocks writers, atomic temp-file swap,
skipped when nothing changed), and **restored on startup** if tmpfs was wiped by a reboot.

| Restart type | What happens to events |
|---|---|
| Service restart / deploy | tmpfs survives a process restart → **nothing lost** (a final snapshot is also taken on shutdown) |
| Machine reboot | tmpfs wiped → **restored from the last disk snapshot**, then windowed sync re-pulls the small gap |

### `disk` (for many-GB / large WoT)
The DB lives **directly on disk in WAL mode** — **no snapshots at all**, because a full-copy
snapshot becomes the bottleneck once the DB is multiple GB. The DB is durable by itself, the
OS page cache keeps hot pages in RAM, and writes are sequential WAL appends. A
`wal_checkpoint(TRUNCATE)` runs on clean shutdown.

**How DB writes work in WAL mode** (`nostr_relay.db` + `-wal` + `-shm`):
- New writes append to the **`-wal`** file, not the main DB; the main file is only updated at
  a *checkpoint*.
- **One writer at a time globally**, serialized by the WAL lock (waiters block up to
  `busy_timeout`, not error). **Readers never block** and read a consistent snapshot — so
  client subscriptions keep working during heavy ingest/backfill.
- A **large WAL** (`WAL size`, default 50 000 pages ≈ 200 MB; set higher, e.g. 500 000 ≈ 2 GB)
  means far fewer checkpoints and much faster sustained writes for a big DB.

In both modes, memory/size is **hard-bounded** by an event-count cap, a byte budget, and an
**auto-cleaner** that deletes notes/reactions older than *N* days (default 30) but **keeps
profiles and contact lists forever** — identities and follow graphs survive indefinitely while
note volume stays bounded.

---

## Quick start

1. **Admin → Relay → enable** "Run the relay on this server", set your **seed npubs** (a
   starter set ships by default), pick the **WoT depth** and **storage mode**, **save**, and
   **restart the service**.
2. Verify it's up: `journalctl -u posterchanai.service | grep nostr-relay` should show
   `listening on ws://…:3052/relay`, `WoT rebuilt: N members`, and periodic `sync tick` lines.
3. Front it with TLS (see below) and connect a client to `wss://relay.yourdomain/`.
4. Visiting `https://relay.yourdomain/` in a browser shows a welcome page with the connect
   URL; `Accept: application/nostr+json` returns the NIP-11 document.

### Docker (turnkey)

```bash
POSTERCHANAI_NOSTR_RELAY=1 docker compose --profile cpu up
```

Auto-enables the relay, binds `0.0.0.0:3052` (published by compose), DB on the data volume.
Still front it with TLS for public use.

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
        proxy_pass http://127.0.0.1:3052/relay;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;   # long-lived subscriptions
    }
}
```

A path also works (e.g. `location = /relay` on an existing host → `wss://yourdomain/relay`).
The same location serves the NIP-11 JSON (`Accept: application/nostr+json`) and the browser
welcome page — the relay decides from the request headers, and the welcome page advertises the
exact path it was reached on.

---

## Settings reference (Admin → Relay)

| Setting | Default | Notes |
|---|---|---|
| Run the relay | off | Master switch (restart after toggling) |
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
| **Storage mode** | tmpfs | `tmpfs` (snapshots) or `disk` (WAL, many-GB) |
| WAL size | 50000 pages | Larger = fewer checkpoints, faster writes |
| Scratch dir | /tmp | tmpfs hot DB location |
| Snapshot path | data/nostr_relay.db | Disk DB (snapshot in tmpfs mode; live DB in disk mode) |
| Snapshot interval | 600s | tmpfs mode only |
| Auto-clean notes older than | 30 days (0=off) | Old notes only — **profiles & contacts kept forever** |
| Max events / Max DB MB | 500k / 1024 | Hard bounds (trim oldest notes first) |
| NIP-11 name/description/pubkey/contact | — | Relay info document |

---

## Supported NIPs

- **NIP-01** — events, REQ/EVENT/CLOSE subscriptions, filters (ids, authors, kinds, since,
  until, `#<tag>`), EOSE, live fan-out.
- **NIP-09** — event deletion (a kind-5 removes the author's own referenced events).
- **NIP-11** — relay information document.
- **NIP-45** — `COUNT` (basic).

**Writes are restricted to the Web of Trust; reads are open.** A blocked write gets
`["OK", id, false, "blocked: not in web of trust"]` (or `"blocked: language '…'"`).

## Notes & limits

- Self-contained: pure Python on FastAPI's `websockets`, no external relay binary, no new
  dependencies, runs/stops with the app under the port-3051 guard.
- Single-instance design: the subscription registry and sync cursor are per-process
  (consistent with the app's other pollers). Run the relay on one node.
- The DB (hot + snapshot/disk) is per-node app data and is **not** in git.
- Port scanners hitting the public port are silently ignored (handshake-failure logging is
  quieted).
