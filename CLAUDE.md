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
  verbatim (so behavior can't drift): `botframework/pleroma_shim.py` →
  `app/services/pleroma_service.py`. It's **opt-in** (per-bot
  `use_app_service:true` in Admin → Bots, which sets `PLEROMA_USE_APP_SERVICE`)
  and **off by default**; each listener picks shim-vs-legacy at import. Validate a shim offline
  with `botframework/test_pleroma_parity.py` (A/B's the constructed HTTP). Pleroma's shim
  reimplements the thin wrappers. Once a
  shim is confirmed in prod, delete the duplicated **network** code from the bot's client (keeping
  the pure helpers the shim reuses) — that's the actual line-count reduction, taken safely.
  TTS/search/news are **intentionally not shimmed**: the bot's TTS is mostly local ffmpeg/video
  work and the app's search/news are `db`-coupled class/router code — different tools, not
  duplicated network clients.

## Architecture

| Area | Where |
|------|-------|
| Routers | `app/routers/*.py` (auth, chat, admin, telegram, pleroma, nostr, streams, …) |
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

**Gotcha (commands that consume uploads):** whether a command is handed the upload's raw BYTES is
`CommandService.wants_attachments()` — `MEDIA_TOOL_COMMANDS` (`compress`/`clip`/`convert`/
`translate`/…) plus the effect sets, aliases resolved. Both chat paths and Telegram
(`_TG_EFFECTS`/`_TG_RAW_MEDIA_COMMANDS` in `app/routers/telegram/messages.py`) derive from it, so a
NEW media tool goes in `MEDIA_TOOL_COMMANDS`, and a new/renamed EFFECT needs nothing. They used to
be four hand-copied literals of ~99 names: renaming `anyways` → `lookingaway` left the effect
running with `attachments=None` (it answered "attach an image"), and the Telegram copies had
already lost `goon`/`hag`. `tests/test_effect_command_coverage.py` fails if a copy comes back.
The Telegram media-action keyboard/callbacks are still wired per command.

**Gotcha (effect aliases):** an alias whose target is an EFFECT must be resolved before anything
gated on `command in MOTION_EFFECTS` — `execute_command` resolves at its public entry for exactly
that reason (the outro end-card and auto-compress are keyed on the name), and every endpoint that
validates against the catalogue (`/meme/effect`, `/meme/apply-effect`, `/effects/run`) aliases
BEFORE the allowlist check, since clients cache the catalogue and keep sending the old name.

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
  **One file per tab, grouped in the nav** (`templates/admin.html`: AI / Nostr / Messaging / Media /
  System). A new tab = a `<div class="tab-content" id="tab-NAME">` file + an `{% include %}` + a
  `data-tab="NAME"` button; anything lazy-loaded hangs off that button's click (Bots, Emoji, Storage
  do). admin.js remembers the open tab in `localStorage` and honours `#tab-NAME`.
  **A field missing from `SettingsResponse` never hydrates.** GET returns the typed model, so an
  undeclared key is dropped from the response, the input loads blank on every visit, and a CHECKBOX
  then posts `false` over the stored value on the next Save — silently turning the feature off. That
  hit `telegram_local_api`, `telegram_api_base/_id/_hash` and `llm_flash_attn` (read at runtime,
  never declared). `tests/test_admin_settings_coverage.py` fails if it happens again, and also
  asserts `id` == `name` (hydration reads the id, Save reads the name).
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

**Uptime monitoring** (`app/services/uptime_service.py`, Admin → Nodes → "Uptime Monitoring",
Discover → Server Stats → **Uptime** tab): HTTP monitors with heartbeats, response time and 24h/30d
uptime, alerting on up→down / down→up over Telegram and NIP-17 DMs. All state is ONE operator-signed
kind-30078 doc (`pcai:kv:uptime`) — no SQL table. The checks run in the WORKER (sole writer); the app
process only READS the doc for the public `/client/uptime` endpoint. **Gotcha:** it reads with
`nostr_store.get_doc(..., strict=True)` and refuses to persist unless the restore succeeded —
`_ws_query` otherwise returns `[]` for BOTH "no document" and "relay unreachable", and writing on the
strength of that empty read replaces the whole history (the same replaceable-doc wipe that took out a
drive's `pcai:files-index`; `scripts/restore_files_index.py` is the recovery for that one).

## Notable features

- **Music generation** (`musicgeni` command; `app/services/music_local.py` + `music_service.py` +
  `music_factory.py`): text-to-song with **ACE-Step 1.5, NATIVE in-process** — same as video gen, on
  the app's own venv/torch/GPU lock. There is no `acestep.service`, no second venv, no HTTP hop
  (`Dockerfile.acestep` is retired). ACE-Step is **not on PyPI**, so its SOURCE is cloned and
  installed `--no-deps` by `./install.sh --music` (its pyproject pins CUDA torch + gradio, which
  would wreck a torch-XPU/ROCm box); its real inference deps are in `requirements.txt`. It loads
  through upstream's **`AceStepHandler`**, NOT diffusers' `AceStepPipeline` — that class exists, but
  `from_pretrained` wants a `model_index.json` no published ACE-Step repo carries, so it 404s. That
  404 is what once justified the sidecar; the weights load fine through the handler, which is the
  same code the sidecar ran. `music_factory` mirrors `image_factory`: round-robin LB over other
  nodes' `/api/generate-music`, and the local path takes the shared `GPUResourceLock` +
  `vram_manager.prepare_for_music()` (one GPU task at a time, swap LLM/image out).
  **Gotchas, all of which failed SILENTLY once:** (1) `music_local.is_available()` gates
  native-vs-legacy-HTTP and must probe **`acestep`**, not diffusers — probing the wrong package sent
  a node to a sidecar that no longer exists, and on a `video_free_music` node that means
  `_ensure_music_server` polls a dead port for **90s synchronously on the single uvicorn worker**,
  per song. (2) Duration comes from **`music_default_duration`** (the key Admin → Music writes) —
  a private `music_duration` read silently pinned every song to the fallback length.
  (3) `AceStepHandler` is a plain object with **no `.to()`**; unload must drop
  `model`/`vae`/`text_encoder`/`mlx_*`/`silence_latent` explicitly, or the VRAM swap frees nothing
  and leaves ~6.3GB resident on the shared 12/16GB GPUs. Covered by `tests/test_music_native.py`.
  (4) **`transformers<5` is required, not preferred.** The Dockerfile always pinned it; nothing
  pinned it for a BARE-METAL install, so a node drifted to 5.14.1 on an unrelated `pip install` and
  ACE-Step (a `trust_remote_code` custom-code model) stopped loading with *"Tensor.item() cannot be
  called on meta tensors"* for both sdpa and eager — same repo, same checkpoint, same commit as the
  working node. Now pinned in `requirements.txt`.
  (5) **`torchaudio.save` routes through `torchcodec`** on torchaudio ≥2.9, and torchcodec is in
  neither `requirements.txt` nor the Dockerfile. ACE-Step's `AudioSaver` calls it for the mp3 temp
  WAV and the wav/flac paths, so on a STOCK checkout every song dies at the final save
  (*"TorchCodec is required for save_with_torchcodec"*) **after** all the GPU work. This hid for
  weeks because ONE node's ACE-Step working tree had been hand-edited to call soundfile — untracked,
  in no repo/installer/image — so that box looked fine while every fresh clone and Docker build was
  broken. `music_local._install_torchaudio_save_shim()` now re-points `torchaudio.save` at
  `soundfile` (already a hard dep) before acestep is imported; **do not "fix" this by patching the
  ACE-Step checkout again.**
  **Duration/steps/format resolve on the REQUESTING node** and travel explicitly to whoever
  generates — settings are per-node, so forwarding `None` made one `musicgeni` yield 4 minutes
  locally and 60s whenever the LB picked the other node.
  `music_api_base` still forces the old HTTP path for a node that really has a remote server.
  **Output is a branded MP4**, not raw audio:
  `media_service.make_music_video` puts the song over a generic PosterChan background
  (`render_music_background`) then appends the `append_outro` end-card ("watermark"); result type
  `generated_video` (falls back to `generated_audio` if ffmpeg is missing). **Vocals** need lyrics,
  so with no `| lyrics` the LLM auto-writes them (`_music_write_lyrics`); `instrumental` skips that.
  Web UI + Telegram only (NOT the fedi bots — abuse surface). The legacy REST client
  (`music_service.generate_once`, only reachable via an explicit remote `music_api_base`) keeps its
  own gotchas: `/query_result` field is **`task_id_list`** (not `task_ids`), and its `result` is a
  **JSON-encoded string** whose items carry `file: "/v1/audio?path=..."`. Deployed: BOTH nas.lan
  (RTX 3060, CUDA) and the Arc (server1, A770 XPU) generate music in-process (measured on the Arc:
  load 6.9s, a 12s song in 14.3s, unload reclaims 100% of the 6.5GB).
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
- **Live-stream bitrate clamp** (`stream_service._write_clamp_script` + the `stream_clamp_*` settings,
  Admin → Live → OBS Streaming): MediaMTX is a pure remux, so without this whatever OBS sends is what
  **every viewer downloads** — one 1080p60/6 Mbps streamer costs 6 Mbps of upload *per viewer*. The clamp
  re-encodes each live stream to a ceiling (default 720p30 @ 1500k) and viewers are served ONLY that.
  **ON by default.** MediaMTX itself supervises the transcode via `runOnReady` (start/stop/restart for
  free — no Python supervisor); source `<token>` in, `<token>_clamped` out; the HLS proxy swaps in the
  clamped path per request (`_upstream_path`) and falls back to the source whenever the clamp isn't up, so
  a missing ffmpeg degrades to "unclamped" rather than "broken". Encoder autodetect is shared with offline
  video (`media_service._video_encoder_candidates`) and runs on the GPU's **media engine**, which is separate
  silicon from the compute cores — it does NOT contend with LLM/image/music/video generation, and so
  deliberately does NOT take `GPUResourceLock` (a 3-hour stream would hold it for 3 hours).
  Four gotchas, all measured against MediaMTX v1.19.2, not guessed:
  (1) **Never authorize the clamp's publish by IP** — MediaMTX reports a *LAN* address for a connection made
  to a `127.0.0.1`-bound listener, so a loopback check denies every clamp and viewers silently get the
  unclamped source. The RTSP URL **query** IS forwarded to the auth hook, so that's the gate
  (`stream_service.clamp_secret`, derived from `stream_auth_secret`).
  (2) `rtspTransports: [tcp]` is **required** — plain `rtsp: yes` also opens UDP :8000/:8001 on ALL
  interfaces, two public ports we never use.
  (3) Clamped paths get their **own** regex path entry (no `runOnReady` → no infinite clamp-the-clamp, no
  `record` → VODs stay the full-quality source, no `runOnNotReady` → can't end a stream by the wrong name).
  (4) RTSP readers must be **excluded from the viewer count** (`stream_viewers`) — the clamp is a reader of
  the source path, so counting it reports "1 viewer" on every stream nobody is watching.
  (5) The scale filter caps the **short** side, not the height — that's what makes 720p mean 720p in both
  orientations. A plain `scale=-2:min(720,ih)` squeezes a portrait 1080x1920 phone stream to **406x720**,
  which saves nothing (the bitrate ceiling already bounds bandwidth) and just looks bad. Covered by
  `tests/test_stream_clamp.py`, which runs the real filter through ffmpeg and checks actual pixel
  dimensions — the string-only assertion passed while this was wrong.
  (6) The bitrate must be a **ceiling, not a target**, and each encoder spells that differently — VAAPI
  `-rc_mode VBR`, NVENC `-rc vbr -cq N -b:v 0`, x264 `-crf N -maxrate`, all with `bufsize = 2x` (a 1x
  buffer is CBR and pads again). Under ffmpeg's DEFAULT rate control a plain `-b:v 1500k` pads every
  stream UP to 1500k: a 125 kbps phone source measured **1441 kbps out, an 11.5x inflation** — the exact
  opposite of the feature's purpose, worst on the weakest connections. The wrong spelling silently
  reverts to padding instead of erroring, which is why the tests assert the exact flags.
  (7) The `runOnReady` encoder must be picked by **probing**, never by "the transcode died quickly so the
  encoder must be broken". A WHIP/phone publisher renegotiates a second after go-live, which kills the
  source and looks identical to encoder failure — that demoted a working GPU stream to libx264 (46% of a
  core) for its whole duration in production. `clamp.sh:hw_ok` probes with the REAL argument set (15ms).
- **Talking pictures** (`talk` command; `app/services/effects_service/talk.py`): attach a face, type a
  line, get an MP4 of that face lip-syncing it. Speech is the existing edge-tts (`tts_service`); the
  MOUTH is a **CPU puppet warp**, not Wav2Lip/SadTalker — numpy+Pillow only, so it is identical on
  CUDA/Arc/ROCm/no-GPU, it **never takes `GPUResourceLock`**, and it works on DRAWINGS (neural
  lip-sync smears on flat art). It still queues like everything else: the Meme Builder path is
  `/meme/apply-effect`, so it inherits `_meme_slot()`, the per-user cooldown and `_meme_lb_forward`
  overflow; chat/Telegram use the ordinary `execute_command` path like `compress`. Wired as a
  **`MEME_LAYER_TOOL`, deliberately NOT an effect** — every effect reads its argument as motion
  MODIFIERS, so `talk hello there` in an effect set is two unknown modifiers. Gotchas: (1) the jaw's
  mask must be cropped at the SOURCE box so the alpha TRAVELS with the pixels — read at the
  destination, the jaw repaints its own footprint and covers the cavity it just opened (symptom: a
  mouth that darkens and never opens); (2) the cavity starts AT the lip seam, never above it, or its
  tooth strip lands on the upper lip as a grey smear; (3) SCRFD's 5 keypoints put the "mouth corners"
  at NOSTRIL height, so this uses the **106-point** landmarks — whose index table is measured, not
  documented (lips 52-71, contour 0-32); (4) faces are ranked by MOUTH width, not box area, which on a
  group shot is the only stable key. Also fixed here: frame PNGs are written at `compress_level=1`
  (167ms → 38ms each; the encode of throwaway temp files dominated EVERY frame-based effect), and
  `frames_to_video` consumes a generator lazily. See `docs/TALK.md`; `tests/test_talk_lipsync.py`.
- **Remote node management** (`app/services/node_service.py`, `node` command): run OS commands
  on SSH-reachable nodes (or `local`), agentic mode, long-running **background jobs**
  (start → job id → result posted back to the originating channel). Config in Admin → Nodes
  (`node_exec_*`). Output: tail inline, full output (≤1 MB) as a `.txt`. **Intentionally
  unrestricted RCE** — gated by enable flag + user allowlist + admins, fully logged. The
  **system-health report** (`logs_scheduler`, see Schedulers) reuses `run_agent` over these same
  nodes, so it needs `node_exec_enabled`.
- **Social notification relay** (`app/services/social_notifications_service.py`): poller
  forwards Pleroma notifications to a user's Telegram; replying to a forwarded
  message posts back to the platform (`SocialReplyMap` maps Telegram msg → target). Per-user
  toggle (User Settings → Telegram) + global kill-switch (default on).
- **Fediverse ↔ Nostr bridge** — three worker services, all sharing `fedi_normalize.py`:
  - **`fedi_nostr_bridge_service.py`** (fedi → Nostr): mirrors a Pleroma timeline onto
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
