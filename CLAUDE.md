# CLAUDE.md

Guidance for working in this repo. Keep changes small, clean, and consistent with the
patterns below.

## What this is

PosterChanAI — a self-hosted FastAPI app: streaming LLM chat (OpenAI-compatible `/v1/`),
image gen, RAG/MCP, TTS/STT, email/news/torrents, a file manager, and **Telegram + Matrix
bots**. Single-admin, multi-user, SQLite-backed.

## Run / dev

- Entry point: `python run.py` (uvicorn, **single worker**, port from `POSTERCHANAI_PORT`,
  default **3051**). On this deployment the Intel Arc box runs `posterchanai.service`
  (port 3051) + `posterchanai-xpu-image.service`; `nas.lan` runs `posterchanai`.
- venv at `venv/` (`venv/bin/python`). Quick checks: `venv/bin/python -m py_compile <files>`.
- Logs: `journalctl -u posterchanai.service` (the fediverse `[PROXY] CONNECT ... SOCKS5`
  errors are unrelated federation noise — ignore when debugging features).

## Deploy — always via `sync.sh`

`./sync.sh` does `git commit -a -m fix && git push`, then restarts local services and
resets/restarts `nas.lan`. **`git commit -a` does NOT stage new untracked files** — `git add`
any new file before running it, or it ships a broken (ImportError) tree to every node.
`sync.sh` deploys **code only**, not Python deps (use `install.sh` option 6 for deps).

## Architecture

| Area | Where |
|------|-------|
| Routers | `app/routers/*.py` (auth, chat, admin, telegram, matrix, misskey, pleroma, …) |
| Services | `app/services/*.py` (business logic; routers stay thin) |
| Models | `app/models.py` (SQLAlchemy); DB init + migrations in `app/database.py` |
| Schemas | `app/schemas.py` (Pydantic) |
| Templates | `templates/` (Jinja2); admin tabs in `templates/admin/tabs/`, modals in `templates/includes/modals/` |
| Frontend JS | `static/js/` (`app.js`, `chat.js`, `admin.js`) |

### Commands (shared by web UI + Telegram)

`app/services/command_service.py` → `CommandService.COMMANDS` dict + `execute_command()`
switch. Reused by the web UI websocket (`app/routers/chat.py`) and Telegram.
**Gotcha:** Telegram does **not** use `parse_command`; it has its own hardcoded command list
(two identical spots in `app/routers/telegram.py`). A new command must be added **both** to
`COMMANDS` and to those Telegram lists, or it works in the web UI but falls through to the LLM
on Telegram.

**Gotcha (commands that consume uploads):** a command operating on uploaded files
(`compress`/`clip`/`convert`/`translate`) must be wired into each interface's media path
separately: `app/routers/chat.py` (`build_media_attachments` is gated by a command allowlist),
the Telegram media-action keyboard/callbacks in `app/routers/telegram.py`, and the Matrix
allowlist in `app/routers/matrix.py`. The **Matrix bot client is a SEPARATE repo** at
`~/posterchan` (`matrixListener.py`, runs as `posterchan.service`) — it caches the upload and
only forwards it for commands in *its own* hardcoded list, so a new media command must be added
there too or it silently runs with no attachment. (`sync.sh` here restarts `posterchan` but
does **not** commit it.)

**Media:** generic ffmpeg/Pillow/PyMuPDF helpers live in `app/services/media_service.py`
(`compress_*`, `clip_video`/`clip_attachment`, `convert_*`, `parse_timecode`). Video ops share
one HW-accel encoder autodetect (`_video_encoder_candidates`: NVENC → VAAPI → libx264). Telegram
makes `clip` interactive (start/end ForceReply prompts); web UI and Matrix pass both times in the
arg (`clip <start> <end>`).

### Settings

- **Admin (global):** key/value `Setting` table; typed defaults in
  `app/schemas.py:SettingsResponse`; `GET/PUT /api/admin/settings`. Admin UI is plain HTML in
  `templates/admin/tabs/*.html`; `static/js/admin.js` loads/saves **generically** by element
  `id`/`name` (no per-field JS). Add a field = add to `SettingsResponse` + an input in a tab.
- **Per-user:** columns on `User` (+ the `UserSetting` key/value table). Migrations for new
  `User` columns go in `app/database.py:_run_migrations` `new_user_columns` (ALTER-on-startup);
  **new tables** are auto-created by `Base.metadata.create_all` in `init_db()`. UI lives in
  `templates/includes/modals/user_settings.html`, saved via `/api/auth/settings`
  (`app/routers/auth.py`), with payload build/load in `static/js/chat.js`.

### Schedulers

APScheduler `AsyncIOScheduler`, started in `app/main.py` startup **only on port 3051** (guard
against duplicate runs): `logs_scheduler` and `social_notifications_service`.

`logs_scheduler` (`app/services/logs_scheduler.py`) is the **agentic system-health report**, not a
hardcoded log collector anymore. For each selected node it drives `node_service.run_agent`
(read-only diagnostics → plain-text summary), then a **deterministic** Python pass (`_render_board`)
renders the fixed emoji status board — Python owns the icons/layout/status, never the model (the
agent model gathers reliably but won't honour a strict format). Files the report in the admin's
"Logs" conversation + Telegram. One entry point, `run_logs_for_admin`, is shared by the cron job,
the admin **Run Logs** button, and the `/logs` command; the interactive call passes
`deliver_telegram=False` so its return value isn't also pushed (double-send). Nodes come from
Remote Node Management (+ a synthetic `local`); the `logs_nodes` setting narrows the set.

### Telegram delivery

Module-level singleton `telegram_service` (`app/services/telegram_service.py`); optional local
Bot API server via the `telegram_api_base` setting (lifts the 20 MB file cap). Background
callbacks that fire after a request must **not** reuse the request's DB session (it's closed) —
open a fresh `SessionLocal` and capture any needed config up front.

## Notable features

- **Finance (Budget Manager)** (`app/services/finance_service.py`; `budget`/`bills`/`pay`/`addbill`
  commands): thin async client for the self-hosted Budget Manager Flask app's `/api/v1/*`
  (summary/bills/add/pay), reached at the global `finance_api_base` setting (default
  `http://localhost:5001`). It's **multi-user**: each PosterChanAI user connects their own finance
  account via a **per-user `User.finance_api_key`** (Settings → Finance in the web UI; sent as
  `X-API-Key`) — a global key would make everyone act as finance user #1. Commands live in
  `CommandService` so the web UI, Telegram and Matrix all share them; Telegram adds an interactive
  budget view (`_send_budget`, `fin:` callbacks: tap a bill to pay, ➕ add via ForceReply, 🔄
  refresh — bill id→name resolved through the per-chat `_finance_bills_cache`). Matrix gets the
  plain-text rendering for free.
- **Remote node management** (`app/services/node_service.py`, `node` command): run OS commands
  on SSH-reachable nodes (or `local`), agentic mode, long-running **background jobs**
  (start → job id → result posted back to the originating channel). Config in Admin → Services
  (`node_exec_*`). Output: tail inline, full output (≤1 MB) as a `.txt`. **Intentionally
  unrestricted RCE** — gated by enable flag + user allowlist + admins, fully logged. The
  **system-health report** (`logs_scheduler`, see Schedulers) reuses `run_agent` over these same
  nodes, so it needs `node_exec_enabled`.
- **Social notification relay** (`app/services/social_notifications_service.py`): poller
  forwards Misskey/Pleroma/Matrix notifications to a user's Telegram; replying to a forwarded
  message posts back to the platform (`SocialReplyMap` maps Telegram msg → target). Per-user
  toggle (User Settings → Telegram) + global kill-switch (default on). Misskey needs a one-time
  re-connect for the `read:notifications` scope.
  - **Matrix** (`matrix_service.fetch_notifications`, `/sync`-based, never backfills — first
    poll just sets the cursor): **DM rooms** (resolved from the user's `m.direct` account data)
    forward every incoming message; **group rooms** forward mentions only (`_mentions_user`
    matches the mxid in `m.mentions`/pill/body); own messages always excluded.
  - **Encrypted (E2EE) Matrix** content can't be read (no Olm/Megolm; token-only). The sync
    filter requests `m.room.message` **and** `m.room.encrypted`; encrypted events in **DM
    rooms** are collapsed into a single **"open Element" notice per room per poll** (no content,
    no `SocialReplyMap` row since there's nothing to reply to); encrypted group events are
    ignored. E2EE-by-default means this is the common DM case.

## Conventions / gotchas

- Routers thin, logic in services. Match surrounding style; plain-text Telegram messages avoid
  Markdown parse errors on arbitrary content.
- The in-memory node job registry and the social poller are **per-process** — correct on the
  single port-3051 instance; would need a shared store if ever scaled to multiple workers.
- Do not run `git gc`/maintenance on the Gitea server data dir (production).
- `app/routers/openai_api.py` is a generic proxy — keep it task-agnostic; never hardcode
  task-specific logic there.
- Detailed setup (RAG/MCP/LLM/image/IPEX/nginx) lives in `docs/`.
