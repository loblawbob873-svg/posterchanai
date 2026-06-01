# Poster-chan AI

**Poster-chan AI** is a self-hosted AI assistant and chat platform with a modern web UI, optional local LLM and image generation, and a native Android app. Run it on your own hardware and connect your email, search, and codebases so one assistant can help with chat, search, and more.

The backend is **Python 3.10+** and **FastAPI**. You can use cloud APIs (OpenAI-compatible) or local backends (Ollama, llama.cpp, IPEX-LLM for Intel Arc). Image generation supports ComfyUI or a built-in diffusers backend. Everything is configurable via the web admin and optional install script.

---

## Features

### Chat & AI

- **Streaming chat** with multiple conversations, history, and optional markdown/formatting
- **OpenAI-compatible API** at `/v1/` for compatible clients and tools
- **Local or remote LLM**: Ollama, llama-cpp-python (CPU/CUDA/HIP), or IPEX-LLM (Intel Arc)
- **Load balancing**: round-robin across multiple chat servers
- **Intent detection** and slash-style **commands** (e.g. `/mail`, `/image`, `/search`)

### Voice & media

- **Text-to-speech (TTS)** and **speech-to-text (STT)**; Edge TTS and configurable backends
- **Image generation**: ComfyUI (external) or native diffusers (SDXL); multiple image servers supported
- **YouTube** summarization and thumbnails in chat

### Knowledge & code

- **RAG (retrieval-augmented generation)** with ChromaDB and sentence-transformers: index git repos, folders, or zip uploads; code-aware chunking (Python, JS/TS, Go, Rust, etc.)
- **MCP server** (Model Context Protocol) for Continue.dev, Claude Desktop, and other clients—expose RAG search to your IDE

### PIM & productivity

- **Email**: read and send mail via IMAP/SMTP
- **News**: RSS-style news sources with summaries
- **Torrents**: built-in torrent client
- **File storage** per user and per conversation; file manager in the UI

### Extensibility & admin

- **4chan** integration (optional)
- **Admin panel**: users, API keys, LLM/image/RAG/email settings, systemd service setup
- **Multi-user** with registration (optional), email verification, and quotas

---

## Requirements

- **Python 3.10+**
- (Optional) **GPU** and backends for local LLM (Ollama, llama-cpp-python, IPEX-LLM) and image generation (ComfyUI or native diffusers)

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

### Large files on Telegram (optional)

The cloud Telegram Bot API limits the bot to downloading files up to **20 MB**, so
`compress`/`convert` only work on small uploads there (the web UI and Matrix have no
such limit). To lift this to **~2 GB**, run a local Bot API server:

1. In **Admin → Services → Telegram Bot**, enter your **API ID** and **API Hash**
   (from https://my.telegram.org) and save.
2. On the bot host, run once — it reads those values from the database, nothing to type:
   ```bash
   sudo ./scripts/setup-telegram-local-api.sh
   ```
3. Back in the admin UI, tick **Use local Bot API server**, set the URL to
   `http://localhost:8081`, save, and re-run **Setup Webhook**.

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
