# Nostr-as-datastore migration (branch: `nostr-datastore`)

Goal: make the **built-in WoT relay's event store the system of record**, replacing the app's
`app.db` (SQLite via SQLAlchemy). The Nostr client becomes the face of the app; anyone signs
up/logs in with a Nostr key; admins grant AI access per-user; the relay ships enabled on new
installs. This is a **phased** migration behind a `storage_backend` flag — it can't be an atomic
swap of 18 tables without a broken, bot-dead tree (and `sync.sh` deploys to every node).

## Two stores, clearly separated
- **`app.db`** (today): user/identity, conversations, messages, settings, bots, maps, blob meta…
- **`nostr_relay.db`** (the relay's OWN sqlite event store, `app/services/nostr_relay/store.py`):
  signed Nostr events. **This becomes the system of record.** It is a *different file* from `app.db`.

The app reaches it as a **Nostr client to its own local relay** (publish/REQ over `ws://127.0.0.1:<port>/relay`,
same path `client.py:signup-follow` already uses) — NOT by cross-thread access to `RelayStore`.

## Durability (must fix before accounts live here)
This deployment points `nostr_relay_db_path` at **tmpfs with ~10-min disk snapshots** — fine for a
replaceable feed cache, fatal for accounts/settings (a reboot drops recent signups). Migration step:
move the relay store to a **durable path** (or snapshot app-data kinds synchronously on write).

## Event-kind schema (no timeline spam)
Nostr clients only render kind 1/6 in feeds, so app data uses **app-specific kinds** that never
appear in timelines, is **NIP-44 encrypted**, and is **never fanned to upstream relays** (the outbox
only re-broadcasts real client posts). The relay can also restrict these kinds to their owner.

| Domain | Event | Signer | Encrypted to |
|--------|-------|--------|--------------|
| Global settings | kind **30078** (NIP-78 app-data), one per key `d=pcai:setting:<key>` | operator | operator (secrets!) |
| Bots / maps / blob-meta / operational | kind **30078**, `d=pcai:<table>:<id>` | operator | operator |
| User account record (admin/can_ai/etc.) | kind **30078**, `d=pcai:user:<npub>` | operator | operator |
| User profile (kind-0 already) | kind **0** | user | — (public) |
| User conversations | kind **30078**, `d=pcai:conv:<id>` | user storage key | user |
| User messages (chat history) | kind **3xxxx** (regular, per-conv `e`-tag) | user storage key | user |
| AI-access request | kind **30078** `d=pcai:ai-request:<npub>` + DM to admins | user | — |

## Key custody (decided)
Server-side AI/bots must read chats to generate replies, so true end-to-end (server-can't-read) is
impossible while inference is server-side. Model: **each user gets a server-held storage keypair**
(extends `User.nostr_nsec`). Identity = their login npub (NIP-07/Amber/nsec); the storage key is
what the server uses to encrypt-at-rest to the relay and decrypt for the AI/bots. Encryption is
**at rest in the relay**, not e2e.

## Auth
Nostr login/signup (NIP-07 / nsec / Amber NIP-46 — signers already built in the client). New users
sign up with their key; existing password admins keep working and are linked to their npub
(account-merge migration moves their data to their configured npub).

## AI access = request → approve (+ notify)
- A non-AI user hits an **"AI" button** in the Nostr client → "Request AI access" → writes an
  AI-access request event AND sends each admin a **Nostr DM/notification** (the admin sees it in the
  client's notifications + optionally their existing Telegram/Matrix relay).
- Admin approves from the user's profile ☰ menu (like the Blossom grant we just built) → sets the
  user's `can_ai` → the AI tab unlocks for them. Revocable the same way.

## Phases
0. **Foundation** (this branch, in progress): NIP-44 (Python) + the event-store repository layer
   (publish/REQ app-data kinds over the local relay WS) + durable relay path.
1. **Identity/auth + UI merge + AI gating**: Nostr login/signup for the AI app; `/client` as the
   face with an AI button; `can_ai` + request/approve/notify; relay-on-by-default. Account-merge
   migration (admin data → configured npub).
2. **User content → relay**: conversations + messages as encrypted events (repository swap behind
   the flag) + one-time migration from `app.db`.
3. **Operational → relay**: settings, bots, maps, blob-meta as operator app-data events; retire the
   corresponding `app.db` tables. Bots/services read through the repository.

## UI = the Nostr client (old webui retired)
The standalone PosterChan AI web UI is **replaced**: the AI chat becomes a **view inside the Nostr
client** (`static/js/client/`), like Home/Settings. An **AI** nav button does `switchView('ai')`.
The client already has the signers (NIP-07/Amber/nsec), so the AI view authenticates by signing a
NIP event → `POST /api/auth/nostr-login` (sets the normal session cookie) → then talks to the AI
chat backend over that session. No separate login page. The old `templates/index.html` chat UI +
its routes are retired once the in-client AI view reaches parity.
- Non-`can_ai` users: the AI view shows **"Request AI access"** → writes a request event + notifies
  admins; admin approves from the profile ☰ menu → `can_ai` flips → the view unlocks.

## UI consolidation notes
- The old per-user **Files/Photos** feature (`shared_files` / user uploads) is **obsolete** — the
  Blossom **Files** view (in the Nostr client) replaces it. Blossom still uses the **storage proxy**
  (`blossom_storage_backend=proxy`, `storage_server_url`) — keep that. Migration step: drop the old
  Files/Photos UI + routes once Blossom Files is the single file surface.

## Compatibility
Every bot/service keeps using a single **repository API** (`repo.*`) instead of `db.query(...)`.
The repo is backed by `app.db` today and swapped to the relay store per-domain behind the flag, so
features keep working through the migration rather than breaking at a big-bang cutover.
