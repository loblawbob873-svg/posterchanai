<div align="center">

<img src="static/mascot/mascot-happy-front-05.png" alt="Poster-chan mascot" width="200" />

# Poster-chan AI

### A Nostr-powered personal cloud, and a self-hosted AI powerhouse — on your hardware, under your keys.

**Replace the cloud, then put a GPU behind it.** Your notes, calendar, contacts, addressbook, files, photos, passwords, bookmarks, mail, music and folder sync — all of it living as **encrypted Nostr events on a relay you run**, reachable from a web client, a desktop app, an Android app, and from any CalDAV/CardDAV phone client you already own. No account with anybody, no per-seat billing, no vendor holding the keys.

**Then the part a cloud drive can't do:** the same box runs your own models. Chat, image, voice, video and music generation, a private metasearch engine, live streaming, and autonomous bots on **Telegram, Pleroma & Nostr** — driven by cloud LLMs or the built-in native `llama.cpp` (CPU / CUDA / ROCm / **Intel Arc**). One FastAPI backend, an OpenAI-compatible `/v1/`, and a web-of-trust relay on PostgreSQL that *is* the datastore.

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Nostr-native](https://img.shields.io/badge/Nostr-native-8e44ad?logo=nostr&logoColor=white)](docs/NOSTR_DATASTORE.md)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-datastore-336791?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![OpenAI-compatible](https://img.shields.io/badge/API-OpenAI--compatible-412991?logo=openai&logoColor=white)](#chat--ai)
[![Self-hosted](https://img.shields.io/badge/Self--hosted-100%25-success)](#quick-start-backend-and-web-ui)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20GPU%20optional-FCC624?logo=linux&logoColor=black)](#requirements)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

[Try it](#try-it-in-one-command) ·
[Quick start](#quick-start-backend-and-web-ui) ·
[Features](#features) ·
[PosterChanOS](#posterchanos) ·
[Bots & social](#bots--social) ·
[Documentation](#documentation)

</div>

---

## Try it in one command

Docker, no configuration, nothing to sign up for:

```bash
git clone https://github.com/loblawbob873-svg/posterchanai.git && cd posterchanai
docker compose --profile nostr up -d --build
```

Then open **<http://localhost:3051/client>** — a self-hosted **Nostr relay + full web client +
Blossom media server**, with no AI stack (a ~2 GB image, not ~70 GB). Sign in with a browser
extension like Alby, or let the app generate you a key. Postgres comes up alongside it
automatically; you don't need a `.env`, a domain or a certificate to look around.

Then, when you want more:

| | |
|---|---|
| **Put it on a server** other people use | **[docs/NOSTR_DOCKER.md](docs/NOSTR_DOCKER.md)** — empty VPS to public instance: HTTPS with one extra profile, claiming admin, relay config, backups |
| **Add the AI** | swap `--profile nostr` for `cpu`, `cuda`, `rocm` or `intel` — [docs/DOCKER.md](docs/DOCKER.md) |
| **Bare metal instead** | the interactive installer — [Option B](#option-b-installer-linux-recommended-for-bare-metal) |

> Local trials skip HTTPS because browsers treat `localhost` as a secure origin — so the client can
> sign events there. On any other address you need TLS, which is what the `tls` profile is for.

---

## Why Poster-chan?

### The personal cloud

- ☁️ **The things you'd otherwise rent** — **Notes** (offline-first, with Joplin import), **Calendar** and **Contacts** over real **CalDAV/CardDAV** so your phone's own apps sync with them, **Files** on encrypted Blossom storage, **Folder sync** for Documents and Pictures across desktop and Android, **Mail** over your own IMAP/SMTP, **Music**, **Budget**, and a **password manager**. One node, one account, no subscription.
- 🟣 **Encrypted events, not somebody's database** — the built-in **web-of-trust relay** (on PostgreSQL) is the source of truth. Settings, accounts, API keys, notes, calendars and AI chats are **NIP-44-encrypted `kind-30078` events** signed by your node. Log in with your **Nostr key** (NIP-07/NIP-46). No SQLite, no proprietary schema, nothing to export because you already hold it.
- 🔐 **Passwords & bookmarks in the browser you already use** — a **Firefox / Chrome / Brave** extension turns your relay into an end-to-end-encrypted **password manager** and **cross-browser bookmark sync**. Every login is an **AES-GCM-encrypted** event decrypted only on your device — the relay holds ciphertext. Autofill, **TOTP** codes, a generator, and a built-in **NIP-07 signer** so you sign into Nostr apps without pasting your `nsec`.
- 📱 **Every surface** — a cyberpunk **Nostr web client** and PWA, a **desktop app** (Windows/macOS/Linux, with bundled Tor), an **Android APK**, a **windowed desktop mode**, and CalDAV/CardDAV for the phone apps you already have. The native apps bundle the client and can run with **no server at all** — relays and a key are enough.

### PosterChanOS

- 🖥️ **A Nostr-native desktop operating system** — **PosterChanOS** is the Gentoo-based, encrypted
  bare-metal edition of PosterChan. It boots directly into the PosterChan desktop shell on Sway, so
  Social, Messages, Notes, Files, Music, Terminal and the rest are applications rather than tabs
  trapped in a browser. Native Wayland applications such as Firefox, Telegram, Steam and office
  tools open alongside them in the same desktop.
- 🔑 **Your Nostr identity is your OS identity** — signing in with an `npub` provisions a stable,
  separate Linux account and a private `0700` home directory. Multiple Nostr identities can use one
  computer without sharing files. The first identity to claim a new installation becomes its
  administrator; later identities are ordinary users, and root is not the daily login.
- 🔒 **Encrypted from the first boot** — the installer creates a LUKS-backed Btrfs system, builds the
  matching initramfs and bootloader configuration, and installs the PosterChanOS boot and recovery
  tools. The live image can be used to try the desktop or install it to disk.
- 🔄 **One update path** — `update-posterchan` updates both the packaged desktop and the PosterChanOS
  session integration. The canonical installer is [`os/gentoo.sh`](os/gentoo.sh), and the design is
  explained in **[the PosterChanOS article](docs/blog/posterchanos.md)**.

### The AI powerhouse

- 🔌 **Bring any model, or host it** — cloud (any OpenAI-compatible API) or the **built-in native `llama.cpp`** backend (CPU / CUDA / HIP / **Intel Arc SYCL**), round-robin load-balanced across several boxes with a shared GPU lock so one card serves chat, images, video and music without thrashing.
- 🎨 **Generate on your own silicon** — images, **text-to-video**, **text-to-music** (ACE-Step, in-process), TTS/STT and voice cloning, talking-picture lip-sync, a meme builder, website screenshots, YouTube/X summarize & download, and interactive study flashcards.
- 🔎 **Your own search engine** — a **SearXNG instance bundled with the node**, running inside the app, behind a search screen with AI overviews and citations. Your node can be your browser's search engine.
- 🤖 **It's also a bot platform** — drive everything from **Telegram**, and run autonomous **Pleroma / Nostr** bots from a single admin tab.
- 🔴 **Go live from OBS** — RTMP to the bundled **MediaMTX** (RTMP → HLS), announced on Nostr via **NIP-53**, with a bitrate clamp so one streamer doesn't cost you a viewer's worth of upload each.
- 🛠️ **Hackable & honest** — thin routers, services for logic, an interactive installer, and an OpenAI-compatible `/v1/` that agentic coding clients (e.g. opencode) can drive against your local models.

> Point any OpenAI-compatible tool at `http://your-box:3051/v1/` and you've got a private, function-calling-capable model server. Open the web UI and you've got your files, mail, calendar and assistant in one place. Link a bot and it's in your pocket.

---

## Features

### Chat & AI

- **Streaming chat** with multiple conversations, history, and optional markdown/formatting
- **OpenAI-compatible API** at `/v1/` for compatible clients and tools, including **function/tool calling** so agentic coding clients (e.g. opencode) can drive your local models
  - **Recommended coding model:** [Qwen3-Coder-30B-A3B-Instruct](https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF) (`IQ4_XS`, MoE ~3B active, ~16GB) — by far the most reliable local model here for multi-step agentic builds; it 1-shots small apps where 8–14B models stall. Needs a 12GB+ GPU (partial CPU offload on 12–16GB cards; leave the context size on `auto`). For smaller GPUs, `Qwen3.5-9B-Claude-Code` is the lightweight fallback. Set it once server-wide in **Admin → AI Settings → Agentic / Tools Model** (`llm_tools_model`) — used for every tool-bearing `/v1` request *and* the `node agent` command (web UI + Telegram), while plain chat stays on the default model.
- **Local or remote LLM**: the built-in native **llama-cpp-python** backend (CPU / CUDA / HIP / Intel Arc SYCL), or any remote OpenAI-compatible server
- **Load balancing**: round-robin across multiple chat servers
- **Intent detection** and slash-style **commands** (e.g. `/mail`, `/image`, `/search`)

### Nostr web client

The app's face is a full cyberpunk **Nostr web client** (PWA at `/client`, plus an installable Android APK) — feed, profiles, DMs, notifications, articles, communities, bookmarks, and more. Highlights:

- **News (RSS reader)** — a built-in RSS/Atom reader under **Discover → News**. Feeds are fetched **server-side** through the built-in **HTTP proxy (→ Tor), with a direct fallback**, and served from a **shared, steady stale-while-revalidate cache** — each feed is fetched at most once per 5 min and concurrent requests are deduped, so it scales to many users without hammering the source sites. Your **feed list and read state are your own encrypted Nostr events** (`kind-30078`), cached locally and hydrated from the relay so they **sync across devices**. Per-article **Share as a note** and **LLM Summarize**, **mark-read-as-you-scroll**, **OPML import/export** (Miniflux-compatible), and YouTube-channel thumbnails. Hardened: `defusedxml` parsing, SSRF-guarded fetches (per-redirect-hop revalidation), and streamed size caps.
- **Voice & video calls** — 1:1 and small-group **WebRTC** calls that work **across instances**, signaled over Nostr (`kind-25050`) with a built-in Pion **TURN** relay for NAT traversal. Audio-first with a mid-call video toggle and screen-share.
- **Live streaming (OBS)** — go live from **OBS** (or any RTMP encoder): the bundled **MediaMTX** ingests RTMP → HLS, and the stream is announced on Nostr via **NIP-53** (`kind-30311`) under **Discover → Streams**, so it also shows up in the wider Nostr stream directories. Auth reuses your per-user API key; off by default (`install.sh --stream`).
- **Meme Builder** — a layered in-browser video editor (image / video / text / audio layers, per-layer sounds, timeline trims) that composites to one MP4 **server-side**. You can drop a **full effect as a transparent overlay layer** — a dancing character, a shrug, a reaction — rendered on an alpha canvas (VP9-alpha WebM) and composited over whatever's beneath it, with its sound. Effect audio clips are also selectable per layer.
- **Effects studio** — one-tap image → MP4 effects: photo treatments (`glow`, `alive` 3-D parallax), meme/caption formats, character overlays, and a large library of audio-gag reaction clips — all also usable as commands in the web UI, Telegram, and the fediverse bots, and now composable as Meme Builder layers.
- **Mini apps / games (webxdc)** — post a `.xdc` (a [webxdc](https://webxdc.org/apps/) game, poll or shared editor, the same file Delta Chat runs) and everyone with the post in their timeline plays the same instance: moves are Nostr events, with an ephemeral realtime channel for continuous play (Quake III and a Half-Life port both work). Apps are untrusted code and get **no network access at all**, so they run on a **separate origin — `xdc.<your-domain>`**, which is the one thing a fresh deployment must add: `./install.sh --webxdc` (DNS record + its own certificate + vhost), one line for Caddy/Traefik, or already seeded in the Docker `proxy` service. See [docs/WEBXDC.md](docs/WEBXDC.md).
- **Git over Nostr (NIP-34)** — a **Discover → Git** view: browse announced repos, and open one to read its **README, issues and patches** right in the app (README fetched from the repo's clone/web URL). Optionally, your instance can **host repos itself** over Nostr via a self-contained **GRASP** git server that runs as its own supervised subprocess — **off by default / experimental**; see [docs/GIT_OVER_NOSTR.md](docs/GIT_OVER_NOSTR.md).

### Passwords, bookmarks & signing (browser extension)

A companion **browser extension** (Firefox, and Chrome / Brave via MV3) makes your self-hosted relay double as a private **password manager** and **cross-browser bookmark sync** — with nothing readable server-side. Pair a browser to your vault (a QR/paste code) as **read-only** (fill only) or **full** (also save & publish):

- **Password vault** — logins are **`kind-30078` events, AES-GCM-encrypted to your key**; the relay only ever holds ciphertext, and it's decrypted on-device. Autofill on the matching site (**exact-origin only** — never a sibling subdomain), **TOTP** one-time codes, and a **password generator**. New logins saved from a read-only browser queue locally until the app publishes them.
- **Bookmark sync** — one encrypted event per bookmark, sealed with the same vault key, **kept in sync across your browsers**: adds, moves and deletes propagate (and deletions *stay* deleted), toolbar-vs-menu placement and folders are preserved, and duplicates are de-duped by URL. `chrome.alarms` keeps an idle browser syncing.
- **NIP-07 signer** — the extension can sign for Nostr web apps, with **per-origin, per-event-kind approval** shown in a real extension window (not a page overlay) — so you log into Nostr sites **without pasting your `nsec`** into a page.

**Firefox: [get it on addons.mozilla.org](https://addons.mozilla.org/firefox/addon/posterchan-passwords/)** — Mozilla-signed, so it installs permanently on release Firefox *and* Firefox for Android (where passwords, one-time codes and signing work; bookmark sync needs a bookmarks API Android's Firefox doesn't have), and auto-updates. **Chrome/Edge/Brave** load it unpacked, and you can sideload your own Firefox build too — both in **[extension/README.md](extension/README.md)**, which also covers pairing and switching on bookmark sync. Build both bundles with `bash extension/build.sh` (Firefox `.xpi` + Chrome unpacked-`dist/chrome`); CI publishes fresh unsigned artifacts to **poster.place/extension/unpacked** and **/chrome**.

### Voice & media

- **Text-to-speech (TTS)** and **speech-to-text (STT)**; Edge TTS and configurable backends
- **Voice cloning** (`voice`): **zero-shot** — attach a few seconds of someone speaking, then `voice <text>` and it says your words **in that voice**, learned from the clip (no training step). Runs **native in-process** (no sidecar), on the app's own GPU lock and VRAM swap like image/music/video gen. The same cloned voice also drives **`talk`** — a still face lip-syncing a line in that voice. Web UI + Telegram.
- **Image generation**: native diffusers (SDXL); multiple image servers supported
- **Music generation** (`musicgeni`): text-to-song with [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5) running **native in-process** — the app's own venv, torch and GPU lock, no sidecar service and no HTTP hop (fits a 12 GB GPU); load-balanced + VRAM-swapped across nodes like image gen. With no lyrics supplied the LLM writes them, so you get vocals by default (`instrumental` skips it); each song comes back as a branded MP4 — the track over a PosterChan background with the end-card outro. Web UI + Telegram. See [docs/MUSIC.md](docs/MUSIC.md)
- **Video generation** (`videogeni`): native in-process text-to-video via **diffusers** — point it at *any* model (Wan2.1 / LTX / CogVideoX, auto-detected) to match your GPU; runs on CUDA / Intel Arc (XPU) / AMD (ROCm), load-balanced + VRAM-swapped across nodes like image gen, with a branded watermark and optional 720p/1080p upscale. Web UI + Telegram. See [docs/VIDEO.md](docs/VIDEO.md)
- **Website screenshots**: full-page capture with the `screenshot <url>` command (also `shot` / `ss`) — works in the web UI and Telegram. Uses headless Chrome (JS-aware, so SPAs render), Firefox fallback (see [Requirements](#requirements)).
- **YouTube / X**: summarize a video **from its transcript** (so summaries and link-posts reflect the actual content, not the page), grab thumbnails, or **download** audio (MP3) / video with the `ytdl` command — in the web UI, Telegram, and Pleroma. A video download can be trimmed and/or shrunk in one command (`ytdl video <url> clip 0:10 0:30 compress`); Telegram also offers these as buttons after the download

### Your personal cloud (mail, calendar, contacts, files, sync)

- **SMS and MMS over Nostr**: use an Android phone as the cellular bridge, then read and answer its
  conversations from any PosterChan client. The phone mirrors SMS/MMS as encrypted, addressable
  Nostr records; message bodies and MMS originals/thumbnails are encrypted on-device and stored in
  the logical **Messages** and **MMS** Blossom folders. A desktop send becomes an idempotent encrypted
  request that the paired phone performs through its radio and acknowledges so it cannot be sent
  twice. Thumbnails save bandwidth and the original is fetched only when opened. Android's system
  message provider remains authoritative, and the phone must be reachable to perform a carrier send.
  Existing MMS already stored by Android can be mirrored; fetching a new MMS from a carrier MMSC is
  not yet supported. See **[docs/PHONE_SHELL.md](docs/PHONE_SHELL.md)**.
- **Email**: a full mail client (Messages → 📧 Email) over your own IMAP/SMTP accounts. The mailbox
  is mirrored into **encrypted Nostr events** — one per message — so it is searchable without an
  IMAP round trip and syncs across your devices; **attachments are AES-GCM encrypted and stored in
  Blossom**, with the key held only inside the (already encrypted) message document. Threads,
  folders, bulk actions, drafts, a unified *All inboxes* view, contacts autocomplete in the
  composer, and per-account **folder mapping** (Settings → Mail → 📂 Folders) for servers whose
  Sent/Drafts mailboxes don't match the guess. The mailbox is **local to your node** and never
  federated. Optional background polling pushes new mail to a locked phone. See
  **[docs/MAIL.md](docs/MAIL.md)**
- **Folder sync**: a folder on your desktop kept in step with a folder on your phone, through your
  own node — Documents, Pictures, whatever you point it at. File **contents are AES-GCM encrypted
  under your drive key before they leave**, and the file list is NIP-44 encrypted on top of that, so
  your node stores a folder it cannot read. Two devices pair by **giving the folder the same name**
  on each; where it lives is chosen per device. Conflicts keep **both** copies, deletions go to
  `.pc-trash` inside the folder rather than away, an exclusion means "stop looking at this" and can
  never delete anything, and large files move in chunks so an interrupted transfer resumes instead of
  restarting. Desktop app and Android; a browser has no filesystem to sync, but can still browse and
  download from a synced folder. See **[docs/FOLDER_SYNC.md](docs/FOLDER_SYNC.md)**
- **Share to PosterChan**: the app is an Android **share target** and a PWA Web Share Target, so
  *Share* from any app — a photo from the gallery, a link from a browser, a file from a file manager —
  opens the composer with the text and the files already attached.
- **Desktop mode**: a windowed desktop *inside* the client — draggable, resizable, snappable windows
  for the timeline, chats, Notes and the rest, with a wallpaper and a taskbar. Entered from the
  instance logo. **Desktop only, deliberately**: below 1024px the logo does nothing and the ordinary
  client stays, because a draggable window on a phone screen is worse than the app it replaces. The
  windows are real DOM, not iframes — one app, one relay socket, however many windows are open.
  **Arrange it however you like**: drag the icons into your own order, drop one on another to make a
  named folder, right-click to rename a folder, take it apart, or hide an icon from the desktop
  (it stays in the start menu). The arrangement follows your **account**, not the browser — it is
  one encrypted Nostr event, so the desktop you arrange on the laptop is the desktop the tablet
  draws, and nobody but you can read which apps you use.
- **Calendar (CalDAV)**: a calendar server *inside* the app at `/caldav` — your phone and desktop
  calendar app sync with it like any other CalDAV server, no extra service to run. Events are
  encrypted Nostr events; the web client has a month grid, an event editor, `.ics` import/export and
  recurring-event support, and an event's own alarm becomes a reminder that reaches a locked phone.
  See **[docs/CALENDAR.md](docs/CALENDAR.md)**
- **Contacts (CardDAV)**: your addressbook, same server, same account and password — one setup gets
  both calendar and contacts on a phone. Cards are stored **exactly as your phone wrote them**
  (photos, labels and all), with search, `.vcf` import/export, and reuse in the mail composer. See
  **[docs/CONTACTS.md](docs/CONTACTS.md)**
- **Web search**: a **SearXNG instance bundled with your node** — installed by default, branded and
  dark-themed, and running *inside* the app rather than as a container beside it — behind a search
  screen with AI overviews and citations, Save to Notes, and a reader
  that opens the page *inside* the app. Your node can also be added as your browser's own search
  engine. See **[docs/WEBSEARCH.md](docs/WEBSEARCH.md)**
- **News**: LLM news summaries from chat (`news` / `dailynews`), plus a full **RSS reader** in the web client (see [Nostr web client](#nostr-web-client))
- **Budget**: bills, monthly summary and spending plans in the web client (Discover → Budget), stored as a Nostr event **encrypted to your own key** — the server can't read it
- **To-do**: quick personal task list from chat (`todo`)
- **Torrents**: built-in torrent client plus **TorrentGalaxy** search and **nyaa.si** anime search (`torrents`, `nyaa`)
- **File storage** per user and per conversation; file manager in the UI
- **Office documents**: optional built-in Collabora CODE editor for DOCX/XLSX/PPTX and OpenDocument
  files on desktop and mobile. Docker: `POSTERCHANAI_OFFICE=1 docker compose --profile nostr
  --profile office up -d`; bare metal: `./install.sh --office`. Encrypted files are decrypted in the
  browser into an expiring edit session, then encrypted again when saved back to Files.
- **Media tools**: upload a file and `compress` it (image/video — H.264 with GPU acceleration when available), `clip <start> <end>` a video to a time span, or `convert` images↔PDF — all shared across the web UI and Telegram
- **Flashcards (study tool)**: upload a **PDF, image, slide deck (PPTX) or Word doc (DOCX)** and send `flashcards` (or `cards`/`study`/`quiz`) to generate an **interactive multiple-choice quiz** — the LLM writes the questions, options and explanations (math problems include worked steps). The web UI shows animated cards with instant ✓/✗ feedback and KaTeX-rendered math; Telegram shows image cards with answer buttons (tap **🎴 Flashcards** after uploading) and a running score. Text PDFs/slides work best; image OCR is weaker (on Telegram, send screenshots as a *file*, not a compressed photo)
- **Reminders** (`remind`): set a reminder in plain language — `remind open the oven in 10m`, `remind me next tuesday to call mom` — and the LLM parses the time (exact relative phrases like `in 10s` are parsed directly). A background scheduler alerts you in the **web UI** (a full-screen pop-up + beep if you're online, plus a dedicated "⏰ Reminders" conversation) and on **Telegram** if linked. `reminders` lists your pending ones, each with a clickable **Cancel**. Your timezone is **auto-detected from the browser** (IANA zone, DST-aware) — no setup — so times follow you when you travel.

### Bots & social

- **Bot manager (Admin → Bots)**: run autonomous fediverse bots — Pleroma reply bots,
  nitter relays, plus blockbot/welcome/report/hashtag/unfollow daemons — from a
  single admin tab (add/edit, On/Off, live status), backed by the database. The bot framework is
  **bundled in this repo** (`botframework/`) and supervised in-process; no separate repo or
  hand-edited config file. See [Bot manager](docs/BOTS.md).
- **Telegram bot** drives chat, commands, and media from your phone
- **Social posting** to **Pleroma/Mastodon** and **Nostr**: turn any reply, link, or topic into a post with the `post` command (rewrite, verbatim, or with your own instructions). See [Social posting from the bots](#social-posting-from-the-bots).
- **Social notification relay**: forward mentions/replies/DMs from Pleroma/Nostr to Telegram and reply right from the chat. See [Social notifications to Telegram](#social-notifications-to-telegram).
- **Nostr** (keypair identity — no instance, no signup): run a **Nostr reply bot** and link your own `nsec` to post & reply. Handles mentions, replies, reactions, reposts, plus `geni`/image **effects** and **Nitter→Nostr** feeds; publishes to **multiple relays**; uploads media to a **Blossom** (BUD-02) or **NIP-96** host (e.g. nostr.build) embedded with `imeta`; supports **NIP-05** verification. The bot only replies when actually addressed (first mention / direct reply — no thread-spam), is **rate-limited per sender** (with an exempt list), and all bot/social egress can route through the built-in **Tor** proxy. Pure-Python signing (BIP-340) — no native deps.
- **Fediverse ↔ Nostr bridge**: mirror a Pleroma timeline (home/global/local) onto Nostr. Each fediverse author is published under a stable **puppet** key (derived deterministically, so an author keeps one npub across restarts and instances), with avatar + display name, custom emoji as NIP-30 tags, media, quote-posts, and replies threaded via NIP-10 markers. Federated copies are deduped on the canonical AP URI, so the same post arriving from two instances mirrors once.
  - **Write-back**: a reply, reaction or repost made on Nostr is performed **back** on the fediverse under the acting user's own linked account — not the bridge's.
  - **Personal plane** (opt-in per user): your own fediverse notifications arrive as the matching Nostr events, and your fedi DMs as **NIP-17** gift-wrapped Nostr DMs, keeping their direct visibility on reply.

  Configure under Admin → Social. Self-serve enrolment is off by default (`fedi_bridge_self_serve`).
- **Nitter post-cards**: per-user Nitter (X/Twitter) RSS feeds rendered as image "post cards" and delivered to your linked Telegram chat.
- **Translate**: translate text or a replied-to message to any language (`translate`), shared across the web UI and Telegram.

### Extensibility & admin

- **Remote node management**: run OS commands across SSH-reachable machines (or a per-user Debian **sandbox** container) from chat or Telegram, with long-running background jobs and a **tool-calling agentic mode** (one node or all nodes) that streams each step live. The agent keeps a persistent `/workspace`, and you can pull a file — or the agent's whole workspace — **back out** as an encrypted **Blossom** download with `node get <path>` / `node backup` (every sandbox agent run auto-archives its workspace too). See [Remote node management](#remote-node-management).
- **Admin panel**: users, API keys, LLM/image/email settings, systemd service setup
- **Multi-user** with registration (optional), email verification, and quotas

---

## Requirements

- **Python 3.10+**
- (Optional) **GPU** and backends for local LLM (native llama-cpp-python / IPEX-LLM) and native diffusers image generation
- (Optional) **Headless Chrome/Chromium** for the `screenshot` command (driven over the DevTools protocol — full-page and JS-aware, so SPAs render instead of coming out blank; no Selenium/chromedriver). Firefox is used as a fallback if Chrome is absent:
  - Gentoo: `emerge www-client/google-chrome` (or `www-client/chromium`)
  - Debian/Ubuntu: `apt install chromium` (or install `google-chrome-stable`)

---

## Quick start (backend and web UI)

> **Recommended: Docker Compose (Option A).** PostgreSQL is the one datastore for **both** the app
> and the built-in Nostr relay, so it's **required in every setup** — and the compose file brings it
> up and wires it automatically. The bare-metal installer (B) and manual setup (C) work too, but you
> must provision PostgreSQL yourself (the installer can do it). A plain `docker run` with no Postgres
> will not start.

### Option A: Docker Compose (turnkey — one image, any GPU) ✅ recommended

One Ubuntu image builds for **CPU, NVIDIA (CUDA), AMD (ROCm), or Intel Arc (XPU)** —
pick the accelerator with a build-arg. It comes up **turnkey**: native local LLM +
image backends, auto-downloads the recommended chat model on first run, and (on AMD)
auto-detects the GPU override and persists the MIOpen kernel cache for fast image gen.
The compose file brings up **PostgreSQL** (the one datastore) for you.

```bash
# compose (recommended — also starts Postgres):  cpu | cuda | rocm | intel | nostr
docker compose --profile rocm up -d --build
```

Open **http://localhost:3051/client** and log in with your **Nostr key** (NIP-07/NIP-46). The GPU
kernel driver comes from the host (CUDA toolkit / `amdgpu` / `i915`); the userspace + a
GPU-compiled `llama-cpp` are baked into the image. Full matrix — GPU run flags, model
auto-download, opt-ins (Tor/proxy/torrenting), and the opencode/OpenAI-client config —
in **[docs/DOCKER.md](docs/DOCKER.md)**.

> **🟣 Just want a Nostr relay + client, no AI?** `docker compose --profile nostr --profile tls up -d --build`
> builds a small (~2 GB) image with **no AI stack** — self-hosted relay + Nostr web client +
> Blossom — and serves it over HTTPS. Step-by-step from an empty VPS:
> **[docs/NOSTR_DOCKER.md](docs/NOSTR_DOCKER.md)**.

**Production:** add `--profile tls` and the stack brings up its own nginx+certbot container — HTTPS
immediately on a self-signed cert, `certbot --nginx` when you have a domain
([docs/DOCKER.md](docs/DOCKER.md#production-https--tls)). Prefer to run nginx yourself? Template +
guide in **[docs/NGINX.md](docs/NGINX.md)**.

### Option B: Installer (Linux, recommended for bare metal)

The **installer** sets up the virtual environment, dependencies, optional GPU backends (LLM and image), **provisions PostgreSQL** (required — the one datastore), and can configure a systemd service. (If you'd rather not manage Postgres yourself, use **Option A / Docker Compose** instead.)

1. **Clone and enter the project:**
   ```bash
   git clone <your-repo-url> posterchanai
   cd posterchanai
   ```

2. **Run the installer:**
   ```bash
   chmod +x install.sh
   ./install.sh
   ```
   Follow the prompts: it first asks **Full** vs **Nostr-only** (relay + Nostr web client +
   Blossom, *no AI* — light, no GPU). For Full it then detects your GPU, sets up the native
   **llama-cpp** LLM backend and native diffusers image generation, creates the venv,
   installs Python deps, and optionally sets up a systemd service.

3. **Start the server** (if not using systemd):
   ```bash
   ./start.sh
   ```
   Or with systemd: `sudo systemctl start posterchanai-cpu` (or the service name chosen during install).

4. **Open the web UI** at **http://localhost:3051** (or your machine’s IP). Log in or register if enabled.

**Installer options:** `./install.sh --help` for usage; `./install.sh --packages` to print required system packages for your distro.

### Option C: Manual setup (all platforms)

1. **Clone and enter the project:**
   ```bash
   cd posterchanai
   ```

2. **Create a virtual environment and install dependencies:**
   ```bash
   python -m venv venv
   source venv/bin/activate   # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Run the server:**
   ```bash
   python run.py
   ```
   Default: **http://0.0.0.0:3051**

4. **Open the web UI** in a browser: **http://localhost:3051**. Log in or register if enabled.

### Other options

- **Port:** `python run.py --port 8080` or set `POSTERCHANAI_PORT=8080`
- **Start script:** `./start.sh` (if present) to launch the server after manual or installer setup.

---

## Configuration

- Copy **`.env.example`** to **`.env`** and adjust (optional).
- Everything else is configured from the **web UI** and **Admin** panel — settings, accounts and API keys are stored as **encrypted Nostr events on the built-in relay** (PostgreSQL-backed), not a local config DB:
  - LLM model (native llama-cpp backend; IPEX on Intel Arc)
  - Image generation (native diffusers model)
  - TTS/STT, email, plugins
- See **`docs/`** for detailed setup (IPEX, nginx, etc.).

### Social posting from the bots

Connect a social account in **User Settings → Pleroma**, then use
the `post` command from the **Telegram** bot to publish.

**Telegram** — reply to any message (a bot answer, a link, a photo) and send:

| Command | What it does |
| --- | --- |
| `post` | Rewrites the replied-to content into a viral, engaging post |
| `post raw` | Shares the reply **exactly as written** — no rewrite (aliases: `verbatim`, `as-is`, `exact`) |
| `post <instructions>` | Rewrites following your instructions, e.g. `post professional`, `post funny and short`, `post don't include links` |

The bot then shows share buttons (**📣 Pleroma / Nostr**, **🚀 Post to All**,
**❌ Skip**) for whichever platforms you've connected. Replying to a photo shares the
image itself. The source URL is appended by default; `post don't include links` (or
"no links", "without url", …) omits it.

### Social notifications to Telegram

Forward new notifications from your connected **Pleroma / Nostr** accounts to your
linked Telegram chat, and reply to them without leaving Telegram. Enable it per-user in
**User Settings → Telegram → "Relay social notifications to Telegram"** (the admin must also
turn on the global switch in **Admin → Social → Social Notification Relay**, where the poll
interval is set).

- New mentions, replies, DMs, follows, and reactions/boosts are forwarded as they arrive.
- **Reply** to a forwarded message in Telegram to respond on the originating platform — your
  reply is posted as a reply to the original post (inheriting its visibility).
- **Pleroma:** DMs are direct-visibility mentions, so they arrive as normal
  notifications and forward with full content; your reply stays `direct`.

### Remote node management

Run OS commands on a fleet of machines from chat or Telegram. Enable it in
**Admin → Nodes → Agentic Node Management**, list your nodes (one per line as
`name|user@host`, or `name|local` for this host), and pick which users may use it
(admins always can). Remote nodes are reached over **SSH** — they need nothing
installed, just the posterchanai host's SSH public key in their `authorized_keys`.
This works for any SSH-reachable device: servers, routers, switches, etc.

| Command | What it does |
| --- | --- |
| `node <name> <command>` | Runs a shell command on the node. Fast commands return inline; long-running ones become a background job |
| `node all <command>` | Runs the command on **every** configured node in parallel and shows each result (`all` is reserved as a node name) |
| `node agent <name> <goal>` | Agentic mode — the AI calls a `run_command` tool, reads each result, and iterates toward your goal. Uses native tool-calling and an agentic-tuned model; each step streams live to your chat |
| `node agent all <goal>` | Run the agent on **every** configured node toward the same goal (sequential; per-node labelled output) |
| `node list` | Show configured nodes |
| `node jobs` | List your recent jobs |
| `node log <id>` | Show a job's output |
| `node kill <id>` | Stop a running job |

Background jobs keep running server-side; when one finishes you get a Telegram DM
(if your account is linked) with the result, or check it with `node log <id>`. In
agentic mode each command is bounded by a per-step timeout (kills and reports
runaway/hung commands so the loop can't deadlock) and runs in its own process
group so nothing is left orphaned.

> ⚠️ **This is unrestricted command execution by design** — there is no confirmation
> step or command allowlist. Only enable it on a trusted, private deployment, and keep
> the allowed-users list tight. Set a **Job Timeout** (and **Agent Step Timeout**) if you
> want a hard kill switch.

### Large files on Telegram (optional)

The cloud Telegram Bot API limits the bot to downloading files up to **20 MB**, so
`compress`/`clip`/`convert`/`translate` only work on small uploads there (the web UI
has no such limit). To lift this to **~2 GB**, run a local Bot API server:

1. In **Admin → Telegram**, enter your **API ID** and **API Hash**
   (from https://my.telegram.org) and save.
2. Set it up — either re-run `./install.sh` and pick **option 5** (*Telegram Bot API
   server — add-on only*, which sets up just the server against your existing install
   and reads the credentials from the database), or run it directly:
   ```bash
   ./scripts/setup-telegram-local-api.sh        # uses sudo internally as needed
   ```
3. Back in the admin UI, click **Test Local Server** (it should go green), then tick
   **Use local Bot API server** and **Setup Webhook**. (The installer/script also set
   these for you.)

### Updating (option 6)

To upgrade an existing install, re-run `./install.sh` and pick **option 6 (Update)**.
It upgrades the posterchanai Python dependencies and optionally rebuilds the local
Telegram Bot API server (`REBUILD`), then restarts the services. On **Intel Arc**
installs it freezes the fragile pins (`torch`/`torchvision` XPU build, the `transformers`
stack) in the unified venv to their installed versions first, so an upgrade can never pull a
CPU torch over the XPU build — a conflicting upgrade is skipped rather than allowed to break
the Arc environment.

> Note: `sync.sh` deploys **code** (git pull + restart); it does **not** touch Python
> deps or the Telegram server binary. Use **option 6** for dependency upgrades.

---

## Project layout

| Path | Description |
|------|-------------|
| `app/` | FastAPI app, routers (auth, chat, admin, TTS, STT, mail, torrent, bots, etc.), services |
| `botframework/` | Merged autonomous bot framework (Pleroma/nitter listeners + daemons); spawned by `app/services/bot_manager_service.py`. See [docs/BOTS.md](docs/BOTS.md) |
| `templates/` | Jinja2 HTML (login, chat, admin, modals) |
| `static/` | CSS, JS, icons, mascot assets |
| `os/` | PosterChanOS installer, Sway session, boot theme, system helpers and Gentoo overlay |
| `run.py` | Server entry (uvicorn) |
| `requirements.txt` | Python dependencies |
| `install.sh` | Interactive installer (Linux) |
| `docs/` | [ADVANCED.md](docs/ADVANCED.md) (LLM, image, load balancing), IPEX, nginx, etc. |

---

## Documentation

- **[docs/DOCKER.md](docs/DOCKER.md)** — Turnkey Docker image (CPU / NVIDIA / AMD / Intel Arc): build matrix, GPU run flags, model auto-download, opt-ins, HTTPS via the `tls` profile, and OpenAI-client/opencode setup
- **[docs/NOSTR_DOCKER.md](docs/NOSTR_DOCKER.md)** — Nostr-only instance, start to finish: empty VPS → HTTPS → claiming admin → relay/NIP-05/Blossom config → backups
- **[docs/BOTS.md](docs/BOTS.md)** — Bot manager: the merged `botframework/`, Admin → Bots, per-bot config, the single server endpoint, and per-node cutover
- **[docs/ADVANCED.md](docs/ADVANCED.md)** — LLM backends, image generation, load balancing, Intel IPEX
- **[docs/MAIL.md](docs/MAIL.md)** — The mail client: how the mailbox is stored, why it is local to your node, encrypted attachments, folder mapping and notifications
- **[docs/CALENDAR.md](docs/CALENDAR.md)** — Bundled CalDAV: what "encrypted" does and does not mean here, adding it to a phone, import/export and recurrence
- **[docs/CONTACTS.md](docs/CONTACTS.md)** — Bundled CardDAV: one account for calendar *and* contacts, and why cards round-trip byte for byte
- **[docs/blog/posterchanos.md](docs/blog/posterchanos.md)** — How PosterChanOS joins Nostr identity, private Unix homes, an encrypted disk and the desktop shell
- **[docs/PHONE_SHELL.md](docs/PHONE_SHELL.md)** — Android phone integration and encrypted SMS/MMS mirroring and sending over Nostr
- **[docs/WEBSEARCH.md](docs/WEBSEARCH.md)** — The bundled SearXNG, the in-app reader, and where a node actually searches
- **docs/** — nginx and other feature documentation

---

## License

**Poster-chan AI** is free software, released under the **[GNU General Public License v3.0](LICENSE)**.
You may use, study, share, and modify it — and if you distribute a modified version, you must
pass on the same freedoms under the GPLv3. See [`LICENSE`](LICENSE) for the full text.

## Contributing

Issues and pull requests are welcome. Keep changes small and consistent with the existing
patterns (thin routers, logic in services); see [`CLAUDE.md`](CLAUDE.md) for a tour of the
codebase conventions.
