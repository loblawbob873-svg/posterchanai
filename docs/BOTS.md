# Bot manager (`botframework/`)

> **Using the bots / command reference:** see [`COMMANDS.md`](COMMANDS.md) — how to drive each
> bot (web, Telegram, Matrix, Pleroma, Misskey) and every command grouped by category.


PosterChanAI bundles a full **autonomous bot framework** (formerly the separate `~/posterchan`
repo) under [`botframework/`](../botframework), managed from **Admin → Bots**. One admin tab adds,
edits, and toggles bots; the database is the source of truth; the app supervises the bots
in-process. No separate repo, no hand-edited `bots_config.py`.

## What a bot is

A **bot** is one long-running listener (or a scheduled image poster) on one fediverse account:

- **Platforms:** Misskey, Pleroma/Mastodon, Matrix.
- **Types:** **text** (continuous listener) or **image** (scheduled poster, daily at 0/6/12/18).
- **Features** (text bots) map to behaviours: reply to mentions, nitter relays, and the
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

There is **no** separate OpenAI endpoint, ComfyUI, or Stable-Diffusion setting — everything
derives from the single server URL, and image generation always goes through the PosterChanAI
server (`USE_POSTERCHANAI` is forced on).

**Master kill-switch — "Run bots on this server" (`bots_manager_enabled`, default off).** The
manager runs **no** bots until this is on. This lets a node deploy the code safely while the
legacy service still owns the bots; flip it on only after cutover (below) to avoid double-posting.

## Per-bot config (Add / Edit bot)

The modal shows only the fields the chosen platform/type needs:

- **Name, Type, Platform, Host** — `Host` empty = run on any node; otherwise must match the
  node's hostname (so each node runs only its own bots).
- **Credentials** — Misskey/Pleroma: Server URL, Bot username, Access token (Pleroma report bot
  also needs an **admin token**). Matrix: server, user ID, access token, room ID, admins.
- **Features** (text bots) → `main.py` modes: Reply to mentions, Nitter feeds, Welcome, Block,
  Report, Hashtag, Unfollow. (No raw `--flags` to type.)
- **Personality prompt.**
- **Voice / narration** — TTS voice/rate/pitch, auto-narrate.
- **Pleroma database name** — for block/welcome/report bots.
- **Per-feature content** — welcome message/prompt/image/lookback, block image/prompt, report
  image/prompt, unfollow image/silent-mode (shown only when that feature is enabled).
- **Advanced (JSON)** — any extra keys (e.g. `nitter_feeds`, `shamebot_rooms`, `stickers_enabled`).

On/Off per bot toggles `enabled`; the manager reconciles within a few seconds. (Nothing runs
unless the master kill-switch is also on.)

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
- **Stickers (Matrix `!name`):** host-specific media in `botframework/stickers/` (gitignored),
  deployed per node; read live, no restart needed to add files. Enable per bot with
  `"stickers_enabled": true`.
- **Incremental dedup (opt-in):** per platform a parity shim can route a bot's network calls
  through the app's shared service (`app/services/{pleroma,misskey,matrix}_service.py`) instead of
  the bot's own client. Enable with `"use_app_service": true` in a bot's Advanced config; validate
  offline with `botframework/test_{pleroma,misskey,matrix}_parity.py`. Off by default.

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
