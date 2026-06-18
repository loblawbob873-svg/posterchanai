# Nostr Web-of-Trust Relay

PosterChanAI ships a **self-contained Nostr relay** (NIP-01/09/11) built natively into the
app — no external relay software (strfry/khatru), no extra daemon, no new dependencies. It
runs in its own thread, stores into its own database, and is configured entirely from
**Admin → Relay**.

Its purpose is a **curated, spam-free feed**: it only ever accepts or syncs notes from a
**Web of Trust (WoT)**, automatically completes broken reply threads, and fetches profile
metadata so clients always render names and avatars. It can also act as your **outbox** —
point your bots/clients at it and it re-broadcasts what you publish to the wider network.

---

## How it works

```
          ┌─────────────────────── PosterChanAI ───────────────────────┐
 clients  │   relay thread (own asyncio loop, own port :3052)          │
   ⇄ wss ─┼─▶  NIP-01 WS  ──▶ WoT gate ──▶ tmpfs SQLite ──▶ snapshot ──┼─▶ disk
          │        ▲                          ▲     │                   │
          │        │  outbox (your writes)    │     └─▶ live fan-out ───┼─▶ subscribers
          │        └──────────────┐           │                        │
          │   windowed sync ◀─────┼───────────┘                        │
          └────────────────┬──────┴─────────── upstream public relays ─┘
```

- **Web of Trust gate.** The trust set = your configured **seed npubs** + everyone they
  follow (their kind-3 contact lists, **depth 1**) + your own bots/linked users (always
  trusted). A single membership check gates **both** inbound writes and upstream sync, so
  a non-WoT note can never be stored. The graph is rebuilt **daily**.

- **Windowed sync (never miss a note).** A poller pulls recent notes authored by WoT
  members from the upstream relays in **overlapping time windows** (default: re-scan the
  last 10 min every 2 min, +2 min overlap) with a persisted cursor. If the relay is down
  for a while, the next run looks back to the cursor and back-fills the gap. De-duplication
  makes the overlap idempotent.

- **Profile auto-download.** Any WoT member without a stored kind-0 profile is fetched
  automatically, so clients get names/avatars for everyone in the feed.

- **Thread completion.** When a synced note is a reply, its missing parent/root events are
  fetched by id and walked up to the thread root (bounded). This is the one deliberate,
  narrow relaxation of WoT-only: a parent may be outside the WoT, but it's stored only
  because a trusted member replied to it.

- **Outbox.** Notes **written to** the relay over its WebSocket (e.g. your bots/users who
  set this relay as their `nostr_relays`) are re-broadcast to the upstream public relays.
  Notes **pulled in** from upstream are not re-broadcast (no loops).

- **Language blocking.** Optionally reject text notes written in chosen scripts
  (Cyrillic/CJK/Arabic/…). Detection is by Unicode script (dependency-free), targeting
  non-Latin spam. Toggle per language in Admin → Relay.

---

## Memory-optimized storage (tmpfs + snapshots)

The **hot database is a SQLite file on tmpfs** (`/tmp`, RAM-backed) for fast write churn
with zero SSD wear. It is **snapshotted to a persistent disk file every 10 minutes** using
SQLite's online backup API (atomic temp-file swap), and **restored on startup** if tmpfs
was wiped by a reboot. The sync cursor lives inside the DB, so a restart resumes cleanly;
a hard crash loses at most one snapshot interval — and even that is re-pulled by the
windowed sync.

Memory is **hard-bounded** by an event-count cap **and** a byte budget, so the relay never
competes with the GPU models for RAM. A separate **auto-cleaner** deletes notes/reactions
older than *N* days (default 30) but **keeps profiles and contact lists forever** — so
identities and follow graphs survive indefinitely while the note volume stays bounded. On a
low-RAM node, point `Scratch dir` at a disk path instead of `/tmp`.

| Restart type | What happens to events |
|---|---|
| Service restart / deploy | tmpfs survives a process restart → **nothing lost** (a final snapshot is also taken on shutdown) |
| Machine reboot | tmpfs wiped → **restored from the last disk snapshot**, then the windowed sync re-pulls the small gap |

---

## Quick start

1. **Admin → Relay → enable** "Run the relay on this server", set your **seed npubs**
   (a starter set ships by default), **save**, and **restart the service**.
2. Verify it's up: `journalctl -u posterchanai.service | grep nostr-relay` should show
   `listening on ws://127.0.0.1:3052/relay` and periodic `sync tick` / `WoT rebuilt` lines.
3. Front it with TLS (see nginx below) and connect a client to `wss://relay.yourdomain/`.
4. Visiting `https://relay.yourdomain/` in a browser shows a welcome page with the URL and
   connection instructions; `Accept: application/nostr+json` returns the NIP-11 document.

### Docker (turnkey)

```bash
POSTERCHANAI_NOSTR_RELAY=1 docker compose --profile cpu up
```

Auto-enables the relay, binds `0.0.0.0:3052` (published by compose), and snapshots to the
data volume. Still front it with TLS for public use.

---

## Reverse proxy (TLS)

Nostr clients expect a relay at the root of a host over `wss://`. Map a subdomain to the
internal relay port:

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

The same `location` serves the NIP-11 JSON (on `Accept: application/nostr+json`) and the
browser welcome page, because the relay decides based on the request headers.

---

## Settings reference (Admin → Relay)

| Setting | Default | Notes |
|---|---|---|
| Run the relay | off | Master switch (restart after toggling) |
| Listen port | 3052 | Internal port; `/relay` path |
| Bind address | 127.0.0.1 | Use `0.0.0.0` only if exposing directly |
| Seed npubs | starter set | Trust roots; +their follows (depth 1) |
| WoT refresh | 86400s | Daily follow-graph rebuild |
| Blocked languages | none | Reject kind-1 notes in chosen scripts |
| Upstream relays | bots' defaults | Sync-from / broadcast-to |
| Sync window / interval / overlap | 600 / 120 / 120 s | Windowed, gap-free sync |
| Ingest kinds | 1,6,7 | 0 & 3 fetched automatically |
| Backfill ancestors | on | Complete reply threads |
| Max connections | 5000 | Concurrent client cap |
| Scratch dir | /tmp | Hot DB (tmpfs/RAM) |
| Snapshot path | data/nostr_relay.db | Persistent disk snapshot |
| Snapshot interval | 600s | hot → cold |
| Auto-clean notes older than | 30 days (0=off) | Deletes old notes only — **profiles & contacts kept forever** |
| Max events / Max DB MB | 500k / 1024 | Hard memory bounds (trim oldest notes first) |
| NIP-11 name/description/pubkey/contact | — | Relay info document |

---

## Supported NIPs

- **NIP-01** — events, REQ/EVENT/CLOSE subscriptions, filters (ids, authors, kinds, since,
  until, `#<tag>`), EOSE.
- **NIP-09** — event deletion (a kind-5 removes the author's own referenced events).
- **NIP-11** — relay information document.
- **NIP-45** — `COUNT` (basic).

Writes are restricted to the Web of Trust; reads are open.

## Notes & limits

- Single-instance design: the subscription registry and sync cursor are per-process
  (consistent with the app's other pollers). Run the relay on one node.
- The hot DB and snapshot are per-node app data and are **not** in git.
- Latin-script languages can't be distinguished by script alone and are not offered as
  block targets.
