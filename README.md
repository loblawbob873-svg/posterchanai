# Poster-chan AI

AI chat application with a **Python/FastAPI backend**, **web UI**, optional local LLM and image generation, and an **Android app**. The server provides chat, RAG, calendar/contacts (CalDAV/CardDAV), file storage, and plugins.

## Requirements

- **Python 3.10+**
- (Optional) GPU and backends for local LLM (llama-cpp, Ollama, IPEX) and image generation (ComfyUI or native)

## Quick start (backend and web UI)

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

4. **Open the web UI** in a browser: **http://localhost:3051** (or your machine’s IP and port). Log in or register if enabled.

### Other ways to run

- **Port:** `python run.py --port 8080` or set `POSTERCHANAI_PORT=8080`
- **Install script (Linux):** `./install.sh` for interactive setup (GPU detection, LLM/image backends, systemd service). Use `./install.sh --help` and `./install.sh --packages` for options.
- **Start script:** `./start.sh` (if present) to launch the server.

## Project layout

| Path | Description |
|------|-------------|
| `app/` | FastAPI app, routers (auth, chat, admin, TTS/STT, RAG, mail, etc.), services |
| `templates/` | Jinja2 HTML (login, chat, admin, modals) |
| `static/` | CSS, JS, icons, mascot assets |
| `run.py` | Server entry (uvicorn) |
| `requirements.txt` | Python dependencies |
| `install.sh` | Interactive installer (Linux) |
| `docs/` | Extra docs (CalDAV, IPEX, nginx, etc.) |

## Android app

The **Android app** (native login, conversation list, chat with streaming) lives in **[`android/`](android/)** and talks to this backend.

- **Run the Android project:** Open the **`android`** folder in **Android Studio**, then use **Run** (▶).  
- Full instructions: **[android/README.md](android/README.md)**.

Configure the app with your server URL (e.g. `http://YOUR_IP:3051`).

## Configuration

- Copy `.env.example` to `.env` and adjust (optional).
- First run creates a SQLite DB; use the web UI or admin to set backends, API keys, and options.
- See `docs/` for advanced setup (CalDAV, IPEX, nginx, etc.).

## License and contributing

See the repository for license and contribution details.
