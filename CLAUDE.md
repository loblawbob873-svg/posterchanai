# CLAUDE.md

Guidance for working in this repo. Keep changes small, clean, and consistent with the
patterns below.

## What this is

PosterChanAI — a self-hosted FastAPI app: streaming LLM chat (OpenAI-compatible `/v1/`),
image gen, TTS/STT, email/news/torrents, a file manager, a **Nostr client + relay**, and
**Telegram + fediverse bots**. Single-admin, multi-user, **Postgres**-backed.

## Run / dev

- Entry point: `python run.py` (uvicorn, **single worker**, port from `POSTERCHANAI_PORT`,
  default **3051**). On this deployment the Intel Arc box runs `posterchanai.service`
  (port 3051) + `posterchanai-xpu-image.service`; `nas.lan` runs `posterchanai`.
- venv at **`venv-unified/`** (`venv-unified/bin/python`) — there is no `venv/` on this deployment.
  Quick checks: `venv-unified/bin/python -m py_compile <files>`, and `-m pyflakes` for undefined
  names (what `sync.sh`'s pre-push gate runs).
- Logs: `journalctl -u posterchanai.service` (the fediverse `[PROXY] CONNECT ... SOCKS5`
  errors are unrelated federation noise — ignore when debugging features).

## Deploy — always via `sync.sh`

`./sync.sh` does `git commit -a -m fix && git push`, then restarts local services and
resets/restarts `nas.lan`. **`git commit -a` does NOT stage new untracked files** — `git add`
any new file before running it, or it ships a broken (ImportError) tree to every node.
`sync.sh` deploys **code only**, not Python deps (use `install.sh` option 6 for deps).

**Two remotes — `origin` is the NOSTR repo (production), `github` is the public mirror. Gitea is
gone.** `origin` is `nostr://npub1fdtthaq…/relay.poster.place/posterchanai`, served by the built-in
GRASP host at `https://poster.place/git/<owner-npub>/posterchanai.git` (see
`docs/GIT_OVER_NOSTR.md`). That is what `sync.sh`/`git push` deploys to **production**, so push there
**first**. The `github` remote (`github.com/loblawbob873-svg/posterchanai`) is a **public mirror**
whose default branch is `main`, mapped from local `master`: push to it explicitly with
`git push github master:main`. **Both remotes get every push, with no prompting** — finish a change
by committing and pushing to `origin` first (deploy), then mirroring the same commits to `github`,
so the public mirror never falls behind production. Keep local `master` tracking `origin` (so plain
`git push` deploys, not publishes).

```
git push                     # or ./sync.sh — origin/production first
git push github master:main  # mirror, same commits, every time
```

Push authorization is a **Nostr signature, not a connection**: only a maintainer of
`30617:<owner>:posterchanai` can move a ref, and the `pre-receive` hook reads the **hosting node's**
(nas) relay Postgres. server1 and nas run separate relays with separate event stores, so the repo
announcement lists **`wss://poster.place/git`** — that endpoint proxies to *nas's* relay, which is why
a push signed on server1 is visible to nas's hook. Everything is a public URL: no `nas.lan`, no SSH.

`scripts/grasp_mirror.py` is **no longer part of a deploy**. It existed to copy commits from a Gitea
`origin` onto the nostr repo; now that `origin` IS the nostr repo, `git push` already does it. The
script is kept for manual/recovery use only. (Provisioning/announcing is `scripts/grasp_selfhost.py`.)

**All three nodes pull from `origin` over nostr**, including `nas.lan` (`~/posterchanai`) and
`router.lan` (`/srv/posterchanai`, root-owned, served as `/static`). That needs `git-remote-nostr`,
which is installed in **`/usr/local/bin`** on every node — NOT `~/.local/bin`, because `sync.sh`
drives those pulls over non-interactive ssh and under `sudo`, neither of which sees a user PATH.

**Who owns the repo.** The owner pubkey IS the clone-URL path segment, so it decides which hosted
directory a push lands in. This repo is announced under the author's npub (`npub1fdtthaq…`) with the
hosting node's operator key (`npub19q5ezl4…`) kept in `maintainers`, so nas can still sign for it.

**Big pushes are chunked, and that's now handled.** Git switches to `Transfer-Encoding: chunked` once a
pack exceeds `http.postBuffer` (1 MB default) — the shape of any first full push. `git_host_main.py`
used to read `Content-Length` only, so the leftover chunk framing was parsed as the next request →
`400 Bad request syntax`; it now de-frames the body into a spool file and gives git-http-backend a real
`CONTENT_LENGTH` (`_read_chunked_body`, covered by `tests/test_git_host_browse_edit.py`). The old
per-node `git config http.postBuffer 524288000` workaround is harmless but no longer needed.

**Web git UI** (Discover → Git): the repo view browses a hosted repo — files, commit diffs, a branch/tag
switcher, per-file download, and an EDITOR that commits. Writes go `client → /client/git/edit → the git
host`, authorized by a **NIP-98 header verified against the repo's 30617 maintainers** (the same ACL as
`pre-receive`), with a `base`-sha compare-and-swap; the host then publishes the operator 30618 witness
and returns the 30618 tags for the client to sign. See `docs/GIT.md` (user guide) and
`docs/GIT_OVER_NOSTR.md` (internals).

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
  Fresh nodes start empty — add bots via the UI.
- **Dedup is incremental** (Phase 4+). Per platform there's now a **parity shim** that routes the
  bot's network calls through the app's shared service while reusing the bot's higher-level logic
  verbatim (so behavior can't drift): `botframework/{pleroma,misskey}_shim.py` →
  `app/services/{pleroma,misskey}_service.py`. They're **opt-in** (per-bot
  `use_app_service:true` in Admin → Bots, which sets `PLEROMA_/MISSKEY_USE_APP_SERVICE`)
  and **off by default**; each listener picks shim-vs-legacy at import. Validate a shim offline
  with `botframework/test_{pleroma,misskey}_parity.py` (A/B's the constructed HTTP). The
  Misskey shim swaps only the transport primitive (`misskey_post` +
  upload) and re-exports the unchanged functions; Pleroma's reimplements the thin wrappers. Once a
  shim is confirmed in prod, delete the duplicated **network** code from the bot's client (keeping
  the pure helpers the shim reuses) — that's the actual line-count reduction, taken safely.
  TTS/search/news are **intentionally not shimmed**: the bot's TTS is mostly local ffmpeg/video
  work and the app's search/news are `db`-coupled class/router code — different tools, not
  duplicated network clients.

## Architecture

| Area | Where |
|------|-------|
| Routers | `app/routers/*.py` (auth, chat, admin, telegram, misskey, pleroma, nostr, streams, …) |
| Services | `app/services/*.py` (business logic; routers stay thin) |
| Models | `app/models.py` (SQLAlchemy); DB init + migrations in `app/database.py` |
| Schemas | `app/schemas.py` (Pydantic) |
| Templates | `templates/` (Jinja2); admin tabs in `templates/admin/tabs/`, modals in `templates/includes/modals/` |
| Frontend JS | `static/js/` (`app.js`, `chat.js`, `admin.js`; Nostr client = `static/js/client/*.js` + `static/css/client.css`) |

### Commands (shared by web UI + Telegram)

`app/services/command_service.py` → `CommandService.COMMANDS` dict + `execute_command()`
switch. Reused by the web UI websocket (`app/routers/chat.py`) and Telegram.
**Gotcha:** Telegram does **not** use `parse_command`; it has its own hardcoded command list
(two identical spots in `app/routers/telegram.py`). A new command must be added **both** to
`COMMANDS` and to those Telegram lists, or it works in the web UI but falls through to the LLM
on Telegram.

**Gotcha (commands that consume uploads):** a command operating on uploaded files
(`compress`/`clip`/`convert`/`translate`) must be wired into each interface's media path
separately: `app/routers/chat.py` (`build_media_attachments` is gated by a command allowlist) and
the Telegram media-action keyboard/callbacks in `app/routers/telegram.py`.

**Media:** generic ffmpeg/Pillow/PyMuPDF helpers live in `app/services/media_service.py`
(`compress_*`, `clip_video`/`clip_attachment`, `convert_*`, `parse_timecode`). Video ops share
one HW-accel encoder autodetect (`_video_encoder_candidates`: NVENC → VAAPI → libx264). Telegram
makes `clip` interactive (start/end ForceReply prompts); the web UI passes both times in the
arg (`clip <start> <end>`).

### Settings

- **Admin (global):** key/value `Setting` table; typed defaults in
  `app/schemas.py:SettingsResponse`; `GET/PUT /api/admin/settings`. Admin UI is plain HTML in
  `templates/admin/tabs/*.html`; `static/js/admin.js` loads/saves **generically** by element
  `id`/`name` (no per-field JS). Add a field = add to `SettingsResponse` + an input in a tab.
  **REMOVING a setting takes three deletes, not one.** Dropping it from `SettingsResponse` only stops
  the code reading it; the VALUE lives on in two places that will resurrect each other:
  (1) the operator-signed relay doc `pcai:setting:<key>` (per node — each node has its own relay and
  operator key), and (2) a row in the legacy Postgres `settings` table, which
  `settings_store.migrate_legacy_table()` re-seeds into the relay **on every startup** for any key the
  relay doesn't already hold. So deleting only the relay doc looks like it worked and silently comes
  back on the next restart. Delete the legacy ROW FIRST, then the relay doc, on EVERY node, then
  restart and re-check. (Learned removing `finance_api_base`.)
- **Per-user:** columns on `User` (+ the `UserSetting` key/value table). Migrations for new
  `User` columns go in `app/database.py:_run_migrations` `new_user_columns` (ALTER-on-startup);
  **new tables** are auto-created by `Base.metadata.create_all` in `init_db()`. UI lives in
  `templates/includes/modals/user_settings.html`, saved via `/api/auth/settings`
  (`app/routers/auth.py`), with payload build/load in `static/js/chat.js`.

### Schedulers

APScheduler `AsyncIOScheduler`. The pollers run in a **separate worker process**
(`app/worker.py`, spawned from `app/main.py` **only on port 3051** so they can't double-run):
`logs_scheduler`, `social_notifications_service`, `nitter_feeds_service`, `uptime_service`, and the
three fediverse↔Nostr bridge services (`fedi_nostr_bridge_service`, `fedi_nostr_writeback_service`,
`fedi_nostr_personal_service`). Each exposes idempotent `start_*`/`stop_*` helpers. The in-process
port-3051 schedulers (relay, streams, bot manager, reminders, DVM, blossom cleanup, tor) stay in
`app/main.py`. **Worker gotcha:** the worker must read `*_enabled` flags from the DB, not
build-time defaults — a service reading the default silently never runs.

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

**Uptime monitoring** (`app/services/uptime_service.py`, Admin → Services → "Uptime Monitoring",
Discover → Server Stats → **Uptime** tab): HTTP monitors with heartbeats, response time and 24h/30d
uptime, alerting on up→down / down→up over Telegram and NIP-17 DMs. All state is ONE operator-signed
kind-30078 doc (`pcai:kv:uptime`) — no SQL table. The checks run in the WORKER (sole writer); the app
process only READS the doc for the public `/client/uptime` endpoint. **Gotcha:** it reads with
`nostr_store.get_doc(..., strict=True)` and refuses to persist unless the restore succeeded —
`_ws_query` otherwise returns `[]` for BOTH "no document" and "relay unreachable", and writing on the
strength of that empty read replaces the whole history (the same replaceable-doc wipe that took out a
drive's `pcai:files-index`; `scripts/restore_files_index.py` is the recovery for that one).

## Notable features

- **Music generation** (`musicgeni` command; `app/services/music_service.py` +
  `music_factory.py`): text-to-song via a self-hosted **ACE-Step 1.5** REST server (`acestep-api`).
  ACE-Step needs Python 3.11–3.12 and a conflicting torch stack and is **not on PyPI**, so it runs
  as a SEPARATE process (installed by `./install.sh --music` via uv+git-clone, or the Docker
  `acestep` service) and the app is just an HTTP client — like `image_server_urls`/`music_server_urls`.
  `music_factory` mirrors `image_factory`: round-robin LB over `music_server_urls`, and the local
  `music_api_base` path takes the shared `GPUResourceLock` + `vram_manager.prepare_for_music()`
  (one GPU task at a time, swap LLM/image out). **Output is a branded MP4**, not raw audio:
  `media_service.make_music_video` puts the song over a generic PosterChan background
  (`render_music_background`) then appends the `append_outro` end-card ("watermark"); result type
  `generated_video` (falls back to `generated_audio` if ffmpeg is missing). **Vocals** need lyrics,
  so with no `| lyrics` the LLM auto-writes them (`_music_write_lyrics`); `instrumental` skips that.
  Web UI + Telegram only (NOT the fedi bots — abuse surface). REST gotchas: `/query_result` field
  is **`task_id_list`** (not `task_ids`), and its `result` is a **JSON-encoded string** whose items
  carry `file: "/v1/audio?path=..."`. Deployed: BOTH nas.lan (RTX 3060, CUDA) and the Arc (server1,
  A770 XPU) host ACE-Step and serve music fine (the Arc's torch-XPU trio works — see the musicgeni
  memory for the soundfile/torchcodec workarounds).
- **Video generation** (`videogeni` command; `app/services/video_service.py` + `video_factory.py` +
  `app/routers/video_api.py`): text-to-video, **NATIVE in-process diffusers** (unlike music — LTX/Wan/
  CogVideoX are stock diffusers pipelines on the SAME torch stack as image gen, so no separate
  server). `video_service` is the generator (generic `DiffusionPipeline.from_pretrained` → any T2V
  model via the `video_model` setting), `video_factory` mirrors `music_factory` for node→node LB over
  `video_server_urls`+local with the shared `GPUResourceLock` + `vram_manager.prepare_for_video` swap.
  Output is a branded MP4 (`media_service.make_generated_video` → frames→mp4 + generic `append_outro`
  watermark + optional lanczos upscale to `video_upscale_height`). Web UI + Telegram only (NOT fedi
  bots). Portability: stock diffusers + SDPA only — NO flash-attn/xformers/fp8/GGUF (break Arc/ROCm).
  **Arc(XPU) gotcha:** Wan VAE conv3d OOMs in fp32 → load VAE bf16 + `enable_tiling()`; CPU-offload
  does NOT work on XPU (CUDA-only), so Arc is limited to models that fit fully (Wan-1.3B on 16GB).
  Frames clamp to `video_max_frames` (per-node VRAM cap) to avoid OOM. **Deployed:** server1/Arc =
  primary (Wan-1.3B, 49f); nas/3060 = secondary via offload, and since music+video share nas's 12GB,
  `video_free_music=true` makes a video render stop `acestep` (sudo systemctl) to reclaim VRAM,
  restarting it for music. New dep: `sentencepiece` (T5 tokenizer). Turn-key: `./install.sh --video`,
  Docker `POSTERCHANAI_VIDEO=1`. See `docs/VIDEO.md`.
- **Budget** (`static/js/client/budget.js`; Discover → Budget): bills, a monthly summary and
  "Plans" (categories of line items), stored as ONE kind-30078 doc `d=pcai:budget` that is
  **NIP-44-encrypted to the user's OWN key** — not the server-held storage key the rest of the app
  uses. That is the point: nobody but that user can read their finances, so there is no server-side
  `budget`/`pay` and none on Telegram either. Replaces a separate self-hosted Budget Manager Flask
  app — `finance_service.py`, the `finance_api_base` setting (incl. its relay doc) and the
  `User.finance_api_key` column are all gone. The surviving `bill` command lives in
  `command_service/bill.py` (`_BillMixin`).
  **Gotcha:** the doc is replaceable, so every write is a read-modify-write of the whole document
  and they MUST stay serialized (`chain` in budget.js) or concurrent saves silently drop changes.
  Summary math is ported verbatim from the old Flask app — `settled(row) = paid OR hidden this
  month`; `remaining = income − paid − due` — and was checked against the live app's `/api/v1/summary`
  before the cutover. The `bill` photo-OCR command survives, but SPLIT: the server does OCR +
  extraction and sets the reminder, the client writes the encrypted row (`PCBudget.addParsed`).
  **Add Bill with AI** is the same idea inside Budget itself: `POST /api/budget/scan` (chat.py, app
  session) takes a photo/PDF and calls the SAME `CommandService._bill_command`, so there is one OCR
  pipeline and one prompt; the client shows the parse in EDITABLE fields (OCR mangles decimal points
  far more often than names) and only then writes the row. Camera and file are two separate inputs —
  `capture=` jumps straight to the camera, which is wrong when you wanted a file you already have.
  Retired commands (`budget`/`bills`/`pay`/`addbill`/`finance`) are kept in `RETIRED_COMMANDS`, matched
  by `parse_command` AND short-circuited in `execute_command` (Telegram never calls `parse_command`),
  so they answer "it moved to Discover → Budget" instead of falling through to the LLM, which would
  invent a budget it cannot read. They stay OUT of `COMMANDS` so `help` doesn't advertise them.
  Migration off the old SQLite DB is `scripts/export_budget_db.py` → paste into Budget → Import
  (it can't be a server script — only the browser holds the key).
- **Remote node management** (`app/services/node_service.py`, `node` command): run OS commands
  on SSH-reachable nodes (or `local`), agentic mode, long-running **background jobs**
  (start → job id → result posted back to the originating channel). Config in Admin → Services
  (`node_exec_*`). Output: tail inline, full output (≤1 MB) as a `.txt`. **Intentionally
  unrestricted RCE** — gated by enable flag + user allowlist + admins, fully logged. The
  **system-health report** (`logs_scheduler`, see Schedulers) reuses `run_agent` over these same
  nodes, so it needs `node_exec_enabled`.
- **Social notification relay** (`app/services/social_notifications_service.py`): poller
  forwards Misskey/Pleroma notifications to a user's Telegram; replying to a forwarded
  message posts back to the platform (`SocialReplyMap` maps Telegram msg → target). Per-user
  toggle (User Settings → Telegram) + global kill-switch (default on). Misskey needs a one-time
  re-connect for the `read:notifications` scope.
- **Fediverse ↔ Nostr bridge** — three worker services, all sharing `fedi_normalize.py`:
  - **`fedi_nostr_bridge_service.py`** (fedi → Nostr): mirrors a Misskey/Pleroma timeline onto
    Nostr under a **puppet** key per fedi author (deterministically derived, so an author keeps
    one npub). `note_uri` (canonical AP URI) is the cross-instance dedup key, `note_id` the
    same-instance fallback. First poll only sets the cursor (no backfill); later polls **drain
    forward** page-by-page with `min_id`/`sinceId` — a single `since_id` fetch silently drops
    everything past `limit` when a busy feed outruns one page (the old missing-posts bug). The
    drain commits its cursor per page and **sorts by id**, so a partial drain resumes with no gap.
  - **`fedi_nostr_writeback_service.py`** (Nostr → fedi): a Nostr reply/reaction/repost on a
    bridged post is performed back on the fediverse under the acting user's own linked account.
    **NIP-25 gotcha:** for kinds 6/7 the target is the **last** `e` tag, not the reply-marked one
    — `_referenced_event_ids` prefers the reply marker, so the target resolver is kind-aware.
  - **`fedi_nostr_personal_service.py`**: per-user personal fedi notifications → Nostr DMs.
    Keeps its **own** cursors so it never consumes the Telegram relay's.
  - **Identity** (`fedi_bridge_identity.py`): `nip05_name_for` appends a sha256[:6] digest
    whenever sanitising the handle is lossy — without it two distinct fedi accounts could claim
    one NIP-05 name (a hijack; 54 rows were repaired). `ensure_puppet` reuses an existing puppet
    by `acct` rather than minting a second one.
  - **Access** (`fedi_bridge_access.py`): `enable(db, user, by_admin=False)` is gated on the
    `fedi_bridge_self_serve` setting (default **OFF**) — self-serve enable was a privilege
    escalation. Instance URLs go through the `rss_service` SSRF guard
    (`looks_fetchable`/`is_safe_host`, `follow_redirects=False`).
  - **`fedi_normalize.py`** is extracted VERBATIM from the old bridge and is **proven** code —
    change it only with a very good reason; every bridge service depends on it.

## Conventions / gotchas

- Routers thin, logic in services. Match surrounding style; plain-text Telegram messages avoid
  Markdown parse errors on arbitrary content.
- The in-memory node job registry and the social poller are **per-process** — correct on the
  single port-3051 instance; would need a shared store if ever scaled to multiple workers.
- Do not run `git gc`/maintenance on the Gitea server data dir (production).
- `app/routers/openai_api.py` is a generic proxy — keep it task-agnostic; never hardcode
  task-specific logic there.
- Detailed setup (LLM/image/IPEX/nginx) lives in `docs/`.
