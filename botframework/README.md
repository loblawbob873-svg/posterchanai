# Posterchan

![Poster Chan AI](poster-chan.png)

AI-powered bots for the Fediverse. Supports Misskey, Pleroma, and Matrix.

## Quick Start

```bash
git clone https://git.poster.place/verita84/posterchan.git
cd posterchan
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 install.py   # creates bots_config.py and guides you through setup
```

On first run the installer generates `bots_config.py`, walks you through the
global settings (AI endpoint, image backend, database), and lets you add bots.

## Features

| Feature | AI | Posterchanai | Description |
|---------|----|--------------|-------------|
| **Chat Bot** | Required | No | AI-powered responses to mentions (Misskey/Pleroma/Matrix) |
| **Shamebot** | Optional | No | Roasts `:matrix.org` users on room join (opt-in per room, fallback insult) |
| **Image Generation** | No | Optional | Scheduled AI image posting via ComfyUI or posterchanai backend |
| **Voice Narration** | No | No | `/narrate` command generates video with avatar + TTS audio |
| **Blockbot** | No | No | Monitors and posts about user blocks (plain fallback) |
| **Unfollowbot** | No | No | Tracks and reports unfollows |
| **Welcome Bot** | No | No | Welcomes new users with custom messages (plain fallback) |
| **Report Bot** | No | No | Posts about user reports (plain fallback) |
| **Hashtag Bot** | No | No | Posts trending hashtags |
| **Web/Image Search** | Required | No | Search via SearXNG with AI summaries (`search` / `images`) |
| **News** | Required | No | Fetch headlines from a source on demand (`news <source>`) |
| **YouTube / X** | Optional | Required | Summarize a video (`yt`) or download its audio/video (`ytdl` / `ytdl video`); optionally `clip`/`compress` a video download in one command |
| **Screenshots** | No | No | Full-page website capture (`screenshot <url>`, also `shot` / `ss`) |
| **Media Tools** | No | No | On an uploaded file: `compress`, `clip <start> <end>`, or `convert` (images↔PDF) |
| **Social Posting** | Required | No | Turn a reply/link/topic into a post and `share` it to the fediverse (`post`) |
| **Translate** | Required | No | Translate a replied-to message to any language (`translate [language]`) |
| **Nitter Reposting** | No | No | Reposts new Nitter (Twitter) RSS items to a Matrix room or fediverse timeline |
| **Stickers** (Matrix) | No | No | Configured `!name` macros post a media file from `stickers/` — fire for anyone, no @mention (`!stickers` lists them) |
| **Fedi Timeline Room** (Matrix) | No | Required | In the configured timeline room, relays every message → new post, thread reply → reply, ❤/emoji → reaction, 🔁 → boost (under each member's own linked account via posterchanai); also relays notification-DM replies back to the fediverse |

## Fediverse Timeline Room (Matrix)

Posterchanai mirrors one Misskey/Pleroma timeline into a single Matrix room (configured in the
posterchanai admin). This bot is the Matrix half: for events in that room it forwards member
interactions to posterchanai's `/api/matrix/timeline-action` so each runs under the member's own
linked fediverse account.

- **`FEDI_TIMELINE_ROOM_ID`** in `config.py` (env `FEDI_TIMELINE_ROOM_ID`) selects the room; it
  must match posterchanai's `fedi_timeline_room_id` setting. Empty disables the handling.
- In that room (`_handle_timeline_event`): a top-level message → a new post; a thread reply → a
  reply (resolved to the thread root); a `🔁`/`♻`/`🚀` reaction → boost; any other reaction → a
  reaction passed through (Misskey keeps the exact emoji); an uploaded image/video → attached.
  Reply shortcuts: `boost`/`rt` → boost, `fav`/`like` → favourite, `quote <comment>` → quote-post.
  A pasted `matrix.to` message link (optionally with a comment) boosts/quotes the original.
- **Notification DMs** (from posterchanai's Matrix notification relay): a reply to a forwarded
  notification is sent to `/api/matrix/notification-reply` **before** the compress/convert media
  flow, so it posts back as a reply — text **or image** — or runs a `boost`/`fav` shortcut.

Both calls authenticate with `POSTERCHANAI_API_KEY` against `POSTERCHANAI_API_ENDPOINT`. The
`m.reaction` parsing and thread-root resolution live in `matrix_client.py` (`get_messages`).

## Configuration

All settings are in `bots_config.py`:

```python
# Global settings
AI_API_URL = "https://your-ai-server.com/api/chat/completions"
AI_API_KEY = "your-api-key"
COMFYUI_API_ENDPOINT = "http://192.168.0.85:8188"
SQL_USER = "root"
SQL_PASS = "your-password"

# Image bots - run at 0:00, 6:00, 12:00, 18:00
IMAGE_BOTS = {
    "miku": {
        "prompt": "Hatsune Miku, anime style",
        "host": "router",  # hostname where this runs
    },
}

# Text bots - run continuously
TEXT_BOTS = {
    "my-bot": {
        "server": "https://your-instance.com",
        "username": "botname",
        "access_token": "your-token",
        "platform": "misskey",  # or "pleroma"
        "modes": ['--misskey'],
        "host": "router",
    },
    # Daemon modes can run with listeners:
    "my-bot-blockbot": {
        "server": "https://your-instance.com",
        "username": "botname",
        "access_token": "your-token",
        "platform": "pleroma",
        "modes": ['--blockbot', '--pleroma'],  # Daemon + mention listener
        "sql_database": "my_db",
        "auto_narrate": True,  # Auto-generate video for all replies
        "host": "router",
    },
}
```

**Mode Combinations:** Listener modes (`--misskey`, `--pleroma`, `--nitter`) can run alongside daemon modes (`--blockbot`, `--welcome`, etc.) in a single bot entry.

## Available Modes

| Mode | Description |
|------|-------------|
| `--misskey` | Respond to Misskey mentions |
| `--pleroma` | Respond to Pleroma mentions |
| `--matrix` | Respond to Matrix messages |
| `--nitter` | Repost new Nitter RSS items to a room/timeline |
| `--image` | Post AI-generated images on schedule |
| `--blockbot` | Monitor and post about blocks |
| `--unfollowbot` | Monitor and post about unfollows |
| `--welcome` | Welcome new users |
| `--report` | Post about user reports |
| `--hashtagbot` | Post trending hashtags |
| `--ping` | Health check for AI endpoint |

## Managing Bots

### Updating

```bash
./install.sh --update
```

Pulls the latest code, upgrades dependencies, and restarts the `posterchan`
service (skips the interactive config). The bot has no GPU-pinned dependencies,
so upgrades are safe.

### Using the Installer

```bash
python3 install.py
```

Menu options:
- **Bot status** - View running/stopped bots with PIDs
- **Service control** - Start/stop/restart service
- **Manage bots** - Add, edit, remove bots, or run image bot once
- **View config** - Display current configuration
- **Deploy** - Install systemd service

### Manual Commands

```bash
# Start/stop service
sudo systemctl start posterchan
sudo systemctl stop posterchan
sudo systemctl restart posterchan

# View logs
sudo journalctl -u posterchan -f

# Check status
python3 botctl.py status
```

## Bot Commands

Users can trigger special commands by mentioning the bot:

| Command | Description |
|---------|-------------|
| `@bot /narrate <message>` | Generate AI reply with avatar video + TTS audio |
| `@bot search <query>` | Web search with AI-summarized results |
| `@bot images <query>` | Image search with downloads |
| `@bot geni <prompt>` | Generate AI image from prompt |
| `@bot regen <changes>` | Regenerate image in thread with modifications (img2img) |
| `@bot news <source>` | Fetch headlines; `share <n>` posts one to your platforms |
| `@bot yt <url>` | Summarize a YouTube video |
| `@bot ytdl <url>` | Download audio (`ytdl video <url>` for video); YouTube/X, on Matrix, Misskey & Pleroma |
| `@bot ytdl video <url> clip 0:10 0:30 compress` | Trim and/or shrink a video download in one command (clip runs first, then compress) |
| upload + `@bot compress` | Shrink the uploaded image(s)/video(s); bot posts the smaller file back |
| upload + `@bot clip <start> <end>` | Trim the uploaded video to a time span, e.g. `clip 0:10 0:30` (also `clip 90 120`) |
| upload + `@bot convert` | Image(s) → a single PDF, or a PDF → one image per page |
| `@bot screenshot <url>` | Full-page screenshot of a website (also `shot` / `ss`) |
| `@bot torrents <query>` · `nyaa <query>` | Search/manage torrents |
| `@bot post <url or text>` | Generate a social-media post (`share` to publish) |
| `@bot poll <question> \| <opt1> \| <opt2>` | Post a native Matrix poll (2-20 options) |
| reply + `@bot translate [language]` | Translate the message you replied to (default English) |

A bare YouTube/link/`magnet:` URL is auto-detected and offered for action. See the [full command reference](docs/ADVANCED.md#bot-commands).

**Sticker macros** (Matrix): need **no @mention** — anyone in the room can type `!<name>` and the bot posts the matching media file (image/video/audio). Enable it on the Matrix bot with a single flag; the available set is **auto-discovered** by scanning the `stickers/` folder, so there's nothing to list:

```python
"stickers_enabled": True,   # then drop files in stickers/ — mario.png → !mario
```

Drop in `mario.png` and `!mario` works instantly — no config edit, no restart. `!stickers` lists what's available. The `stickers/` folder is git-ignored, so files are per-host. See [Stickers](docs/ADVANCED.md#stickers).

**Matrix admin DM commands** (admins only, sent as a direct message): `join <!room:server>`, `leave <!room:server>`, `block <@user:server>`, `unblock <@user:server>`. The bot also auto-accepts any room invite. See [Matrix Admin Commands](docs/ADVANCED.md#matrix-admin-dm-commands).

See [Advanced Documentation](docs/ADVANCED.md#bot-commands) for detailed command usage and configuration.

## Prerequisites

- Python 3.10+
- PostgreSQL (for database features)
- OpenAI-compatible API (Ollama, Open-WebUI, etc.)
- ComfyUI (optional, for image generation)
- ffmpeg (for video narration)
- edge-tts (for voice synthesis)

See [Requirements](docs/ADVANCED.md#prerequisites) for complete dependency list.

## Getting Help

- **Configuration Issues**: See [Advanced Configuration](docs/ADVANCED.md#configuration)
- **TTS/Video Setup**: See [TTS/Video Configuration](docs/ADVANCED.md#ttsvideo-configuration)
- **Image Generation**: See [Image Regeneration](docs/ADVANCED.md#image-regeneration-regen)
- **Troubleshooting**: See [Troubleshooting Guide](docs/ADVANCED.md#troubleshooting)
- **Multi-Host Setup**: See [Multi-Host Setup](docs/ADVANCED.md#multi-host-setup)

## Advanced Topics

For detailed technical documentation, see:

- **[Advanced Configuration](docs/ADVANCED.md)** - TTS/Video setup, GPU acceleration, detailed settings
- **[Bot Commands](docs/ADVANCED.md#bot-commands)** - Complete command reference with examples
- **[Image Regeneration](docs/ADVANCED.md#image-regeneration-regen)** - Technical details on img2img
- **[Troubleshooting](docs/ADVANCED.md#troubleshooting)** - Common issues and solutions

## License

MIT License
