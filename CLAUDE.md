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

**A deploy pulls EVERY node, even when it restarts nothing.** The pull is free; only the restart
costs an outage, and `scripts/deploy_targets.py` decides that separately. Skipping `sync.sh` for a
UI-only change and hand-pulling router.lan left **nas.lan 3 commits behind**, running old code with
nothing in any log to say so. `sync.sh` now pulls both nodes, waits on the GPU **only** if something
is actually restarting, and ends by verifying local/origin/github/nas/router are all on the commit —
exiting **1** on drift rather than reporting a green deploy. Guarded by
`tests/test_sync_deploy_flow.py`.

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

**IF THE CLIENT ALREADY HOLDS WHAT THE VIEW IS ABOUT, PAINT THAT BEFORE THE FIRST NETWORK AWAIT.**
The Store is a local relay and every screen paints into one shared `#feed`, so the failure shape
repeats: `feed.innerHTML='<div class="spinner">'`, a serial chain of awaits, and the real render at
the very bottom. Nothing logs, nothing breaks, and the user gets a blank screen for as long as the
radio takes. It bit a POST OPENED FROM A NOTIFICATION — `renderThread` waited on the socket, fetched
the event, walked the ancestors, fetched the root and ran up to four rounds of reply expansion (each
retrying an incomplete answer) before painting a pixel, while the event sat in the Store the whole
time *because the notification was built from it* ("it's like all the content is empty" → "takes
forever to load but it does") — and a PROFILE, which additionally retried an empty notes query twice
with backoff. Both now paint from cache first (`_paintThreadHead`, `renderProfileView`'s `_cached`
branch) and refresh behind it (`_patchProfileHeader`). **The COLD paths are deliberately unchanged**:
with nothing cached, an early paint is a header reading "anon" that rewrites itself, or a thread
claiming a reply count it has not counted — an empty answer dressed up as an answer, which is worse
than a spinner. So the split is on *having something real to show*, never on a timeout, and the early
paint states nothing it has not checked. `tests/client/test_cache_first_paint.py`.

**A VIEW THAT QUERIES ON ENTRY MUST WAIT FOR A SOCKET THAT CAN ANSWER — `await Relay.ready()`.** A REQ
written to a CONNECTING socket is silently dropped (`relay.js _send`), and the moment a view is most
likely to be opened against one is right after somebody logs in. `renderProfileView` and `flushEvents`
already waited; **Trending did not**, so "logged in, nothing under Trending" — and it STAYED nothing,
because an empty answer widened the window and a few widenings later `exhausted` latched and the view
gave up for the rest of the visit. The second half of the fix is the same `complete` rule the timeline
uses: a query that no relay EOSE'd is "the relays never spoke", never "nothing is trending", so it must
not spend a window or latch anything — `_tr.unreachable` says so on screen with a retry.

**ONE BAD EVENT MUST NOT COST A SCREEN, and `noteCard`'s try/catch was never enough.** It only ever
covered kind 1: a poll (1068), an article (30023), a community (34550), a channel (40) or a webxdc
1063 threw straight out of `noteHtml` and out of whatever was building the list. On a PROFILE that is
the whole view — `listFor('notes')` maps the author's events and its result is assigned to
`#prof-list`, so a throw meant the list was never filled AND every binding after it (the tabs, ⋯,
"Copy npub", the follow stats) was never made. That reads as three unrelated bugs — "no posts", "the
hamburger menu isn't showing", "copying the npub does nothing" — with nothing on screen and nothing
in any log, because the header above it rendered perfectly. The guard is in `noteHtml` now (every
kind), plus `fillList`. **The clipboard is `copyValue()`, never `navigator.clipboard` directly**: it
works in a browser and in NEITHER shell (the APK's WebView refuses it, and so does the desktop's
`app://` origin), and ~30 call sites were written `navigator.clipboard.writeText(x); toast('copied')`
— no await, no catch, so the promise rejected into nothing and the toast lied. `_copyFrom(el)` stays
separate for INPUTS (it copies from the real visible field and unmasks a password for the instant of
the copy). The password manager's `copy()` in vault.js tries the native bridge first and only arms
its 45s auto-clear on the web path, since clearing requires reading the clipboard back.
**"↩ REPLYING TO …" with nothing under it** (the Android "ghost timeline") is the same family:
`feedNoteHtml` builds the CARD first and never wraps a label around one that does not exist, and
`_healGhostPairs` sweeps after every draw — the reconcile matches on `data-key` and REUSES cards, so
a pair that came out wrong stays wrong for the life of the page. It distinguishes MISSING (no
`<article>` → rebuild from the Store) from BLANK (an `<article>` of zero height → kick a reflow) and
counts both in `PC.ghostStats()`, so the next report says which. `scripts/check_timeline_ghosts.py`
plants one against the live client and asserts it heals.
**BUT THE ONE THAT WAS ACTUALLY HAPPENING ON RESUME WAS A THIRD KIND, AND BOTH OF THOSE PROBES ARE
BLIND TO IT: the card is present, at full height, and TRANSPARENT.** `body.anim-off` (set while
backgrounded, to idle the GPU) was `animation-play-state: paused`, and `.note`'s entry animation is
`noteRise … both`, which STARTS at `opacity:0` — so every card drawn while the class was on was
frozen at its first keyframe for as long as the class stayed on. `.reply-ctx` has no animation, so
the label rendered perfectly above an invisible post: "↩ REPLYING TO alice" all the way down, plain
posts simply absent, nothing in any log, and the ghost sweep reporting success because the card is
there and 46px tall. **The local relay and the cache-first paint were working the whole time — the
right posts were being painted invisible.** Three changes: (1) `anim-off` now sets `animation: none`,
not `paused` — a disabled animation always resolves to the element's resting style, so it cannot
strand content, where a frozen one holds whatever keyframe it reached; (2) the class is owned by
`_animOff`, called from `_tlBackground`/`_tlForeground`, so the NATIVE resume signal releases it too
— it was armed and released from `visibilitychange` alone, which on Android arrives late or is
coalesced away, and the resume path DRAWS inside that gap (Capacitor's `resume` fires from the
Activity, which was never frozen, so `_tlForeground` re-subscribes and `_resumeRelay` repaints while
the WebView still says hidden; "nothing loads for a while" is the delayed event finally landing);
(3) `_healGhostPairs` clears a stale `anim-off` on a visible page and counts it as `frozen`, ahead of
the no-replies early return, since a frozen timeline need not contain a reply.
`tests/client/test_anim_off_never_hides_content.py` measures OPACITY against the real stylesheet, and
re-runs the pre-fix rule to prove the check can fail.

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

**The board's FACTS are measured, not retold — never let a model back into that path.** Python owning
the icons/layout/status was not enough: the numbers still crossed two LLM hops (agent prose → the
board model), and both invent. Real reports claimed a `[3/3] [UUU]` array was *"degraded (disk 4
failed)"* (with a 🟢 beside the word "degraded" — status and detail are chosen independently, so they
can contradict), `2048M` of swap on a host with none, a `/raid` mount that doesn't exist, and *"no
RAID array"* over a healthy one, while silently dropping a drive from the SMART list. So
`_HEALTH_SHELL` now runs on **every** node and `_parse_probe` parses df/smartctl/mdstat/zpool/
systemctl/free/journal in Python; `_render_board(raw, probe)` overrides the model row-for-row. The
agent still runs — `errors` keeps its wording (naming the noisy source is a real language task) with
the measured counts appended, and a model `red` there survives, because the probe only counts lines
and is a **floor** on severity, never a ceiling. Two rows deliberately do NOT override: `raid` when
the probe found nothing (megaraid/btrfs are invisible to mdstat and zpool, so that means "no
evidence", not "no array"), and any row the probe couldn't read.
**The recurring failure mode is a false 🟢, and it is always a command that did not RUN**: `sudo -n
journalctl` without the sudoers rule exits nonzero having printed nothing — identical to a clean host
— and `${f:-none failed}` reports healthy systemd when systemctl never reached the bus. Every such
leg emits an explicit `probe-error:` marker from its **own** exit status (`rc=$?` after a *pipeline*
reads `head`'s status, i.e. always 0 — `tests/test_logs_scheduler.py` runs the real script with
stubbed `sudo`/`systemctl` because no parser test can catch that). Do **not** replace the markers
with output-sniffing for "permission denied": that string is ordinary journal *content*. `head`
limits are generous for the same reason — mdstat lists arrays newest-first, so a tight cap drops the
oldest (usually the data) array and reports the rest as clean.

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
- **Music player: the APK's media controls + home-screen widget**
  (`mobile/android/.../music/` = `MusicService` + `MusicPlugin` + `MusicWidget`; JS in `app.js`'s
  `MusicPlayer._nativeInit/_nativePush`): lock screen, notification shade, headset/car buttons and a
  4x1 widget, for the encrypted Music library the client plays.
  **Why a native half exists at all:** the player already speaks `navigator.mediaSession`, and in a
  BROWSER that IS the whole job — Chrome turns those calls into the media notification. **A WebView
  accepts every one of them and shows nothing**: that surface lives in Chrome, not in the WebView. So
  in the APK the music played with no controls anywhere outside the app. The audio STAYS in the
  WebView (a track is an encrypted blob only the client can decrypt — there is no file for a native
  player to open), so the service owns the *session*, not the sound: JS pushes state, the service
  publishes it, and every press comes back as a `musicTransport` event JS performs on the audio
  element. The foreground service is also what keeps the WebView's render process off the low-memory
  killer while the screen is off.
  **Gotchas, each one silent:** (1) per-second updates go **direct to `MusicService.INSTANCE`**, never
  through `startForegroundService` — Android 12+ refuses a background FGS start, which is precisely
  the screen-off case the lock screen exists for, so the Intent path is only the FIRST start (made
  while the app is on screen). (2) A plugin `stop` must NOT be echoed back to JS while a notification
  **dismissal** must (`ACTION_STOP` vs `ACTION_DISMISS`), or the two chase each other. (3) `close()`
  sets `_nativeOff` because the `pause` event lands AFTER it and its state push would rebuild the
  notification the close just removed. (4) Artwork is drawn from the launcher **Drawable** —
  `BitmapFactory.decodeResource` returns null for the adaptive (XML) icon every phone since API 26
  uses, i.e. it would look fine only on the oldest emulator. (5) Every `PendingIntent` needs
  `FLAG_IMMUTABLE` (Android 12 throws when the notification is built). (6) **Audio focus is
  deliberately NOT requested** — the WebView's own media stack may hold it, and a second request from
  the same app can steal it from the first, at which point Chromium pauses the element the fix exists
  to keep playing. That needs one measurement on a device; `ACTION_AUDIO_BECOMING_NOISY` (pause on
  unplug) is handled, since it cannot break playback either way.
  **A PRESS IS NOT PLAYBACK, and the service is the only thing that can tell them apart.** Every
  button ends in `emit()` — a call into the WebView — and the WebView is the half Android takes away:
  the renderer is killed under memory pressure (`MainActivity.recreate` → a fresh page with an EMPTY
  player) and a backgrounded Activity is destroyed outright, while this foreground service keeps the
  session and the notification exactly as they were. Reported as *"after a while in the car the
  multimedia controls no longer work until I open the app again"*, with nothing in any log, because
  from the service's side the emit SUCCEEDED. Three things now stand between that and a dead button:
  (1) `_nativeInit` is armed **at startup**, not from `ensure()` — it used to run only the first time
  THIS page played something, so a page that came back after a renderer death had nothing subscribed
  to `musicTransport` at all; (2) a press that should make sound goes through **`press()`** (NOT
  `command()` — that name is the notification's PendingIntent builder, and Java ignores return types
  when comparing signatures, so the duplicate stopped the whole module compiling while every regex
  test here stayed green; `tests/…::test_no_two_methods_in_a_file_share_a_signature` now parses the
  declarations), which waits for a RECEIPT and wakes the app with the press attached when none
  arrives — measuring the wake-up too, since a background activity start is **refused silently** from
  Android 10 on; (3) `_resumeOrPlay` falls back to the last track this device played, so a car button
  works on a page holding nothing, with the app still in the background.
  **The receipt is a bare `ack()`, not the state push**, and that distinction is the whole thing: a
  reloaded page holds no track, so `_nativePush` (rightly) sends nothing — used as the receipt it made
  a LIVE client identical to a dead one, waking an app that was already awake and then writing "the
  player stopped responding" over a notification the user was looking at. **Pause is receipt-checked
  too, via `hush()`** — `playing` is only ever written by the client, so a WebView that vanished
  mid-track leaves the service believing a track plays forever, and the notification's one transport
  button then stays a ⏸ whose every press takes the same dead branch. `hush` never `revive()`s (a car
  stereo must not open an app to stop silence) but does `markGone()`, which frees the toggle. The
  widget goes through `fromWidget()` for the same check — it is the surface most often pressed with
  the app closed. The client also heartbeats **while paused** (the state the failure happens in, and
  the one state nothing was ever pushed in). Nothing DECIDES on heartbeat silence — backgrounded
  timers are throttled to ~1/min — only an unanswered press does.
  **Bluetooth autoplay** (`autoplayBluetooth`, opt-in, per device) rides the same path:
  `registerAudioDeviceCallback`, **never** a `BluetoothDevice` broadcast (`ACL_CONNECTED` and the A2DP
  state broadcast both need the `BLUETOOTH_CONNECT` runtime permission on 12+, for a device TYPE that
  is free without it). Gotchas: the callback fires IMMEDIATELY with everything already connected, which
  is the service starting and not a car door (`firstDeviceSweep`); one connection reports several
  devices; and it only works while the player is up, since with it closed there is no session and
  nothing decrypted. A media button arriving cold is started by `MediaButtonReceiver` with
  `startForegroundService`, so that path must `startForeground` or `stopSelf` within ~5s — and its
  KeyEvent is read rather than assumed, because a press arrives as DOWN *and* UP (two wake-ups) and ⏭
  is a media button too. `MusicPlugin.status()` reports what the phone measured (silence, unanswered
  presses, wake-ups, BT connects); Music → "Details" is where it is read. **Those counters are
  `static`**, because the case they exist to explain is the cold one and that path ends in
  `stopSelf()` — read off the instance, the panel answered "nothing has played this session" about
  the very press being investigated.
  `tests/test_android_music_controls.py` guards the wiring (the Gradle build only runs in CI).
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
- **Calendar** (`static/js/client/calendar.js` + `app/routers/calendar.py` + `app/services/caldav*`;
  sidebar → Calendar): a calendar whose events are **encrypted Nostr events**, served to phones and
  desktop calendar apps over **CalDAV** by a Radicale that is MOUNTED INSIDE THIS APP at `/caldav` —
  `radicale` + `a2wsgi` are dependencies, so Docker, `install.sh` and the updater ship it with nothing
  extra to install, and it rides `posterchanai.service` (one port, one process, one certificate). OFF
  by default (`caldav_enabled`, Admin → Tools).
  **What "encrypted" means here is NOT what Notes/Budget mean**: a CalDAV client sends plaintext and
  the server must answer it, so a calendar is encrypted at rest and on the relay with the user's
  server-held storage key — the node CAN read it. That is the price of phone sync, and it is stated
  first in `docs/CALENDAR.md` so nobody assumes otherwise.
  **One event per item** (`pcai:cal:<calendar>:<uid>`, plus `pcai:calmeta:<calendar>`), for Notes'
  reason: a per-calendar document is a read-modify-write of everything on every save and two devices
  editing different events lose one. The storage plugin **subclasses Radicale's `multifilesystem`**
  rather than reimplementing `BaseStorage` — sync tokens, history, locking and etags are the hard
  parts and upstream has them right; the working directory is a CACHE that re-hydrates from the relay
  (verified: `rm -rf caldav-data/collection-root`, restart, the events come back).
  **Gotchas, each of which cost a session:** (1) the auth plugin implements **`_login`, not `login`** —
  Radicale marks `login()` @final, and overriding it imports cleanly then 500s EVERY request; (2)
  nothing may configure `[server]` — `hosts: ""` makes Radicale refuse to build, and since the app
  builds it at import **that took the whole app down**; (3) the mount's `except` must use a logger that
  exists at import time (the first one called an undefined `logger`, so the guard meant to prevent (2)
  raised NameError and every request 502'd); (4) an app-side write calls `storage.forget_user()` or a
  calendar made in the web UI is invisible to a phone until the app restarts; (5) items are stored as
  the client PUT them (a whole VCALENDAR each), so export UNWRAPS them or the file nests calendars;
  (6) import updates by UID rather than duplicating, so a re-import converges.
  **An import stores a RESOURCE, not a component**, and the three ways that differ were each measured
  silent on a real 707-event Radicale export: a `VTIMEZONE` has no UID, so keying on UID dropped every
  one (577 events kept a `TZID` referring to a definition no longer in the file — invalid to a strict
  client, an offset-shifted appointment to a lenient one); components sharing a UID (a master plus its
  `RECURRENCE-ID` overrides) are ONE resource, and stored separately the second write silently
  overwrote the first; and 10 items were `VTODO`s, which keying on `VEVENT` drops. `group_resources()`
  + `wrap_ics(..., timezones=…)` in `caldav_store.py`.
  **Recurrence lives in `static/js/client/ical.js`** — DOM-free, so `tests/test_ical_recurrence.py`
  runs the shipped parser under node against real rules. The grid used to place only `DTSTART`, so 59
  of those 707 events (every weekly delivery, every birthday) drew exactly once and the calendar
  looked empty. Expansion JUMPS to the period containing the window rather than stepping from
  `DTSTART` (a daily series from 2011 is ~5500 iterations per repaint); `COUNT` is the exception and
  is bounded by itself. Editing a recurring event re-emits the rule, its `EXDATE`s and its overrides
  VERBATIM — rebuilding from the form flattens a weekly series into one appointment the first time
  someone fixes a typo. An imported calendar is a HISTORY (1 of those 707 events fell in the month it
  was imported in), so the view jumps to a month with content and the import shows progress; an empty
  grid after a successful import is indistinguishable from a failed one.
  See `docs/CALENDAR.md`; `tests/test_calendar.py` + `scripts/check_calendar_mobile.py`.
- **Contacts** (`static/js/client/contacts.js` + `vcard.js` + `app/routers/contacts.py`; sidebar →
  Contacts): CardDAV, served by the SAME Radicale mount and the same account/password/URL as the
  calendar — one identity per user, not two. An addressbook is just a collection whose metadata
  carries `kind: VADDRESSBOOK`, stored in the same `pcai:cal:`/`pcai:calmeta:` namespace, so hydration
  stays a single relay pass. Same encryption trade as the calendar (this node CAN read it — a CardDAV
  client sends plaintext), stated first in `docs/CONTACTS.md`.
  **Cards are stored as the owner's phone wrote them, and that is the design.** A real book carries
  base64 `PHOTO`s, Apple-style grouped properties (`item1.EMAIL` labelled by `item1.X-ABLABEL`), a
  foreign `PRODID` and `X-*` fields; this app has fields for ~8 properties, so `vcard.js` rewrites
  only what it manages and carries every other line through untouched **with its group prefix**, or a
  saved phone number silently strips the photo everywhere else.
  **THE PHONE-BOOK RECONCILE IS A KEEP-SET, so a SHORT list is a delete order.** The APK's native
  sync (`mobile/android/.../contacts/`, switch in ⋯ → Addressbooks) emptied a real phone book twice:
  the guards then in place only ever asked "is the list EMPTY?", and it never was. Four things now
  stand between a bad read and somebody's dialer, each with a test verified to fail without it —
  (a) a per-book fetch failure is no longer swallowed into `[]` (it keeps the last good cards and
  marks the load PARTIAL; `loadedOk` is about history and cannot see it, because a load HAD
  completed); (b) `/api/contacts/cards` and `/books` read the relay **strictly**, so "I could not
  ask" is a 503 rather than a 200 carrying part of the address book (`list_docs` returns what it
  collected when a read times out part-way); (c) the client refuses a reconcile that would delete
  more than it keeps; and (d) `ContactSyncPlugin.commit()` applies the SAME rule to the ROWS and
  answers `refused:true` — a plugin must not trust its caller, and the caller is what got it wrong.
  `force:true` is the only way past (d) and nothing passes it; the deliberate way to rebuild a
  handset's copy is the switch, off and on. Also: an EMPTY `owner` is "I don't know", never
  "somebody else" — read as a mismatch it wiped the phone book and recorded `""`, so the next sweep
  wrote it all back (written, gone, written, gone). And the edit schema's kinds are AOSP's exact
  spellings — `structuredPostal`, never `postal`, which throws `DefinitionException` and discards the
  WHOLE `<EditSchema>`, leaving the account read-only *in the Contacts app's editor*. That schema is
  read by the CONTACTS APP (`ExternalAccountType`), never by ContactsProvider2 — it cannot affect
  what this app WRITES, which goes to the provider through `applyBatch` as a sync adapter. Rows were
  landing on the phone while the spelling was wrong, so do not reach for it to explain a write that
  did not happen.
  **THEN THE GUARDS STOPPED IT SYNCING AT ALL, and that was the same bug with the sign flipped.**
  Refusing the whole sweep on a partial load meant NOTHING reached the phone — for ever, if a book
  fails reliably — reported as *"nothing going to android contacts app"*, with no error on screen (a
  per-book failure keeps the last good cards, so the address book still looks complete) and nothing
  in any log. **A partial load suppresses DELETION, never INSERTION**: `put` runs, `commit` is
  skipped and says which, and the PULL is skipped too — but for its own reason, that a card whose
  book failed is missing from `heldCards()` and the phone's row for it is stored again as a
  phone-created contact (one person, two cards). The push signature carries the mode, so a partial
  sweep does not tell the next whole one there is nothing left to do.
  **AND THE SWEEP REPORTS WHAT IT MEASURED.** Four APK builds were spent guessing because there is no
  device here and the failure REPORTS SUCCESS: `applyBatch` does not throw for an operation that
  changes nothing. `put()` now answers with the row count under our account BEFORE and AFTER, re-read
  from ContactsContract, plus the `ContentProviderResult` tally, and ⋯ → Addressbooks shows the line
  **on success too** (`cards=90 sent=90 landed=0 phone 0→0 noop=180/180` is the shape that was
  invisible). Two conditions that were silent now say so once and are NOT signed off in `_pushSig`,
  so they retry: a phone that could not create the account (`begin`/`pull` used to `call.reject`,
  which lands in a client `catch` and looks exactly like nothing happening), and a write that was
  accepted and stored nothing.
  `tests/test_android_contact_sync.py` (javac + `java` RUN the pure guards) +
  `tests/client/test_contacts_phonebook_guard.py` (the shipped contacts.js against a stub phone).
  **Gotchas:** (1) a collection with no `kind` must default to a CALENDAR — anything else hides every
  calendar that existed before addressbooks did, data intact and nothing logged; (2) the reconcile
  picks BOTH the Radicale tag and the file extension from the kind, and the delete half matters as
  much as the write half — matching `.ics` unconditionally meant an addressbook's `.vcf` files were
  never reconciled, so a contact deleted in the web UI stayed on the phone and could be edited back
  into existence; (3) a `.vcf` has no envelope (it IS a concatenation), unlike iCalendar.
  **"DOES THE ACCOUNT EXIST" IS ANSWERED BY `getAccountsByType()`, NEVER BY WHAT AN ATTEMPT RETURNED
  OR THREW** — and that one line is why the phone half wrote **zero** contacts for its whole life.
  `ensureAccount` took the account off the sync framework with `ContentResolver.setIsSyncable` /
  `setSyncAutomatically`, which **require `WRITE_SYNC_SETTINGS` and throw `SecurityException`
  without it** (undeclared until now); the catch turned that into "no account", `begin()` answered
  `account:false`, and every sweep aborted before writing. `addAccountExplicitly` is the same trap
  one line up: **`false` means "already there"**, not failure. Both attempts are best-effort in their
  own guard now and the verdict is a fresh `hasAccount()`. The bug was found in ONE round only
  because the panel printed `no contacts account on this phone` directly above `account=yes` — so
  every surface that reports `account` reads the SAME measurement (`ContactSyncPlugin.haveAccount`),
  which is what keeps that contradiction meaningful. Android-only: a `sync.sh` deploy ships the JS
  half, the fix itself needs the **CI APK build**.
  See `docs/CONTACTS.md`; `tests/test_contacts.py` + `tests/test_vcard.py` +
  `scripts/check_contacts_mobile.py` (the generic and calendar checks never open this screen).
- **Web Search** (`static/js/client/websearch.js` + `app/routers/websearch.py`; sidebar → Web Search):
  a front end to this node's SearXNG, plus Save to Notes / Share / summarize a link / an **AI overview**
  of the results with numbered citations. The whole search lives in MODULE state, not the DOM —
  `#feed` is shared by every view and app.js blanks it on entry, so leaving and returning repaints
  query/filters/results/overview/scroll with no refetch. A result opens IN the app as the PAGE ITSELF — an iframe of
  `/api/websearch/page`, which re-serves the fetched HTML from our origin (most sites refuse to be
  framed) with every script/handler/form stripped and a no-`script-src` CSP, CSS+images kept and
  absolutised; **Reader** toggles to extracted text, `← Results` returns to the exact offset, and
  `PCWebSearch.readerOpen()` lets the Android back button close it before leaving the view. `/search?q=`
  + `/opensearch.xml` make the node addable as the browser's own search engine.
  **Where a node searches is now ONE resolution order** (`search_service.resolve_searxng_url`), shared
  by the AI's web-search tool, the news digests, the bots (`bot_manager_service` injects the resolved
  `SEARXNG_URL`) and this screen: the **"Web search enabled"** switch → Admin → Tools → the SearXNG
  **bundled with this node** → a public instance. That last one is a fallback, not a plan — measured,
  it 429s a server on both its JSON and HTML endpoints. It replaced a hardcoded `search.poster.place`,
  so every node that never filled the field in was silently searching through one deployment's box.
  The bundled instance is `posterchanai-searxng.service` (a systemd unit like every other service
  here, `--network host`, branded + dark-themed, on 127.0.0.1:8899), installed by DEFAULT on a fresh
  install, re-run on upgrade, and available as `./install.sh --searxng`; compose gets it from every AI
  backend profile.
  **Gotchas, each of which fails silently:** (1) SearXNG ships its **JSON API off**, and with it off
  every search here is a 403 with an HTML body that every caller reads as "no results" —
  `search.formats: [html, json]` is the load-bearing line, and it must come from a settings FILE:
  `secret_key` is the ONLY setting this image maps to an env var, so a `SEARXNG_SEARCH_FORMATS=…`
  compose service configures nothing at all. Both paths generate from `docker/searxng/settings.yml`.
  (2) The bundled instance's ENGINE requests CAN go through the proxy's **fallback listener**
  (`proxy_fallback_port`, default 8119: Tor1 → Tor2 → direct) — never the main `:8118`, which is
  Tor-only because torrents share it — but it is **off by default**: MEASURED, the default engines
  answer a Tor exit with "too many requests"/"access denied"/CAPTCHA and SearXNG suspends them for an
  hour, giving 0 results vs 25 direct. `SEARXNG_TOR=1` opts in (and needs `request_timeout: 12.0`;
  the 3s default times out over Tor on its own). Never send torrent traffic to 8119. That is also why the container is `--network host`: from a bridge
  network there is nothing at the proxy's loopback address. (3) **Only LOOPBACK being exempt from the
  Tor transport is not enough** — Tor cannot route RFC1918 and the proxy returns a 502 *response*,
  which `afallback_transport` never retries (it falls back on connect errors only), so an ordinary LAN
  instance (`http://192.168.0.85:8888`) would fail every request; `_is_local_base` resolves the host
  and treats private/link-local/`.lan`/`.local` as direct. (4) The probe demands 200 on `/healthz` AND
  JSON from `/config`: `status < 500` let an unrelated listener's 404 pass and the node adopted it as
  its search backend. (5) `searxng_enabled` reads a BLANK stored value as ON — `get_bool` treats `""`
  as false, and a blank row would turn search off node-wide with nothing said; it is also checked
  FIRST in the bots' resolver, or the app would stop searching while every bot carried on. (6) The
  bots' copy of the resolved URL is **sticky for the process**: it feeds `NO_PROXY` and therefore
  `_spec_sig`, so a flapping 5-minute probe would restart every running bot, mid-stream, on a timer —
  and only a PRIVATE host may join `NO_PROXY`, since the public fallback landing there would send
  every bot search direct from the node's real IP. (7) `/overview` re-runs the search server-side
  rather than trusting client-supplied results, and **`fetch_url_content` re-checks the SSRF guard on
  every REDIRECT HOP** (it followed redirects with only the first URL validated — one 302 reached
  169.254.169.254 and the body came back to the caller and the model). (8) 8888 was the obvious port
  for the bundled instance and is MediaMTX's HLS port on every streaming node. (9) The bind address
  comes from **`GRANIAN_HOST`** — the image serves through granian, which ignores both
  `SEARXNG_BIND_ADDRESS` and `server.bind_address`; measured, the first version listened on `*:8899`
  with the limiter off. (10) The installer re-decides the outgoing-proxy block on EVERY run: on a
  fresh install it probes before the app's proxy exists, so a frozen answer pins the node to direct
  engine requests forever (and the container chowns its config dir, so the rewrite needs it back).
  (11) A stored `search.poster.place` — the retired hardcoded default, seeded on older installs — is
  treated as "not configured" rather than honoured, since the box behind it is gone.
  See `docs/WEBSEARCH.md`; `tests/test_websearch.py` + `scripts/check_websearch_mobile.py` (the
  generic `check_client_mobile.py` never opens this screen).
- **Folder Sync** (`static/js/client/foldersync.js` + `syncrun.js` + `sync.js`, `desktop/fsbridge.js` /
  `FolderSyncPlugin.java`; sidebar → Folder Sync): Documents/Pictures kept in step across devices in
  encrypted Blossom. **Desktop + Android only** — a browser has no filesystem, and Firefox has no File
  System Access API at all. The split is load-bearing: everything that can get the ANSWER wrong is
  pure and tested (`diff()`'s three-way merge, the battery policy, the exclusion matcher), everything
  that can destroy a FILE is a thin platform adapter.
  **THE PAIR KEY.** `f.id` is the PLATFORM's handle for a directory — random hex on desktop, a SAF
  tree URI on Android — and is device-local by construction. Keying the manifest on it meant every
  device wrote and read a DIFFERENT document, so each synced happily with itself and **cross-device
  sync could not work at all**; every test passed because they all inject the "remote" snapshot by
  hand, which is the one thing the bug prevents two devices from sharing. It is keyed on `f.key`, the
  NAME the user gives the pair ("Documents"), the same on every device; the local path stays local,
  and that IS the per-device mapping. `tests/client/two_device_sim.js` now runs two (and once three)
  independent devices — own filesystem, own `base`, own platform id — against ONE manifest addressed
  the way the server addresses it, so this cannot come back silently.
  **Rules that cost data if changed**, each with a test verified to bite: THREE snapshots (`base` per
  PATH is what tells "new here" from "deleted there"); a conflict renames the local copy BEFORE
  writing the incoming one; delete loses to edit both ways; identical bytes arriving independently is
  NOT a conflict; `base` advances PER FILE and only on success; excluding a folder drops it from ALL
  THREE snapshots so it is never DELETED elsewhere; nothing is deleted in place (`.pc-trash/<date>/`).
  **And `base` must be cleared under the key it was WRITTEN under** — it moved to the pair key and
  "Stop syncing"'s `removeItem` did not, so re-adding a folder started from an agreement claiming
  files were synced that were no longer on disk, and the engine correctly deleted them everywhere.
  The manifest goes through the SERVER (`/client/sync-manifest`) purely for the collapse guard — the
  same one that saved the drive index — because an empty read written back over a full manifest does
  not lose a setting, it loses the folder. `/client/sync-folders` enumerates the manifests' own
  d-tags so a device that syncs NOTHING can still browse a folder from Files; there is deliberately
  no index document, since an index is a second source of truth one empty read can wipe.
  **THE FEATURE HAD A HARD CEILING AT ~376 FILES, and every symptom of it was "it synced fine, then
  synced everything again".** NIP-44 refuses a plaintext over 65535 bytes and a manifest entry is
  ~174, so past that `store.save()` threw at the LAST step of every sweep: the blobs were all
  uploaded, the manifest was never stored, `base` was never written, and the next sweep read the
  whole folder as new. Measured on a real 15790-file folder. Past 45 KB the paths now go to an
  encrypted Blossom blob with a pointer in the doc (what `FilesIdx` already does) — and `sealed` is
  still set, to an undecryptable marker, because a client older than this reads `sealed`, falls back
  to `doc.paths`, and would take a v2 document as an EMPTY manifest: every file "deleted elsewhere",
  trashed locally and tombstoned for the others. Two more of the same shape: `base` (same size as the
  manifest) was in localStorage under a `catch` that swallowed the quota error — it is in IndexedDB
  now and a failed write is reported; and `base` was persisted ONCE at the end of a sweep, so an
  interrupted sweep resumed from file one — it checkpoints during the sweep now, bounded to ~20
  manifest writes however big the folder is. `tests/client/test_sync_store_scale.py` drives the real
  store at those sizes with the real ceilings stubbed in.
  **An empty `base` must not conflict the whole folder.** Both sides look changed for every path at
  once (re-adding a synced folder, a cleared agreement), and an ordinary sweep does NOT hash — so a
  convergence test written as `L.sha === R.sha` can never fire, and every file becomes a
  "(conflict from …)" copy on every device. It is `same(L, R)`, which falls back to size+mtime.
  **A DEVICE THAT LOSES ITS MAPPING MUST NOT LOOK LIKE AN ACCOUNT THAT SYNCS NOTHING.** The mapping is
  localStorage, so a reinstall or an app update that moves the storage origin takes it — reported as
  "I updated the windows app and my existing Folder sync was no longer there". `/client/sync-folders`
  is what makes that recoverable: anything the ACCOUNT syncs that this device has no directory for is
  offered back under "Synced on your other devices", and re-attaching asks only WHERE, never for the
  name (a different name is a second folder that never meets the first). Nothing is re-uploaded —
  that is the empty-base rule above doing its job. The cache lives in `sync.js` and Files borrows it
  through `PCSync.accountFolders()`, so the same question is not asked twice per visit.
  **Superseded manifest blobs are RELEASED, not expired.** `expire_blob_in` cannot collect them: the
  cleanup sweep filters `keep.is_(False)` and a manifest blob is a `keep` blob. The doc carries a
  one-deep chain (`pathsSha`/`prevSha`, written server-side) and `_release_sync_blob` releases the
  generation behind it — ownership-checked, because the sha comes out of a client-written document.
  **A mass delete has to be able to complete.** The collapse guard refused the save, so the agreement
  was never written and every later sweep re-proposed the same delete and was refused again. The 409
  now carries `old`/`new`, and the sweep passes `removed`: when it accounts for the shrink the store
  re-sends with `force`, otherwise it asks and honours a no.
  **Background sync cannot upload and that is not a bug**: every network step is signed by the user's
  Nostr key, which with Amber/NIP-46 is not on the device — so Android's WorkManager job notices
  changes and notifies, and opening the app is what syncs. See `docs/FOLDER_SYNC.md`.
  **Files → Synced folders can now ADD/RENAME/DELETE, and what it edits is the MANIFEST, never a file**
  (`PCSync.edit` in sync.js; the browser half is `_renderSyncedRoot`). Devices apply it on their next
  sweep through the paths they already use — new entry → download, tombstone → `.pc-trash`, rename →
  both — so a browser that syncs nothing can reorganise a folder and nothing here touches a disk. It
  goes through `store.save` like a sweep, inheriting the re-read+merge, the server collapse guard and
  the `removed` count that lets a deliberate mass delete through. **A delete is a TOMBSTONE, never a
  removed key** — the same record `plan.deleteRemote` writes, so `same()`/`live()`/`gone()` read a web
  edit exactly as they read a device's, and the document can still say a file was deleted rather than
  never existing. What NEITHER shape can do is stop a device whose `base` was cleared (a reinstall,
  "Stop syncing" and back) re-uploading a file it still holds — both sides look changed and diff()
  applies DELETE LOSES TO EDIT on purpose. **That is engine policy and it fires identically for a
  delete made on a device**, which is the point of
  `tests/client/two_device_sim.js::a-web-delete-behaves-exactly-like-a-device-delete`: three arms, one
  outcome, so this screen cannot become a second and subtly different way to delete. A rename is a
  tombstone plus a new entry pointing at the SAME blob (no bytes move, asserted). Dropping a FOLDER on
  that screen is refused on purpose: a half-walked directory tree is a half-created folder on every
  device.
  **Three more rules, each one a review finding with teeth:** (1) a manifest has no directories and a
  filesystem does, so `_blockedBy` refuses a file at a path other entries live under (and vice versa) —
  otherwise every device is asked to write a FILE where it has a DIRECTORY, fails on every sweep for
  ever, and the bad entry draws as ONE file while deleting it covers the whole subtree. (2) The delete
  confirmation's count is read FRESH and travels to `edit.remove` as `expect`, which refuses if it has
  grown: a screen left open while another device fills the folder would otherwise ask about 3 files and
  tombstone 403 — and `removed` accounting for the shrink is exactly what waves that past the server's
  collapse guard. (3) `rename` re-checks its destinations inside `store.save`'s merge (`verify`), not
  only against the manifest it read: the merge is what would otherwise overwrite a path another device
  published seconds earlier. Uploads batch through `edit.uploadMany` (one manifest write per 20 files),
  because each write is a fresh encrypted copy of the WHOLE document — per file, a 50-photo drop moves
  more manifest than photos.
- **The nostr-only Docker image downloads NO model weights.** `DOWNLOAD_MODEL` /
  `DOWNLOAD_DEPTH_MODEL` / `DOWNLOAD_U2NET_MODEL` are `ENV …=1` in the Dockerfile's **shared** final
  stage, so they are on in EVERY image — including `GPU=nostr`, which installs
  `requirements-nostr.txt` and therefore has no llama-cpp, torch, onnxruntime or rembg. A plain
  `docker compose --profile nostr up -d` was starting a background pull of ~**5.9 GB** (5.6 GB GGUF
  + 94 MB depth + 176 MB u2net) that nothing in the container can load. `docker-entrypoint.sh`
  computes **`PC_WANT_MODELS`** once and every pre-fetch is gated on it; it is 0 when `PC_ACCEL=nostr`
  (a BUILD fact, so it holds under a bare `docker run` with none of the compose env) **or** when
  `POSTERCHANAI_NOSTR_ONLY` is on (an AI-capable image deliberately run as a Nostr node — the AI
  surfaces are hidden, so the weights would reach nothing). It says so in the log rather than
  skipping silently. The **admin panel is NOT gated by nostr-only mode**, so its "Download chat
  model" button is still there on such a build: `model_download_service._no_ai_build()` refuses with
  a sentence instead of fetching. The bare-metal `install.sh` nostr path was always clean (it never
  calls `download_model`/`download_depth_model`/`download_u2net_model`).
  `tests/test_docker_nostr_no_models.py` RUNS the entrypoint's download section under bash with a
  stubbed `curl` — a grep-for-the-variable test would pass against a gate wired into two of the
  three blocks — and asserts an AI build still pre-fetches, which is how this guard would otherwise
  "pass" while quietly disabling the turnkey pull for everybody.
- **Desktop mode is ARRANGEABLE** (`static/js/client/os.js`; the windowed desktop, entered from the
  instance logo ≥1024px): icons drag into your own order, one dropped on another makes a named
  folder, right-click renames/takes a folder apart or hides an icon from the desktop (it stays in the
  start menu, which is the way back). The arrangement is ONE kind-30078 doc `d=pcai:desktop`,
  NIP-44-encrypted to the user's own key, so it follows the ACCOUNT across devices and the server
  cannot read which apps you use.
  **The document holds DECISIONS (order, folders, hidden), never the app list** — the list is still
  read from the sidebar every draw, so a feature added to the nav appears on a year-old customised
  desktop for free. The built-in `FOLDERS` (Nostr Games) are a DEFAULT applied only to views the
  document has no opinion about: drag a game out and it stays out, while a game shipped later still
  joins the rest — and merges into a folder the user RENAMED rather than making a second one beside
  it. All of that is `computeLayout()`, kept DOM-free so `tests/test_desktop_layout.py` can run the
  shipped code under node; the drag/drop/hydrate half is `scripts/check_os_desktop.py` (real pointer
  events, a stub relay).
  **Gotchas, each silent:** (1) the doc must be in **both** `_isPinned` (store.js) and `_CARRY_D`
  (app.js) — every private doc here has missed one at least once, and the symptom is the DEFAULT
  desktop drawing, which is indistinguishable from never having arranged one; (2) a write is only
  allowed once a relay has ANSWERED (`_wr`), or an unreachable pool publishes the defaults over the
  real layout (the replaceable-doc wipe); (3) a refused save is ROLLED BACK on screen and said out
  loud, since an icon that moves and moves back on the next reload is worse than one that refuses;
  (4) the in-flight read is shared **per pubkey** — a read that finds nothing retries for ~1.4s,
  which is long enough for the account switcher to run inside it, and handing the new account that
  read meant it finished, saw the pubkey had moved, discarded its result, and nothing read the
  layout again for the whole session; (5) a folder left holding ONE app dissolves, and the survivor
  takes the folder's own place in the order rather than being appended to the end.
- **Mini apps / webxdc** (`static/js/client/webxdc.js` + `zip.js` + `static/webxdc-sandbox/`): a
  `.xdc` (a zip with an `index.html`) attached to a post renders as a Play cartridge — games, polls,
  shared editors — with state synced over Nostr. Ditto's `NOSTR_WEBXDC` draft VERBATIM, so a game
  started in Ditto is playable here: `imeta`/kind-1063 attachment with `m application/x-webxdc` and a
  `webxdc` identifier, moves as kind **4932** (`i` tag = that identifier), and a realtime channel as EPHEMERAL kind
  **20932** (`joinRealtimeChannel`, which is what a continuously-moving game like the Quake III port
  needs — relays forward and store nothing, which is exactly the channel's semantic; it is a relay
  round trip per packet, where Delta Chat's is direct P2P). The identifier, not the
  file or the event, is what makes two people the same game. **Serials are LOCAL** — assigned by
  `(created_at, id)` — which is what lets an append-only log ride a network with no global ordering.
  Kind 4932 must stay out of `_PRUNABLE_KINDS`: a mini app's state IS the sequence, so losing the
  oldest updates makes the game unreplayable, not shorter (`tests/test_relay_prune.py`).
  **WHERE IT RUNS IS THE SECURITY.** Untrusted code on the instance's origin could read the
  localStorage holding the user's key and session, so apps run on **`xdc.<instance>`** — one extra
  `-d` on the certbot cert, no wildcard. Two designs were measured and rejected first, and neither
  should be re-attempted: a subdomain per app (what Ditto does via iframe.diy) needs a WILDCARD cert,
  i.e. DNS-01 and a DNS API token on the web server; a PORT (`:8443`) is a distinct origin needing no
  cert but **does not survive Cloudflare**, which accepts 8443 from the browser and then connects to
  the origin on 443 (proven with a marker header — present direct, absent through the CDN). The
  sandbox origin serves only a loader and a service worker; the app's bytes come from the client,
  which unzipped them, over postMessage — so "no network access" is structural. TWO frames, because a
  worker cannot serve the navigation of the page it needs to ask. Trade of one shared origin: apps can
  read each other's localStorage (keys are namespaced in the bridge — a collision guard, not a
  boundary); `pc_webxdc_wildcard` upgrades a node that has a wildcard. See `docs/WEBXDC.md`.
- **Notes** (`static/js/client/notes.js` + `joplin.js`; sidebar → Notes, ☰ More on mobile): private
  encrypted note taking, offline-first. **ONE kind-30078 event PER NOTE** (`d=pcai:note:<id>`,
  folders `pcai:notefolder:<id>`, both tagged `l=pcai-notes` so the library is one indexed
  subscription), NIP-44-encrypted to the user's OWN key like Budget — so there is no `notes`
  command, nothing on Telegram, and the AI cannot read them. Deliberately NOT one document like
  Budget: a document is a read-modify-write of everything per save (two devices editing different
  notes lose one) and a Joplin library does not fit in one event. No index doc anywhere — an index
  is a second source of truth one empty read can wipe. See `docs/NOTES.md`.
  **Three auto-cleaners had to be taught about it, and each was a silent total loss:**
  (1) the relay's **NIP-40 expiration sweep** is otherwise unconditional, and NIP-37 *recommends*
  `expiration: now+90d` on drafts — so kind 30078 joins `_GIT_KINDS` in `_NEVER_EXPIRE_KINDS`
  (`nostr_relay/store.py`), and ingest DROPS the tag rather than merely not sweeping it, because a
  stored expiration hides the event from every read (`expiration > now` in the query builder) —
  intact on disk and invisible is worse than deleted. (2) Blossom's **age sweep** is driven live by
  `blossom_blob_ttl_days`, so turning it on later retroactively deletes attachments/music/the
  files-index — encrypted-drive uploads now send `X-Keep` → `BlossomBlob.keep`, exempt from that
  **age rule** forever, and `keep` only ever goes False→True (dedup means one blob can be both a
  throwaway and drive content). An **explicit `expires_at` still applies to a keep blob**, and that
  is not a hole: the age rule is a blanket policy nobody set per blob, while a stamp is written one
  blob at a time by code that PROVED those bytes are referenced by nothing (a files-index blob out of
  backup retention, a folder-sync manifest two generations stale). `keep` used to swallow those too,
  which protected nothing and leaked every superseded index and manifest for ever while the code
  looked like it was reclaiming them — measured, 88 blobs carrying a TTL that could never fire. What
  makes it safe is the UPLOAD path: a fresh keep reference clears any expiry it finds, so a stamp can
  only ever mean "still unreferenced" (`tests/test_blossom_keep.py`). (3) the CLIENT cache evicts newest-N by `created_at` in **three**
  places (`_evictMem`, the IDB hydrate, `_pruneIDB`) — right for the firehose, fatal for a document
  only its author can decrypt, since minutes of global-feed reading pushes a library out of a
  3000-event window; `_isPinned` in `store.js` exempts `pcai:note*`. Tests:
  `test_relay_prune.py`, `test_blossom_keep.py`, `test_client_store_pinning.py` (each verified to
  FAIL without its guard).
  **Offline writes need their own queue** — the app's Outbox refuses replaceable kinds on purpose
  (blind replay caused the follows-wipe). Notes queues the signed ciphertext and, on flush,
  DISCARDS anything the library already holds a newer version of. `publish()` rolls its optimistic
  Store save back on failure, so `save()` must re-save or a note typed offline vanishes as you type;
  `scripts/check_notes_mobile.py` asserts exactly that (run it — `check_client_mobile.py` never
  opens this screen).
  **Joplin import** is `joplin.js`, DOM-free so `tests/test_joplin_import.py` can build real `.jex`
  tars with Python and run the shipped parser under node. Input is the **`.jex` export**, never
  Joplin's live `database.sqlite` — the previous attempt (`scripts/migrate_joplin.py`, deleted) did
  that and only ran on the machine Joplin was installed on, broke on schema migrations, and read
  nothing when E2EE was on. The metadata block can only be found by walking **backwards** from the
  last line (a body line like `todo: call the bank` is prose, not metadata); an E2EE export is
  refused loudly rather than importing a wall of blank notes; re-importing UPDATES by Joplin id
  rather than duplicating (imports of thousands of notes get interrupted).
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
- **Pay to stay** (`app/services/paid_retention_service.py` + the `nostr_relay_paid_*`/`_free_retention_days`
  settings, Admin → Nostr Relay): an OPTIONAL paid retention tier for the relay, **off on every node**
  until an admin enables it AND types a free window. Everything a client publishes here
  (`origin='direct'`) is kept forever today; the tier ages out a NON-subscriber's own feed posts after
  `free_retention_days` and a subscriber's after `paid_retention_days` (0 = forever). Accounts here,
  NIP-05 holders, operators and bridged puppets are in the preserve set and are never affected — only
  WoT strangers are. `store._tiered_rules()` is the ONLY thing in the codebase that can delete a
  direct-published event; it returns nothing (so nothing changes) unless the feature is on, a free
  window is set, and the ledger was read this pass. Payment is a **zap of the relay's profile**;
  `verify_receipt` trusts a kind-9735 only because it is signed by the `nostrPubkey` the configured
  `paid_lud16`'s LNURL endpoint advertises — a receipt on our relay proves nothing, any WoT member can
  publish one. Ledger = ONE operator-signed 30078 doc `pcai:kv:paid_retention` (worker writes, relay +
  app read). **Gotchas, each a silent loss:** (1) an unreadable ledger and "nobody subscribed" must not
  look the same — `set_subscribers(pks, ledger_ok=…)` carries it and the tier is skipped entirely when
  False, *including* when the doc doesn't exist, because the alternative deletes what people paid for;
  (2) reads are `strict=True` and a failed read is never written back (replaceable-doc wipe); (3) the
  amount comes from the bolt11 invoice — an unreadable invoice is REFUSED, never replaced by the zap
  request's `amount` tag, which the payer controls; (4) a zap with an `e` tag is a tip for that post,
  not a purchase; (5) the splash-page QR encodes the `nostr:` PROFILE, never `lightning:` — a plain
  wallet payment carries no identity, so a payment QR would take sats and credit nobody. Both prune
  triggers (nightly + Admin "Run auto-clean now") and the dry-run preview refresh tiers + ledger first.
  **The EXISTING auto-clean is untouched and disjoint** — every old rule carries `origin != 'direct'`,
  both new ones `origin = 'direct'` — except that a subscriber is also exempt from the old age prune
  and count cap (`_subscriber_exempt`), or "your posts stay" would silently exclude the copies that
  arrived over the firehose. That exemption treats an unreadable ledger the OPPOSITE way to the tiered
  rules, deliberately: a direct write can be the only copy (fail closed — skip the rule), while a
  synced row is a mirror AND its rule is the relay's only bound on firehose growth (keep pruning, fall
  back to the last successfully-read subscriber set; the master switch OFF drops that memory). The
  block purge is NOT exempt — paying doesn't buy immunity from moderation.
  **Notifications** (`notify_lifecycle`, all via `system_dm` — never the operator key, which is a
  self-DM on a single-admin node): payer on credit AND on a too-small payment (silence there reads as
  "my money vanished"), the ADMIN on every payment (Nostr + Telegram if linked), the recipient of an
  admin grant, and — the one that prevents a LOSS — the subscriber 7 days before expiry and at expiry,
  since a lapse hands them back to the free window and the next auto-clean. The warn/end markers are
  keyed on the EXPIRY TIMESTAMP (a renewal re-arms them for free) and `_normalize` must carry them or
  both DMs re-send every 5-minute tick. The two paths order the write and the send OPPOSITELY, on
  purpose: a PAYMENT DM asserts persisted state (never send unless the ledger write landed — and the
  unsaved dedup id makes the next scan re-credit it anyway), a LAPSE WARNING asserts a fact about the
  clock, so it sends FIRST and marks only what went out — marking first lets one transient publish
  failure swallow the only warning a subscriber gets before their posts are deleted.
  **The tiered rules' `kind IN (_PRUNABLE_KINDS)` qualifier is load-bearing far beyond feed posts.**
  They are the only rules in the codebase that can delete an `origin='direct'` event, and the app's
  own datastore — settings, chats, Notes, calendars, contacts — is direct-published kind 30078. It is
  easy to read a rule as "age out this stranger's old direct writes" and not notice that a calendar is
  one. `tests/test_relay_prune.py::test_calendars_and_contacts_survive_every_cleaner` asserts it by
  name against every cleaner at once with the paid tier ON: drop that clause and **all** of a
  non-subscriber's calendar and addressbook documents are deleted.
  See `docs/PAY_TO_STAY.md`; `tests/test_paid_retention.py`.
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
- **Talking pictures** (`talk` command; `app/services/effects_service/talk.py`): attach a face AND a
  few seconds of a voice, type a line, get an MP4 of that face lip-syncing it IN THAT VOICE. The
  feature is TWO halves on two different queues, and that split is the design: **speech = the CLONED
  VOICE model** via `voice_factory` (the same call `voice` makes, so it inherits `GPUResourceLock`,
  `prepare_for_voice`'s VRAM swap, the `chat_server_urls` round-robin and the busy probe) — it is
  deliberately **NOT edge-tts**, which is what `narrate` uses; **mouth = a CPU puppet warp**, not
  Wav2Lip/SadTalker — numpy+Pillow only, identical on CUDA/Arc/ROCm/no-GPU, **never takes
  `GPUResourceLock`**, and it works on DRAWINGS (neural lip-sync smears on flat art). `talk.py` takes
  a picture and a PATH TO AUDIO and knows nothing about where the speech came from — that is what
  keeps the GPU discipline in one place. The mouth render queues like every meme render:
  `/client/meme/talk` takes `_meme_slot()`, the per-user cooldown and `_meme_lb_forward` overflow;
  chat/Telegram use the ordinary `execute_command` path like `compress`. The Meme Builder button
  **borrows `PC.openVoiceStudio`** (as "Add a voice line" already does) rather than growing a second
  library/recorder/queue-notice — only the ENDING differs, so there is no second speech endpoint.
  **Telegram is a KNOWN GAP, not a bug to hunt:** it can't put a photo AND an audio clip in one
  message, and the handler never downloads `message.voice`/`message.audio` at all (only photo /
  document / video — which also means `voice`'s "reply to a voice note" docstring doesn't hold
  there). `talk` stays in the TG lists only so it can't fall through to the LLM; making it work needs
  an interactive ForceReply flow like `clip`'s. Treat it as web-UI-only for now.
  **ANIME/flat art: the mouth is PLACED BY HAND, and that is the design.** Every face model here is
  trained on photographs — InsightFace *detects* an anime face fine and then puts the mouth
  landmarks on the chin and a cheek (measured: 1.7x too wide, 16px low), and the cascade's 0.42x
  face-width mouth belongs to `blue`'s paint smear, not to lip-sync (~3x too wide). A confident
  wrong answer is worse than none. So the builder opens a placement control BEFORE the voice
  (draggable marker + width, seeded from `POST /client/meme/face` — CPU, no render slot), and its
  Photo/Drawing toggle picks the RENDERER: a photo WARPS its real jaw, flat art REDRAWS the mouth
  (a cel mouth is an ink stroke — sliding it duplicates and smears it). Placement is NORMALISED so
  it survives every resize, and CLAMPED server-side (`_clean_mouth`) because `w` scales every length
  in a 600-iteration loop. Chat/Telegram have no picker and still auto-detect. A CHARACTER POSE
  (`carl`, `jerry`, …) goes through the picker too — it briefly did not, on the theory that fixed
  artwork has a fixed detection, but a fixed answer that is off is off on EVERY render with no way
  to correct it. Its layer `src` is the rendered clip, so the picker's picture and its detection seed
  both come from the pose's own artwork (`GET /client/meme/character/<name>`, `POST /meme/face`
  with `character`), which is also what the render animates. One name check for all three,
  `_pose_art_path`.
  **A cut-out layer forces a SILENT clip:** MP4 has no alpha, so rendering one turns a
  background-removed layer into a BLACK RECTANGLE with the subject on top; the transparent form must
  be VP9-alpha WebM, which cannot carry audio without corrupting the alpha (`_ALPHA_VCODEC`). So
  `add_talk(keep_alpha=True)` returns `(webm, ct)`, the endpoint reports `alpha:true`, and the client
  adds the spoken line as its OWN audio layer. Chat/Telegram keep the MP4 (a reply must be one file).
  ffprobe reports `pix_fmt=yuv420p` on that WebM and the alpha IS still there — decode with
  `-c:v libvpx-vp9` to see it; don't "fix" a working file on ffprobe's say-so.
  Reference clips are normalised by ONE shared helper, `_voice_reference_wav` (`voice` + `talk`);
  it writes the upload into a SUBDIRECTORY because a clip named `ref.wav` would otherwise BE
  ffmpeg's output path ("cannot edit existing files in-place") — that bit `voice` too. Wired as a
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
- **Native apps run WITHOUT an instance** (`desktop/`, `mobile/`): both BUNDLE the web client, and the
  desktop build (`desktop/build-www.sh` → `desktop/main.js`) can run with **no PosterChan server at all** —
  relays + a key. See `desktop/README.md`. Three things this changed, each a trap:
  - **`BUNDLED` and "has an instance" are different questions.** `typeof window.__PC_API_BASE__ !==
    'undefined'` means bundled; its VALUE being empty means no server. Conflating them registered the web
    PWA's `/client/sw.js` inside a bundle that only ships `/sw.js` (404 → no SW → no media cache) and
    removed the instance picker on exactly the installs that needed it. `_standalone()` = both.
  - **Standalone hides every server-backed surface** (`applyInstanceGating`, `INSTANCE_VIEWS`,
    `INSTANCE_SETTINGS_TABS` in `app.js`) and forces `PC_NOSTR_ONLY` at RUNTIME — one bundle serves every
    instance, so the template's baked value is either wrong or permanent, hence `nostr_only` in
    `/client/config`. Anything reading a server must ALSO tolerate its absence: `renderUserSettings` spent
    ~2.4s failing `/api/auth/settings` then dead-ended on "Couldn't load your settings" — on the one
    screen a server-less user cannot do without, since it is where relays and the instance are set. Its
    Save read `#us-email` unguarded and threw BEFORE the client-side saves, so the relay and media edits
    silently did nothing.
  - **Relay pre-fill is the feature, not a nicety.** `defaultRelays()` offers this node's relay +
    `default_relays` from the server, and falls back to a HARDCODED copy of OUR relay set — the case that
    matters, because "I want no instance" and "the instance is down" look identical from the client. Keep
    `FALLBACK_RELAYS` in step with `nostr_service.DEFAULT_RELAYS`. `connectRelays()` used to call
    `Relay.connect(undefined)` with no instance, which opens a socket to the page's own origin and can
    only fail — "reconnecting…" forever in an app that needs no server to read Nostr.
  - Desktop loads the bundle over a privileged **`app://`** scheme, NOT `file://`: a file page is not a
    secure context, so Chromium deletes `crypto.subtle` and the client cannot sign anything. That origin
    (`app://posterchan`) must stay on the CORS allowlist in `app/main.py`.
  - **`build-www.sh` must copy `static/fonts/*.woff2`.** `client.css` `@font-face`s them at root-relative
    urls INSIDE a stylesheet, which the fetch shim never sees — so a bundle without them 404s and the
    whole app drops to a system font, silently. Both build scripts were missing them.
  - **Native Tor is desktop-only and bundled** (`desktop/tor.js`; Android can only ask Orbot). The window
    is HELD on `boot.html` until the circuit is up (the client opens relay sockets on evaluation, so
    loading first leaks), and it **fails closed** — tor dying must not clear the session proxy.
    `GeoIPFile` is load-bearing: without it `ExitNodes {cc}` cannot be resolved and the country picker
    silently does nothing while tor reports 100%. `StrictNodes 1` goes with `ExitNodes` and nowhere else.
    Ports are ephemeral (9050 collides with the system tor a Tor user already runs).
    `tests/test_desktop_tor.py` + `scripts/check_desktop_standalone.py` cover all of it; Electron itself
    cannot be driven here (it needs an X display), so the preload bridge is STUBBED the way preload
    injects it.
    **Windows worked and the other two did not, for two different reasons — both invisible.** LINUX: the
    bundled tor has NO RPATH/RUNPATH and ld.so does not search cwd, so it either would not start or (on
    any distro with a system libevent, i.e. most) loaded the WRONG one and died on `undefined symbol:
    evutil_secure_rng_add_bytes`; `spawnEnv` sets `LD_LIBRARY_PATH` to the bundle dir and REPLACES the
    inherited value (an AppImage exports its own lib dir, which shadows tor's). macOS needs none of that
    (`@executable_path`) and must not get `DYLD_LIBRARY_PATH`, which hardened processes strip. MACOS: only
    the x86_64 binary shipped, so Apple Silicon needed ROSETTA — not installed until something asks, and a
    native arm64 app never asks; CI now also packs `resources/tor/arm64/` and `torBinary()` prefers
    `<root>/<process.arch>`. A binary that cannot exec is an error in the panel, not a dead app: an
    `'error'` event with NO listener is re-thrown and kills the Electron main process.
  - **QR codes are drawn in the CLIENT** (`static/js/client/qr.js`, byte mode + EC level M, versions
    1-40), not fetched from `POST /client/qr`. A server-rendered QR is the one dependency the sign-in
    screen cannot have — with no instance there is nothing to ask and over Tor an unrouted .onion fails
    the same way, on the screen whose entire instruction is "scan this". Same for the two tip QRs. The
    endpoint is KEPT for installed clients (a cached PWA, an older APK/desktop build still POST to it).
    `tests/test_client_qr_encoder.py` does not compare pictures — it DECODES every version 1-40 with
    jsQR, because a wrong EC table looks perfect and scans as nothing.

## Conventions / gotchas

- Routers thin, logic in services. Match surrounding style; plain-text Telegram messages avoid
  Markdown parse errors on arbitrary content.
- The in-memory node job registry and the social poller are **per-process** — correct on the
  single port-3051 instance; would need a shared store if ever scaled to multiple workers.
- Do not run `git gc`/maintenance on the Gitea server data dir (production).
- `app/routers/openai_api.py` is a generic proxy — keep it task-agnostic; never hardcode
  task-specific logic there.
- Detailed setup (LLM/image/IPEX/nginx) lives in `docs/`.
