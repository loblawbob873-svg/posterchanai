# Nostr-as-datastore (COMPLETE — on `master`)

The **built-in Web-of-Trust relay's event store is the system of record.** There is no SQLite and
no separate "app DB" anymore: the app's relational tables AND the relay's signed Nostr events live
in **one PostgreSQL database** (`posterchan_relay`). The Nostr web client (`/client`) is the face of
the app; anyone signs up / logs in with a Nostr key; admins grant AI access per-user.

This migration is **done** — the old phased `*_backend` flags (`settings_/users_/bots_/chat_/
records_backend`) are **gone**; every store is relay-authoritative unconditionally.

## One database, two kinds of state
Everything is in PostgreSQL (`DATABASE_URL` for the app's SQLAlchemy tables, `NOSTR_RELAY_PG_DSN`
for the relay's raw-SQL `events`/`event_tags`/`wot` tables — same DB, no name collisions):

- **Relay event store** (`app/services/nostr_relay/store.py`, psycopg2) — **the system of record**
  for user data. Signed Nostr events.
- **SQL tables** (SQLAlchemy) — fast **read-through caches** rebuilt from the relay on startup
  (`*_store.hydrate`), kept in sync by **write-through** on every mutation (`*_store.sync_*`). They
  exist so the many `db.query(...)` callers + FK relationships keep working unchanged; a fresh node
  reconstructs them from the relay.

The app reaches the event store as a **Nostr client to its own local relay** (publish/REQ over
`ws://127.0.0.1:<port>/relay`), plus a direct same-DB read in `*_store.hydrate` for startup.

## The store modules (`app/services/*_store.py`)
Each domain has `hydrate(db)` (relay → SQL cache at startup, deferred until the relay WS is up) and a
write-through (`sync_*` / `mirror_*` / `*_blocking`). `enabled()` hard-returns `True` everywhere.

| Domain | Module | d-tag | Signer / encrypted-to |
|--------|--------|-------|-----------------------|
| Global settings | `settings_store` | `pcai:setting:<key>` | operator |
| User account record (admin/can_*/quota) | `users_store` | `pcai:user:<npub>` | operator |
| Per-user config (mail/social prefs) | `users_store` | `pcai:usercfg:<npub>` | operator |
| **Budget** (bills/plans) | `budget.js` (client) | `pcai:budget` | **the user's OWN key** — the server cannot read it |
| Bot config | `bots_store` | `pcai:bot:<name>` | operator |
| Conversation index (title/timestamps) | `chat_store` | `pcai:conv:<id>` | user storage key |
| Chat messages | `chat_store` | `pcai:msg:<conv>:<seq>` | user storage key |
| Reminders / saved searches / API keys | `record_store` | `pcai:reminder:` / `pcai:search:` / `pcai:apikey:<id>` | user storage key |

All are **kind-30078** (NIP-78 app-data), **NIP-44 encrypted**, and **never fanned to upstream
relays** (the outbox only re-broadcasts real kind-1/6 client posts), so app data never hits a
timeline. The relay's prune is preserve-aware and never touches `origin='direct'` kind-30078 docs.

### Message mirroring is automatic + complete
A SQLAlchemy `after_commit` hook (`chat_store.install_message_mirror`) mirrors **every committed
Message row** to the relay — on the async chat WS via the running loop, and for off-path saves
(APScheduler/Telegram threadpool, sync routes) on a short-lived daemon thread. Non-nostr (no-npub)
users are skipped.

### Write-through coverage
Account mutations write through on **every** path: nostr-login / first-login-admin, settings save,
client caps/ai-access grants, **admin user create/delete/storage-quota/capabilities**, client
**self-delete**, avatar upload/delete. Deletes also remove the `pcai:user:`/`pcai:usercfg:` docs so a
rebuild can't resurrect a deleted account. (Admin-created password users and email-verify are no-npub
legacy → intentionally not synced; the relay store is npub-keyed.)

## Key custody (the one irreducible local secret)
Server-side AI/bots must read chats to generate replies, so true e2e is impossible — encryption is
**at rest in the relay**, not e2e. Each user gets a **server-held storage keypair**; the operator key
signs operator docs. These keys CANNOT live in the relay (they encrypt it — circular), so they live
in a gitignored keyfile `data/keys.json` (`{operator_nsec, storage:{npub:hex}}`, 0600, keyed by
npub). `app/services/keystore.py`; read by `nostr_store.user_storage_seckey`,
`settings_store._operator_seckey`, and the relay's `_collect_operator_pubkeys`. This is the only
on-disk state that lets the database be rebuilt from scratch.

## Mandatory, always-on
- **The relay always runs** — it's the datastore. `cfg['enabled']=True` is hard-coded; there is **no
  on/off toggle** in the admin UI.
- **Blossom (media) runs by default** — `blossom_enabled` defaults `true`; no enable toggle in the
  UI. A node can still be pinned off via the relay-stored setting (e.g. a keyless storage backend).
- First npub to sign in **auto-claims admin** (`POSTERCHANAI_AUTO_ADMIN`, locks once any npub-admin
  exists) and gets `can_ai`/`can_image`/`can_blossom`.

## Auth + AI access
Nostr login/signup (NIP-07 / nsec / Amber NIP-46). A non-AI user hits the **AI** view → "Request AI
access" → writes a request event + DMs admins; an admin approves from the profile ☰ menu (sets
`can_ai`). Blossom upload is gated to operator keys, `blossom_whitelist`, or a linked user who is
admin / has `can_blossom`.

## Deployment
- **PostgreSQL is required.** Bare metal: `run-intel.sh`/`run-nvidia.sh` export `DATABASE_URL` +
  `NOSTR_RELAY_PG_DSN`; `scripts/install/postgres.sh` provisions the role+db (localhost trust).
- **Docker: use `docker compose`, NOT `docker run`.** The compose `postgres:18` service + the
  `DATABASE_URL`/`NOSTR_RELAY_PG_DSN` env only exist in compose; a bare `docker run` has no database
  and exits with "connection refused". `docker compose --profile <cpu|cuda|rocm|intel|nostr> up -d
  --build` brings up Postgres + the app wired together. (AMD/ROCm: the entrypoint auto-sets
  `HSA_OVERRIDE_GFX_VERSION`, required or HIP throws "invalid device function".)

## UI
The standalone web UI is retired — AI chat is a **view inside the Nostr client** (`static/js/client/`)
alongside Home/Settings/Files. The Blossom **Files** view replaces the old per-user Files/Photos.
See also `docs/RELAY.md` and `docs/BLOSSOM.md`.
