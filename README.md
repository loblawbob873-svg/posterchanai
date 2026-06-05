# Poster-chan AI

**Poster-chan AI** is a self-hosted AI assistant and chat platform with a modern web UI, optional local LLM and image generation, and a native Android app. Run it on your own hardware and connect your email, search, and codebases so one assistant can help with chat, search, and more.

The backend is **Python 3.10+** and **FastAPI**. You can use cloud APIs (OpenAI-compatible) or local backends (Ollama, llama.cpp, IPEX-LLM for Intel Arc). Image generation supports ComfyUI or a built-in diffusers backend. Everything is configurable via the web admin and optional install script.

---

## Features

### Chat & AI

- **Streaming chat** with multiple conversations, history, and optional markdown/formatting
- **OpenAI-compatible API** at `/v1/` for compatible clients and tools, including **function/tool calling** so agentic coding clients (e.g. opencode) can drive your local models
- **Local or remote LLM**: Ollama, llama-cpp-python (CPU/CUDA/HIP), or IPEX-LLM (Intel Arc)
- **Load balancing**: round-robin across multiple chat servers
- **Intent detection** and slash-style **commands** (e.g. `/mail`, `/image`, `/search`)

### Voice & media

- **Text-to-speech (TTS)** and **speech-to-text (STT)**; Edge TTS and configurable backends
- **Image generation**: ComfyUI (external) or native diffusers (SDXL); multiple image servers supported
- **Website screenshots**: full-page capture with the `screenshot <url>` command (also `shot` / `ss`) — works in the web UI, Telegram, and Matrix. Uses headless Chrome (JS-aware, so SPAs render), Firefox fallback (see [Requirements](#requirements)).
- **YouTube / X**: summarize a video **from its transcript** (so summaries and link-posts reflect the actual content, not the page), grab thumbnails, or **download** audio (MP3) / video with the `ytdl` command — in the web UI, Telegram, Matrix, Misskey, and Pleroma. A video download can be trimmed and/or shrunk in one command (`ytdl video <url> clip 0:10 0:30 compress`); Telegram also offers these as buttons after the download

### Knowledge & code

- **RAG (retrieval-augmented generation)** with ChromaDB and sentence-transformers: index git repos, folders, or zip uploads; code-aware chunking (Python, JS/TS, Go, Rust, etc.)
- **MCP server** (Model Context Protocol) for Continue.dev, Claude Desktop, and other clients—expose RAG search to your IDE

### PIM & productivity

- **Email**: read and send mail via IMAP/SMTP
- **News**: RSS-style news sources with summaries (`news` / `dailynews`)
- **Finance (Budget Manager)**: per-user budget summary, bills, and payments from chat (`budget`, `bills`, `pay`, `addbill`) against a self-hosted Budget Manager app
- **To-do**: quick personal task list from chat (`todo`)
- **Torrents**: built-in torrent client plus **TorrentGalaxy** search and **nyaa.si** anime search (`torrents`, `nyaa`)
- **File storage** per user and per conversation; file manager in the UI
- **Media tools**: upload a file and `compress` it (image/video — H.264 with GPU acceleration when available), `clip <start> <end>` a video to a time span, or `convert` images↔PDF — all shared across the web UI, Telegram, and Matrix

### Bots & social

- **Telegram and Matrix bots** drive chat, commands, and media from your phone
- **Social posting** to **Misskey**, **Pleroma/Mastodon**, and **Matrix**: turn any reply, link, or topic into a post with the `post` command (rewrite, verbatim, or with your own instructions). See [Social posting from the bots](#social-posting-from-the-bots).
- **Social notification relay**: forward mentions/replies/DMs from Misskey/Pleroma/Matrix to Telegram and reply right from the chat. See [Social notifications to Telegram](#social-notifications-to-telegram).
- **Fediverse timeline → Matrix room**: mirror one Misskey/Pleroma timeline (home/global/local) into a single Matrix room, with avatar + name, custom emoji, inline images (as captions), quote-posts, and **conversations grouped into Matrix threads** (replies thread under their parent; missing ancestors are backfilled). Members act straight from Element, each under their **own** linked fediverse account (resolved cross-instance by canonical AP URI):
  - **react** ❤/any emoji → favourite (Misskey keeps the exact emoji) · **🔁** → boost
  - **post** a top-level message → new status (with image) · **reply in a thread** → reply (auto-mentions the author)
  - **reply shortcuts**: `boost` / `fav` / `quote <comment>`
  - **share→boost/quote**: paste a post's matrix.to link (add a comment to quote) to boost/quote the original with the author preserved

  Configure under Admin → Services; the matching Matrix-bot handler lives in the separate [`posterchan`](https://git.poster.place/verita84/posterchan) repo. On a high-volume *global* feed, raise the bot's Synapse message rate limit (admin API `override_ratelimit`) so it keeps up.
- **Fediverse notifications → Matrix DM**: opt-in per user — the bot DMs you your Pleroma/Misskey notifications (mentions, replies, favourites, boosts, follows) in a private room, each with a 🔗 link and the **conversation mirrored into the message's thread** so you read context in Element. **Reply to a notification** to respond on the platform (text or image), or reply `boost`/`fav` to act on it.
- **Nitter post-cards**: per-user Nitter (X/Twitter) RSS feeds rendered as image "post cards" and delivered to your linked Telegram chat.
- **Translate**: translate text or a replied-to message to any language (`translate`), shared across the web UI, Telegram, and Matrix.

### Extensibility & admin

- **4chan** integration (optional)
- **Remote node management**: run OS commands across SSH-reachable machines from chat or Telegram, with long-running background jobs and a **tool-calling agentic mode** (one node or all nodes) that streams each step live. See [Remote node management](#remote-node-management).
- **Admin panel**: users, API keys, LLM/image/RAG/email settings, systemd service setup
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

### Option A: Installer (Linux, recommended)

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

### Option B: Manual setup (all platforms)

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
  - RAG, TTS/STT, email, plugins
- See **`docs/`** for detailed setup (IPEX, nginx, RAG/MCP, etc.).

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
installs it freezes the fragile IPEX pins (`torch`, `intel-extension-for-pytorch`,
`ipex-llm`, `numpy<2`, the transformers stack) to their installed versions first, so
an upgrade can never move them — a conflicting upgrade is skipped rather than allowed
to break the Arc environment.

> Note: `sync.sh` deploys **code** (git pull + restart); it does **not** touch Python
> deps or the Telegram server binary. Use **option 6** for dependency upgrades.

---

## Project layout

| Path | Description |
|------|-------------|
| `app/` | FastAPI app, routers (auth, chat, admin, TTS, STT, RAG, mail, torrent, etc.), services |
| `templates/` | Jinja2 HTML (login, chat, admin, modals) |
| `static/` | CSS, JS, icons, mascot assets |
| `run.py` | Server entry (uvicorn) |
| `requirements.txt` | Python dependencies |
| `install.sh` | Interactive installer (Linux) |
| `docs/` | [ADVANCED.md](docs/ADVANCED.md) (RAG, MCP, LLM, image, load balancing), IPEX, nginx, etc. |

---

## Android app

The **Android app** (native login, conversation list, streaming chat, optional “Web app” view) lives in **[`android/`](android/)** and talks to this backend.

- **Run:** Open the **`android`** folder in **Android Studio**, then **Run** (▶).
- Full instructions: **[android/README.md](android/README.md)**.

Set the app’s server URL to your instance (e.g. `http://YOUR_IP:3051`).

---

## Documentation

- **[docs/ADVANCED.md](docs/ADVANCED.md)** — RAG, MCP server, LLM backends, image generation, load balancing, Intel IPEX
- **docs/** — Email, nginx, and other feature documentation

---

## License and contributing

See the repository for license and contribution details.
