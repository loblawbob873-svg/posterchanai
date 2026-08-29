# Bot manager (`botframework/`)

> **Using the bots / command reference:** see [`COMMANDS.md`](COMMANDS.md) — how to drive each
> bot (web, Telegram, Pleroma) and every command grouped by category.


PosterChanAI bundles a full **autonomous bot framework** (formerly the separate `~/posterchan`
repo) under [`botframework/`](../botframework), managed from **Admin → Bots**. One admin tab adds,
edits, and toggles bots; the database is the source of truth; the app supervises the bots
in-process. No separate repo, no hand-edited `bots_config.py`.

## What a bot is

A **bot** is one long-running listener (or a scheduled image poster) on one fediverse account:

- **Platforms:** Pleroma/Mastodon, Nostr.
- **Types:** **text** (continuous listener) or **image** (scheduled poster, daily at 0/6/12/18).
- **Features** (text bots) map to behaviours: reply to mentions and the
  blockbot / welcome / report / hashtag / unfollow daemons.

Each bot stores its identity/filter fields as columns (`name`, `enabled`, `bot_type`,
`platform`, `host`, `modes`) plus a JSON `config` blob for everything else (creds, prompt,
feature options) — see the `Bot` model in `app/models.py`.

## Architecture

```
posterchanai (FastAPI, port 3051)
 ├─ bot_manager_service        (app/services/bot_manager_service.py)
 │    ├─ reads Bot rows for THIS host from the DB
 │    ├─ builds per-bot env from the global settings + the bot's JSON config
 │    ├─ spawns  botframework/main.py <modes>  (one child process per bot)
 │    └─ reconcile loop: starts enabled bots, restarts crashes (rate-limited),
 │        stops disabled/deleted ones; image bots run on a daily schedule
 └─ Admin → Bots  ──REST──>  /api/admin/bots  (app/routers/bots.py)
```

The bots run as **separate processes** and reach the shared LLM + image generation over HTTP
(they can't share the single GPU-loaded model in-process). That HTTP target is **one server URL**
(below). Wired into the **port-3051** startup/shutdown guard, alongside the other schedulers.

> Single-worker assumption: the in-memory process registry is correct only on the one port-3051
> instance (same caveat as the node-job registry and the social poller).

## Global bot settings (Admin → Bots → "Global bot settings")

Shared by every bot — set once:

| Setting | Purpose |
|---|---|
| **PosterChanAI server URL** (`bots_server_url`) | The one endpoint. Chat hits `{server}/api/chat/completions`; image generation uses the same server's API. e.g. `https://ai.poster.place` |
| AI API key / AI model | Auth + model for the chat endpoint |
| App username / password / API key | Login the bots use for the **image** API (one is enough) |
| SearXNG URL | Web search for the bots |
| Timezone | Bot clock |
| Pleroma DB user / password / host | Postgres access for blockbot/welcome/report |

There is **no** separate OpenAI endpoint setting — everything derives from the single server
URL, and image generation always goes through the PosterChanAI server's native diffusers
backend (`USE_POSTERCHANAI` is forced on).

**Master kill-switch — "Run bots on this server" (`bots_manager_enabled`, default off).** The
manager runs **no** bots until this is on. This lets a node deploy the code safely while the
legacy service still owns the bots; flip it on only after cutover (below) to avoid double-posting.

## Per-bot config (Add / Edit bot)

The modal shows only the fields the chosen platform/type needs:

- **Name, Type, Platform, Host** — `Host` empty = run on any node; otherwise must match the
  node's hostname (so each node runs only its own bots).
- **Credentials** — Pleroma: Server URL, Bot username, Access token (Pleroma report bot
  also needs an **admin token**).
- **Features** (text bots) → `main.py` modes: Reply to mentions, Welcome, Block,
  Report, Hashtag, Unfollow, **Data Vending Machine (NIP-90)**. (No raw `--flags` to type.)
- **Personality prompt.**
- **Voice / narration** — TTS voice/rate/pitch, auto-narrate.
- **Pleroma database name** — for block/welcome/report bots.
- **Per-feature content** — welcome message/prompt/image/lookback, block image/prompt, report
  image/prompt, unfollow image/silent-mode (shown only when that feature is enabled).
- **Advanced (JSON)** — any extra keys (e.g. `shamebot_rooms`, `stickers_enabled`). A key from a
  retired feature shows up here too, so you can see it and clear it rather than have it dropped.

On/Off per bot toggles `enabled`; the manager reconciles within a few seconds. (Nothing runs
unless the master kill-switch is also on.)

### Data Vending Machine (NIP-90)

A **Nostr bot** with the **Data Vending Machine** feature checked runs `botframework/dvmListener.py`
(`--dvm` mode): it watches the bot's relays for NIP-90 **job requests** (kind 5xxx) and fulfils them
with this node's AI, publishing a **result** (kind 6xxx = request + 1000) plus **feedback** (kind
7000), signed by the bot's key. It handles **text** jobs — `5050` text-generation and `5000`/`5001`
summarization — via the same `generate_reply` the chat bot uses, **and image jobs** (`5100`
image-generation → result `6100`): the prompt is rendered through the node's image backend, the PNG
is uploaded to the bot's Blossom host, and the result event carries the public image URL plus an
`imeta` tag. Capped at `DVM_MAX_PER_POLL` jobs per poll (default 3) so it can't monopolise the GPU.
Image jobs can be disabled per node with `DVM_IMAGE_ENABLED=0`. It needs the bot's Nostr key +
relays + the node's AI/image endpoint (all injected by the manager). It stays dormant until you
create a Nostr bot, tick the feature, and enable it.

### Chess referee — #chesstr

A **Nostr bot** with the **Chess referee** feature checked runs `botframework/chessListener.py`
(`--chess` mode): it lets two Nostr users play chess, with the bot as the board + referee. Every
post it makes carries the **#chesstr** hashtag (text + a `t` tag).

- **Start:** someone posts mentioning the bot **and** another user with the word "chess"
  (e.g. `@chessbot chess @bob`). The initiator is **White** (cyan), the opponent **Black**
  (magenta). The bot replies with a cyberpunk board (drawn with Pillow — no font/SVG deps) and the
  side-to-move's pieces **numbered**.
- **Move:** a player replies to the bot's board post with `<n> <square>` — e.g. `1 d4` moves the
  piece labelled **1** to **d4**. `SAN` (`Nf3`), `UCI` (`g1f3`), `O-O`/`O-O-O` and `resign` also
  work. The bot validates legality, applies the move, and posts the updated board tagging the other
  player. Illegal/wrong-turn attempts get a short nudge (and the legal destinations for that piece).
- **Game over:** checkmate / stalemate / draw / resign → a final board + result post.
- **State** is a replaceable **kind-30078** app-data event keyed by the game's root note id, so
  games **survive restarts and never expire** — a game can span days, just waiting for the next
  reply. Needs `python-chess` (in `requirements.txt`) + the bot's Nostr key/relays + a Blossom host
  for the board images (all injected by the manager).

## Migrating a node (cutover from the legacy `posterchan.service`)

On first start, if the `bots` table is empty and a (gitignored, local-only)
`botframework/bots_config_export.json` exists, the manager **seeds** bots + globals from it once.
To recover that export from a node still running the old setup, load its `bots_config` and dump
`TEXT_BOTS`/`IMAGE_BOTS` + globals to that JSON path.

Per node, in order (to avoid double-posting):

1. Deploy the code (via `sync.sh`).
2. Ensure bot deps are installed in the **service venv** (`install.sh` option 6 installs
   `requirements.txt` **and** `botframework/requirements.txt`, e.g. `psycopg2-binary`).
3. Restart posterchanai → it seeds the bots (kill-switch still off → nothing runs).
4. **Stop + disable** the legacy `posterchan.service`.
5. Flip **"Run bots on this server"** on.
6. Watch `journalctl -u posterchanai.service` for `[BOTS] started …`.

`sync.sh` only restarts `posterchan.service` on nodes where it is still **enabled**, so a
cut-over node won't have the old service resurrected.

## Notes

- **Python:** the bot code runs under the service venv. It is kept **3.11-compatible** (and runs
  natively on 3.13 nodes). `py_compile` under the service's Python catches any regressions.
- **Dependencies:** `psycopg2-binary`, `edge-tts`, `beautifulsoup4`, `lxml`, `Pillow`, `pytz`,
  `requests` (in `botframework/requirements.txt`, merged into the installer).
- **Incremental dedup (opt-in):** per platform a parity shim can route a bot's network calls
  through the app's shared service (`app/services/pleroma_service.py`) instead of
  the bot's own client. Enable with `"use_app_service": true` in a bot's Advanced config; validate
  offline with `botframework/test_pleroma_parity.py`. Off by default.

## Troubleshooting

- **Bot replies but image fails** → check the **PosterChanAI server URL** is set and reachable
  from the node (`curl -s -o /dev/null -w '%{http_code}' -X POST {server}/api/chat/completions`
  should be `422`, not connection-refused). Image gen always uses this server.
- **A DB bot crash-loops** (`ModuleNotFoundError: psycopg2`) → install bot deps into the service
  venv (`install.sh` option 6).
- **Nothing runs after enabling a bot** → confirm the master **"Run bots on this server"** switch
  is on for that node.
- **Logs:** `journalctl -u posterchanai.service` — manager lines are tagged `[BOTS]`; each bot's
  own output is forwarded inline.
