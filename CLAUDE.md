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

**Two remotes — `origin` is production, `github` is the public mirror.** `origin`
(`git.poster.place`, Gitea) is what `sync.sh`/`git push` deploys to **production**, so push
there **first**. The `github` remote (`github.com/loblawbob873-svg/posterchanai`) is a **public
mirror** whose default branch is `main`, mapped from local `master`: push to it explicitly with
`git push github master:main`. **Always prompt/confirm before pushing to GitHub** — it's public,
so never push there automatically as part of a normal deploy. Keep local `master` tracking
`origin` (so plain `git push` deploys, not publishes).

## Bot framework (merged from `~/posterchan` → `botframework/`)

The standalone `~/posterchan` bot framework now lives **in this repo** under `botframework/`
(co-located, imports kept root-relative; spawned as a subprocess with `cwd=botframework/`).
The bots are managed from **Admin → Bots** (`templates/admin/tabs/bots.html` +
`static/js/admin-bots.js`), backed by the `Bot` model and the `bot_manager_service`.

- **Manager:** `app/services/bot_manager_service.py` (the in-app replacement for `botctl.py`).
  Reads `Bot` rows for this host, builds per-bot env from the global `bots_*` settings + the
  bot's JSON `config` (a faithful port of `botctl.build_env`), and spawns
  `botframework/main.py <modes>`. A reconcile loop keeps enabled bots running (rate-limited
  restarts) and stops disabled/deleted ones. Wired into the **port-3051** startup/shutdown guard.
- **Config is DB-backed, not `bots_config.py`.** `bots_config.py` is gone at runtime;
  `botframework/config.py` reads its old globals from env (manager-injected). The `Bot` model is
  identity/filter columns + a JSON `config` blob (mirrors the old per-bot dict). Global settings
  are the `bots_*` keys in `SettingsResponse`.
- **Master kill-switch:** `bots_manager_enabled` (default **off**). The manager runs NO bots
  until it's on — so deploying the merged code is safe while the legacy `posterchan.service`
  still owns the bots. **Cutover per node:** retire `posterchan.service` (stop+disable), then flip
  "Run bots on this server" on in Admin → Bots. Doing both avoids double-posting.
- **Migration seed:** on first start, if the `bots` table is empty and a (gitignored, local-only)
  `botframework/bots_config_export.json` exists, the manager seeds bots + globals from it once.
  Fresh nodes start empty — add bots via the UI. (`nas.lan`'s Matrix bot is not yet seeded;
  migrate it before retiring its `posterchan.service`.)
- **Dedup is incremental** (Phase 4+). Per platform there's now a **parity shim** that routes the
  bot's network calls through the app's shared service while reusing the bot's higher-level logic
  verbatim (so behavior can't drift): `botframework/{pleroma,misskey,matrix}_shim.py` →
  `app/services/{pleroma,misskey,matrix}_service.py`. They're **opt-in** (per-bot
  `use_app_service:true` in Admin → Bots, which sets `PLEROMA_/MISSKEY_/MATRIX_USE_APP_SERVICE`)
  and **off by default**; each listener picks shim-vs-legacy at import. Validate a shim offline
  with `botframework/test_{pleroma,misskey,matrix}_parity.py` (A/B's the constructed HTTP). The
  Misskey/Matrix shims swap only the transport primitive (`misskey_post`/`matrix_request` +
  upload) and re-export the unchanged functions; Pleroma's reimplements the thin wrappers. Once a
  shim is confirmed in prod, delete the duplicated **network** code from the bot's client (keeping
  the pure helpers the shim reuses) — that's the actual line-count reduction, taken safely.
  TTS/search/news are **intentionally not shimmed**: the bot's TTS is mostly local ffmpeg/video
  work and the app's search/news are `db`-coupled class/router code — different tools, not
  duplicated network clients. The bot half of the fedi-timeline bridge (`matrixListener.py`) is now
  in `botframework/` too — no longer a separate repo to commit, but still its own listener.

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
against duplicate runs): `logs_scheduler`, `social_notifications_service`, `nitter_feeds_service`,
`fedi_timeline_service`, and `matrix_notifications_service`. Each exposes idempotent
`start_*`/`stop_*` helpers and is wired into the port-3051 startup/shutdown blocks.

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

- **Music generation** (`musicgeni` command; `app/services/music_service.py` +
  `music_factory.py`): text-to-song via a self-hosted **ACE-Step 1.5** REST server (`acestep-api`).
  ACE-Step needs Python 3.11–3.12 and a conflicting torch stack and is **not on PyPI**, so it runs
  as a SEPARATE process (installed by `./install.sh --music` via uv+git-clone, or the Docker
  `acestep` service) and the app is just an HTTP client — like `image_server_urls`/`finance_api_base`.
  `music_factory` mirrors `image_factory`: round-robin LB over `music_server_urls`, and the local
  `music_api_base` path takes the shared `GPUResourceLock` + `vram_manager.prepare_for_music()`
  (one GPU task at a time, swap LLM/image out). **Output is a branded MP4**, not raw audio:
  `media_service.make_music_video` puts the song over a generic PosterChan background
  (`render_music_background`) then appends the `append_outro` end-card ("watermark"); result type
  `generated_video` (falls back to `generated_audio` if ffmpeg is missing). **Vocals** need lyrics,
  so with no `| lyrics` the LLM auto-writes them (`_music_write_lyrics`); `instrumental` skips that.
  Web UI + Telegram only (NOT the fedi bots — abuse surface). REST gotchas: `/query_result` field
  is **`task_id_list`** (not `task_ids`), and its `result` is a **JSON-encoded string** whose items
  carry `file: "/v1/audio?path=..."`. Deployed: nas.lan (RTX 3060, CUDA) serves music; the Arc
  (server1) can't easily host it (XPU torch swap breaks ACE-Step's CUDA-pinned torchvision/audio ABI).
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
- **Fediverse timeline → Matrix bridge** (`app/services/fedi_timeline_service.py`): mirrors ONE
  Misskey/Pleroma timeline (home/global/local) into ONE admin-configured Matrix room, posting via
  the fedi-timeline bot account. State/dedup/action-routing go through the **`TimelinePost`** table;
  **`note_uri` (canonical AP URI) is the cross-instance dedup key**, `note_id` the same-instance
  fallback. A row is recorded for **every delivered event** (text + each media), so interacting
  with an image resolves to its post. Avatars + custom emoji are uploaded once and cached in
  **`MatrixAvatarCache`** (a generic URL→mxc cache). First poll just sets `fedi_timeline_since`
  (no backfill); subsequent polls **drain forward** page-by-page with **`min_id`** (Pleroma) /
  `sinceId` (Misskey) — a single `since_id` fetch silently drops everything beyond `limit` when a
  busy feed posts more than a page between polls (the old missing-posts bug). The drain commits the
  cursor per page and is bounded by `_DRAIN_BUDGET` (under the `_POLL_TIMEOUT` cap), so leftover
  drains next cycle with no gap.
  - **Rendering** (`_body_html`): header = avatar + bold display name + the **@handle** (plain text
    via `<font data-mx-color>`, NOT an `<a>` link — a link would make Element render a
    profile-preview card with bio; plain text is safe and keeps the sender identifiable even when
    the display name is blank). Mention/profile `<a>` links in the body are **stripped to plain
    text** (`_strip_profile_links`) for the same reason. Custom
    emoji shortcodes in names → inline `<img data-mx-emoticon>`. Quote-posts/boosts render the
    quoted original in a `<blockquote>`. A post **with media** is sent as ONE message: the first
    image carries the text as an **MSC2530 caption** (`send_image(caption=, caption_html=)`); extra
    images hang in its thread. (Matrix caption renders *below* the media — known layout limit.)
  - **Threading**: the feed delivers replies as flat items, so on delivery a reply is threaded
    under its parent if that parent is already in the room; if not, **ancestors are backfilled**
    (`_backfill_ancestors`, capped `_MAX_ANCESTORS`) to anchor the whole conversation to its real
    root. `send_event`/`send_image` take `thread_root_event_id` + `reply_to_event_id` (the real
    parent, preferring the parent's text event) so Element shows the true reply chain. A periodic
    `_poll_replies` re-checks the newest `_REPLY_MAX_ROOTS` roots over `_REPLY_WINDOW_HOURS`
    (throttled by `_REPLY_POLL_INTERVAL`); a brand-new post with `replies_count>0` is backfilled at
    delivery. All Matrix sends ride out rate limits via `_with_429_retry` (honours `retry_after_ms`).
  - **Member actions** → `POST /api/matrix/timeline-action` (Bearer API key, like `/command`),
    performed under the member's own account (same-instance → same-platform → any linked; resolves
    cross-instance via `note_uri`): top-level message → new post; thread reply → reply (resolved to
    the thread root, auto-mentioning the author); ❤/any emoji react → favourite/reaction (Misskey
    keeps the exact emoji; Pleroma emoji-react→favourite); 🔁 react → boost. **Reply shortcuts**:
    `boost`/`rt`/🔁 → boost, `fav`/`like` → favourite, `quote <comment>` → quote-post.
    **Share→boost/quote**: a message containing a `matrix.to` link (or matching a delivered post's
    body) acts on the ORIGINAL (boost, or quote with a comment) — and a `matrix.to` link is never
    federated raw. Actions are recorded (synthetic `event_id`) so the poller won't echo them back.
  - **The Matrix bot half is the SEPARATE `~/posterchan` repo** (`matrixListener.py`, gated on
    `FEDI_TIMELINE_ROOM_ID`): `_handle_timeline_event` for the timeline room and the
    notification-reply interception for DMs. A change to either contract must be made there too
    (commit that repo separately — `sync.sh` restarts but doesn't commit it).
  - **Per-user fedi rate limit**: a busy *global* feed bridged into one room can exceed Synapse's
    per-user message limit for the bot (symptom: HTTP 429 storms, posts trickle). Raise it with the
    Synapse admin API: `POST /_synapse/admin/v1/users/<bot>/override_ratelimit`
    `{"messages_per_second":50,"burst_count":500}` (needs a homeserver-admin token).
- **Personal fedi notifications → Matrix DM** (`app/services/matrix_notifications_service.py`):
  the Matrix counterpart of the social relay — DMs each user (via the fedi-timeline bot, in a
  per-user room persisted in `UserSetting matrix_notif_dm_room`) their Pleroma/Misskey
  notifications. Reuses the social relay's `_norm_*`/`_format`; keeps **its own** per-user cursors
  (`UserSetting matrix_notif_{platform}_since`) so it doesn't consume the Telegram relay's. Gated
  on global `matrix_notif_enabled` setting (admin kill-switch, default off) + the user's **own**
  per-user `User.matrix_notif_enabled` opt-in (independent of the Telegram relay's
  `social_notif_enabled` — separate toggle in User Settings → Matrix) + a linked Matrix account.
  First poll sets cursors (no backfill).
  Messages are rendered Matrix-native (hand-built HTML so handles like `@a_b_c` aren't markdown-
  mangled), include a 🔗 "open thread" link, and **mirror the conversation into the notification's
  Matrix thread** (`_thread_context` reuses the timeline bridge's `_deliver`) so it's readable in
  Element without the web. The cursor advances **per delivered notification** (not once at the end)
  so a mid-batch send failure can't cause the duplicate-flood class of bug. **Reply-back:** each
  notification with a post records a **`MatrixNotifyMap`** row (`(room_id, event_id)` → target
  note/status + visibility); replying to that DM message → `POST /api/matrix/notification-reply`,
  which posts a reply (text **or image** — handled before the bot's compress/convert media flow),
  or runs a `boost`/`fav` shortcut, on the user's account (returns `not a notification` so the bot
  falls through for non-notification replies). **Image + caption stitch:** Element sends an image
  reply and its caption as two events, which would post twice. The bot (`matrixListener.py`,
  `_pending_notif_replies`) holds an image-only reply for `_PENDING_IMAGE_GRACE` (12s) so the
  following text becomes its caption and they post as ONE reply (same mechanism as the timeline
  room's `_pending_image_posts`). It only holds when a no-post **`probe`** call (`probe:true` on
  `notification-reply`, returns `{is_notification}`) confirms the replied-to event is a tracked
  notification — otherwise an ordinary image reply (e.g. for a `compress`/`translate` media-action)
  must fall straight through. Orphan held images are flushed image-only each poll cycle. **Direct
  messages:** a fedi DM is never in the
  shared room — it arrives here as a notification (visibility `direct`), and a reply preserves that
  visibility (`notification-reply` uses `row.visibility`) so it stays a DM. To *start* a DM, the
  Matrix `/command` endpoint handles `dm @user@host <message>` → a `visibility="direct"` post
  (Pleroma/Mastodon; Misskey not supported — needs resolved `visibleUserIds`).

## Conventions / gotchas

- Routers thin, logic in services. Match surrounding style; plain-text Telegram messages avoid
  Markdown parse errors on arbitrary content.
- The in-memory node job registry and the social poller are **per-process** — correct on the
  single port-3051 instance; would need a shared store if ever scaled to multiple workers.
- Do not run `git gc`/maintenance on the Gitea server data dir (production).
- `app/routers/openai_api.py` is a generic proxy — keep it task-agnostic; never hardcode
  task-specific logic there.
- Detailed setup (RAG/MCP/LLM/image/IPEX/nginx) lives in `docs/`.
