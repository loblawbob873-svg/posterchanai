<div align="center">

<img src="static/mascot/mascot-happy-front-05.png" alt="Poster-chan mascot" width="200" />

# Poster-chan AI

### Your own AI assistant — self-hosted, private, and ridiculously capable.

One FastAPI backend that does chat, image generation, voice, email, news, torrents, and runs autonomous bots on **Telegram, Matrix, Misskey & Pleroma**. Cloud LLMs or fully local. Your hardware, your data, your rules.

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![SQLite](https://img.shields.io/badge/SQLite-backed-003B57?logo=sqlite&logoColor=white)](https://www.sqlite.org/)
[![OpenAI-compatible](https://img.shields.io/badge/API-OpenAI--compatible-412991?logo=openai&logoColor=white)](#chat--ai)
[![Self-hosted](https://img.shields.io/badge/Self--hosted-100%25-success)](#quick-start-backend-and-web-ui)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20GPU%20optional-FCC624?logo=linux&logoColor=black)](#requirements)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

[Quick start](#quick-start-backend-and-web-ui) ·
[Features](#features) ·
[Bots & social](#bots--social) ·
[Documentation](#documentation)

</div>

---

## Why Poster-chan?

- 🏠 **Truly self-hosted** — runs on your own box, SQLite-backed, no telemetry, single-admin multi-user. Your conversations and keys never leave your network.
- 🔌 **Bring any model** — cloud (any OpenAI-compatible API) or local: **Ollama** or **llama.cpp** (CPU / CUDA / HIP / **Intel Arc SYCL**). Round-robin load-balance across several backends.
- 🤖 **It's also a bot platform** — drive everything from **Telegram & Matrix**, and run autonomous **Pleroma / Misskey / Matrix** bots from a single admin tab.
- 🎨 **More than chat** — image generation, TTS/STT, website screenshots, YouTube/X summarize & download, media tools, interactive study flashcards, email, news, finance, torrents — all behind one chat box.
- 🛠️ **Hackable & honest** — thin routers, services for logic, an interactive installer, and an OpenAI-compatible `/v1/` endpoint that agentic coding clients (e.g. opencode) can drive against your local models.

> Point any OpenAI-compatible tool at `http://your-box:3051/v1/` and you've got a private, function-calling-capable model server. Open the web UI and you've got a full assistant. Link a bot and it's in your pocket.

---

## Features

### Chat & AI

- **Streaming chat** with multiple conversations, history, and optional markdown/formatting
- **OpenAI-compatible API** at `/v1/` for compatible clients and tools, including **function/tool calling** so agentic coding clients (e.g. opencode) can drive your local models
  - **Recommended coding model:** [Qwen3-Coder-30B-A3B-Instruct](https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF) (`IQ4_XS`, MoE ~3B active, ~16GB) — by far the most reliable local model here for multi-step agentic builds; it 1-shots small apps where 8–14B models stall. Needs a 12GB+ GPU (partial CPU offload on 12–16GB cards; leave `ollama_num_ctx` on `auto`). For smaller GPUs, `Qwen3.5-9B-Claude-Code` is the lightweight fallback.
- **Local or remote LLM**: Ollama, or llama-cpp-python (CPU / CUDA / HIP / Intel Arc SYCL)
- **Load balancing**: round-robin across multiple chat servers
- **Intent detection** and slash-style **commands** (e.g. `/mail`, `/image`, `/search`)

### Voice & media

- **Text-to-speech (TTS)** and **speech-to-text (STT)**; Edge TTS and configurable backends
- **Image generation**: ComfyUI (external) or native diffusers (SDXL); multiple image servers supported
- **Music generation** (`musicgeni`): text-to-song via a self-hosted [ACE-Step](https://github.com/ace-step/ACE-Step-1.5) server (fits a 12 GB GPU); load-balanced + VRAM-swapped like image gen, with a spoken watermark. Web UI + Telegram. See [docs/MUSIC.md](docs/MUSIC.md)
- **Video generation** (`videogeni`): native in-process text-to-video via **diffusers** — point it at *any* model (Wan2.1 / LTX / CogVideoX, auto-detected) to match your GPU; runs on CUDA / Intel Arc (XPU) / AMD (ROCm), load-balanced + VRAM-swapped across nodes like image gen, with a branded watermark and optional 720p/1080p upscale. Web UI + Telegram. See [docs/VIDEO.md](docs/VIDEO.md)
- **Website screenshots**: full-page capture with the `screenshot <url>` command (also `shot` / `ss`) — works in the web UI, Telegram, and Matrix. Uses headless Chrome (JS-aware, so SPAs render), Firefox fallback (see [Requirements](#requirements)).
- **YouTube / X**: summarize a video **from its transcript** (so summaries and link-posts reflect the actual content, not the page), grab thumbnails, or **download** audio (MP3) / video with the `ytdl` command — in the web UI, Telegram, Matrix, Misskey, and Pleroma. A video download can be trimmed and/or shrunk in one command (`ytdl video <url> clip 0:10 0:30 compress`); Telegram also offers these as buttons after the download

### PIM & productivity

- **Email**: read and send mail via IMAP/SMTP
- **News**: RSS-style news sources with summaries (`news` / `dailynews`)
- **Finance (Budget Manager)**: per-user budget summary, bills, and payments from chat (`budget`, `bills`, `pay`, `addbill`) against a self-hosted Budget Manager app
- **To-do**: quick personal task list from chat (`todo`)
- **Torrents**: built-in torrent client plus **TorrentGalaxy** search and **nyaa.si** anime search (`torrents`, `nyaa`)
- **File storage** per user and per conversation; file manager in the UI
- **Media tools**: upload a file and `compress` it (image/video — H.264 with GPU acceleration when available), `clip <start> <end>` a video to a time span, or `convert` images↔PDF — all shared across the web UI, Telegram, and Matrix
- **Flashcards (study tool)**: upload a **PDF, image, slide deck (PPTX) or Word doc (DOCX)** and send `flashcards` (or `cards`/`study`/`quiz`) to generate an **interactive multiple-choice quiz** — the LLM writes the questions, options and explanations (math problems include worked steps). The web UI shows animated cards with instant ✓/✗ feedback and KaTeX-rendered math; Telegram shows image cards with answer buttons (tap **🎴 Flashcards** after uploading) and a running score. Text PDFs/slides work best; image OCR is weaker (on Telegram, send screenshots as a *file*, not a compressed photo)
- **Reminders** (`remind`): set a reminder in plain language — `remind open the oven in 10m`, `remind me next tuesday to call mom` — and the LLM parses the time. A background scheduler alerts you in the **web UI** (a dedicated "⏰ Reminders" conversation + a live pop-up if you're online) and on **Telegram** if linked. `reminders` lists your pending ones, each with a clickable **Cancel**.

### Bots & social

- **Bot manager (Admin → Bots)**: run autonomous fediverse bots — Pleroma/Misskey reply bots,
  the Matrix bot, nitter relays, plus blockbot/welcome/report/hashtag/unfollow daemons — from a
  single admin tab (add/edit, On/Off, live status), backed by the database. The bot framework is
  **bundled in this repo** (`botframework/`) and supervised in-process; no separate repo or
  hand-edited config file. See [Bot manager](docs/BOTS.md).
- **Telegram and Matrix bots** drive chat, commands, and media from your phone
- **Social posting** to **Misskey**, **Pleroma/Mastodon**, and **Matrix**: turn any reply, link, or topic into a post with the `post` command (rewrite, verbatim, or with your own instructions). See [Social posting from the bots](#social-posting-from-the-bots).
- **Social notification relay**: forward mentions/replies/DMs from Misskey/Pleroma/Matrix to Telegram and reply right from the chat. See [Social notifications to Telegram](#social-notifications-to-telegram).
- **Fediverse timeline → Matrix room**: mirror one Misskey/Pleroma timeline (home/global/local) into a single Matrix room, with avatar + name, custom emoji, inline images (as captions), quote-posts, and **conversations grouped into Matrix threads** (replies thread under their parent; missing ancestors are backfilled). Members act straight from Element, each under their **own** linked fediverse account (resolved cross-instance by canonical AP URI):
  - **react** ❤/any emoji → favourite (Misskey keeps the exact emoji) · **🔁** → boost
  - **post** a top-level message → new status (with image) · **reply in a thread** → reply (auto-mentions the author)
  - **reply shortcuts**: `boost` / `fav` / `quote <comment>`
  - **share→boost/quote**: paste a post's matrix.to link (add a comment to quote) to boost/quote the original with the author preserved

  Configure under Admin → Services; the matching Matrix-bot handler is bundled in `botframework/`
  (`matrixListener.py`) and run by the **Bot manager** (Admin → Bots) — see [docs/BOTS.md](docs/BOTS.md).
  On a high-volume *global* feed, raise the bot's Synapse message rate limit (admin API
  `override_ratelimit`) so it keeps up.
- **Fediverse notifications → Matrix DM**: opt-in per user — the bot DMs you your Pleroma/Misskey notifications (mentions, replies, favourites, boosts, follows) in a private room, each with a 🔗 link and the **conversation mirrored into the message's thread** so you read context in Element. **Reply to a notification** to respond on the platform (text or image), or reply `boost`/`fav` to act on it. **Direct messages** stay private — a received DM shows up here (not the shared room) and replying keeps it direct; send a new one with `dm @user@host <message>` (Pleroma/Mastodon).
- **Nitter post-cards**: per-user Nitter (X/Twitter) RSS feeds rendered as image "post cards" and delivered to your linked Telegram chat.
- **Translate**: translate text or a replied-to message to any language (`translate`), shared across the web UI, Telegram, and Matrix.

### Extensibility & admin

- **4chan** integration (optional)
- **Remote node management**: run OS commands across SSH-reachable machines from chat or Telegram, with long-running background jobs and a **tool-calling agentic mode** (one node or all nodes) that streams each step live. See [Remote node management](#remote-node-management).
- **Admin panel**: users, API keys, LLM/image/email settings, systemd service setup
- **Multi-user** with registration (optional), email verification, and quotas

---

## Requirements

- **Python 3.10+**
- (Optional) **GPU** and backends for local LLM (Ollama, llama-cpp-python, IPEX-LLM) and image generation (ComfyUI or native diffusers)
- (Optional) **Headless Chrome/Chromium** for the `screenshot` command (driven over the DevTools protocol — full-page and JS-aware, so SPAs render instead of coming out blank; no Selenium/chromedriver). Firefox is used as a fallback if Chrome is absent:
  - Gentoo: `emerge www-client/google-chrome` (or `www-client/chromium`)
  - Debian/Ubuntu: `apt install chromium` (or install `google-chrome-stable`)

---

## Quick start (backend and web UI)

### Option A: Docker (turnkey — one image, any GPU)

One Ubuntu image builds for **CPU, NVIDIA (CUDA), AMD (ROCm), or Intel Arc (XPU)** —
pick the accelerator with a build-arg. It comes up **turnkey**: native local LLM +
image backends, auto-downloads the recommended chat model on first run, and (on AMD)
auto-detects the GPU override and persists the MIOpen kernel cache for fast image gen.

```bash
# build for your accelerator:  cpu | cuda | rocm | intel
docker build -t posterchanai:rocm --build-arg GPU=rocm .

# run (AMD shown — see docs/DOCKER.md for the cuda/intel/cpu run flags)
docker run -d --device /dev/kfd --device /dev/dri --security-opt seccomp=unconfined \
  -p 3051:3051 -v pc-data:/var/lib/posterchanai -v pc-rag:/app/data posterchanai:rocm
```

Open **http://localhost:3051** and log in with **`admin` / `admin`**. The GPU kernel
driver comes from the host (CUDA toolkit / `amdgpu` / `i915`); the userspace + a
GPU-compiled `llama-cpp` are baked into the image. Full matrix — GPU run flags, model
auto-download, opt-ins (Tor/proxy/torrenting), and the opencode/OpenAI-client config —
in **[docs/DOCKER.md](docs/DOCKER.md)**.

### Option B: Installer (Linux, recommended for bare metal)

The **installer** sets up the virtual environment, dependencies, optional GPU backends (LLM and image), and can configure a systemd service.

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
   Follow the prompts: it checks dependencies, detects your GPU, lets you choose LLM backend (Ollama, llama-cpp, etc.) and image backend (ComfyUI or native), creates the venv, installs Python deps, and optionally sets up a systemd service.

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
- First run creates a **SQLite** database; use the **web UI** and **Admin** panel to configure:
  - LLM backend (Ollama, llama-cpp, IPEX) and model
  - Image generation (ComfyUI URL or native)
  - TTS/STT, email, plugins
- See **`docs/`** for detailed setup (IPEX, nginx, etc.).

### Social posting from the bots

Connect a social account in **User Settings → Misskey / Pleroma / Matrix**, then use
the `post` command from the **Telegram** or **Matrix** bot to publish.

**Telegram** — reply to any message (a bot answer, a link, a photo) and send:

| Command | What it does |
| --- | --- |
| `post` | Rewrites the replied-to content into a viral, engaging post |
| `post raw` | Shares the reply **exactly as written** — no rewrite (aliases: `verbatim`, `as-is`, `exact`) |
| `post <instructions>` | Rewrites following your instructions, e.g. `post professional`, `post funny and short`, `post don't include links` |

The bot then shows share buttons (**📣 Misskey / Pleroma / Matrix**, **🚀 Post to All**,
**❌ Skip**) for whichever platforms you've connected. Replying to a photo shares the
image itself. The source URL is appended by default; `post don't include links` (or
"no links", "without url", …) omits it.

**Matrix** — reply to a message (mention the bot) **or** type the content inline:

| Command | What it does |
| --- | --- |
| reply + `post` | Rewrites the replied-to message into a post |
| reply + `post raw` | Shares the replied-to message verbatim |
| reply + `post <instructions>` | Rewrites the replied-to message following your instructions |
| `post <url>` | Fetches the link and writes a post (URL appended) |
| `post <topic>` | Writes a post about the topic |
| `post raw <text>` | Saves `<text>` verbatim, no rewrite |
| `share` | Publishes the last `post` to your connected platforms (`share matrix <n>` picks a room) |

### Social notifications to Telegram

Forward new notifications from your connected **Misskey / Pleroma / Matrix** accounts to your
linked Telegram chat, and reply to them without leaving Telegram. Enable it per-user in
**User Settings → Telegram → "Relay social notifications to Telegram"** (the admin must also
turn on the global switch in **Admin → Services → Social Notification Relay**, where the poll
interval is set).

- New mentions, replies, DMs, follows, and reactions/boosts are forwarded as they arrive.
- **Reply** to a forwarded message in Telegram to respond on the originating platform — your
  reply is posted as a reply to the original post (inheriting its visibility) or sent into the
  Matrix room.
- **Pleroma / Misskey:** DMs are direct-visibility mentions, so they arrive as normal
  notifications and forward with full content; your reply stays `direct`.
- **Matrix** forwarding rules: **DM rooms** forward every incoming message; **group rooms**
  forward only messages that mention you; your own messages are never forwarded, and the first
  poll sets a cursor without backfilling history.
- **Encrypted (E2EE) Matrix DMs** can't be decrypted (the relay has no E2EE support), so instead
  of the content you get a **"🔒 You received an encrypted message — open Element"** notice (one
  per room per poll). Since DMs are encrypted by default in most clients, this is the common
  case; unencrypted DMs forward in full.
- **Misskey** must be **re-connected once** (User Settings → Misskey) so the new token includes
  the `read:notifications` permission. Pleroma and Matrix need no changes.

### Remote node management

Run OS commands on a fleet of machines from chat or Telegram. Enable it in
**Admin → Services → Remote Node Management**, list your nodes (one per line as
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
and Matrix have no such limit). To lift this to **~2 GB**, run a local Bot API server:

1. In **Admin → Services → Telegram Bot**, enter your **API ID** and **API Hash**
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
| `botframework/` | Merged autonomous bot framework (Pleroma/Misskey/Matrix/nitter listeners + daemons); spawned by `app/services/bot_manager_service.py`. See [docs/BOTS.md](docs/BOTS.md) |
| `templates/` | Jinja2 HTML (login, chat, admin, modals) |
| `static/` | CSS, JS, icons, mascot assets |
| `run.py` | Server entry (uvicorn) |
| `requirements.txt` | Python dependencies |
| `install.sh` | Interactive installer (Linux) |
| `docs/` | [ADVANCED.md](docs/ADVANCED.md) (LLM, image, load balancing), IPEX, nginx, etc. |

---

## Documentation

- **[docs/DOCKER.md](docs/DOCKER.md)** — Turnkey Docker image (CPU / NVIDIA / AMD / Intel Arc): build matrix, GPU run flags, model auto-download, opt-ins, and OpenAI-client/opencode setup
- **[docs/BOTS.md](docs/BOTS.md)** — Bot manager: the merged `botframework/`, Admin → Bots, per-bot config, the single server endpoint, and per-node cutover
- **[docs/ADVANCED.md](docs/ADVANCED.md)** — LLM backends, image generation, load balancing, Intel IPEX
- **docs/** — Email, nginx, and other feature documentation

---

## License

**Poster-chan AI** is free software, released under the **[GNU General Public License v3.0](LICENSE)**.
You may use, study, share, and modify it — and if you distribute a modified version, you must
pass on the same freedoms under the GPLv3. See [`LICENSE`](LICENSE) for the full text.

## Contributing

Issues and pull requests are welcome. Keep changes small and consistent with the existing
patterns (thin routers, logic in services); see [`CLAUDE.md`](CLAUDE.md) for a tour of the
codebase conventions.
