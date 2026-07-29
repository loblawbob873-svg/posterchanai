# Advanced Documentation

This document contains detailed technical documentation for Posterchan. For quick start and basic usage, see [README.md](../README.md).

## Bot Commands

Users can trigger special commands by mentioning the bot:

| Command | Description |
|---------|-------------|
| `@bot /narrate <message>` | Generate AI reply with avatar video + TTS audio |
| `@bot search <query>` | Web search with AI-summarized results |
| `@bot images <query>` | Image search with downloads |
| `@bot geni <prompt>` | Generate AI image from prompt |
| `@bot regen <changes>` | Regenerate image in thread with modifications (img2img) |
| `@bot news <source>` | Fetch headlines from a source, then `share <number>` to post one |
| `@bot yt <url>` | Summarize a YouTube video |
| `@bot ytdl <url>` | Download audio (`ytdl video <url>` for video) and post the file back — YouTube/X, on Pleroma — see note |
| `@bot ytdl video <url> clip 0:10 0:30 compress` | Trim and/or shrink a video download in one command — `clip <start> <end>` and/or `compress`, applied in that order — see note |
| upload + `@bot compress` | Shrink the uploaded image(s)/video(s); the bot posts the smaller file back |
| upload + `@bot clip <start> <end>` | Trim the uploaded video to a span, e.g. `clip 0:10 0:30` (seconds or M:SS / H:MM:SS) |
| upload + `@bot convert` | Image(s) → one PDF, or a PDF → one image per page |
| `@bot torrents <query>` | Search/manage torrents |
| `@bot nyaa <query>` | Search nyaa.si (anime) torrents |
| `@bot post <url or text>` | Generate a social-media post; `share` posts it to platforms |
| `@bot help` | List available commands |
| `@bot poll <question> \| <opt1> \| <opt2>` | Post a poll |
| reply + `@bot translate [language]` | Translate the replied-to message (see below) |

**Auto-detected (no command word needed):** a bare **YouTube URL** prompts for
summary / mp3 / video / post; a bare **link** prompts for summary / post; a bare
**`magnet:?` link** is added to torrents; a bare **number** selects a pending news
article or room.

Most of these are routed to the posterchanai backend over its command API;
`search`, `images`, `news`, `poll`, `translate`, and
image generation are handled in the bot itself.

**`ytdl` for everyone:** `ytdl` is identity-agnostic — it requires only the bot's
posterchanai **API key** (`POSTERCHANAI_API_KEY` in the bot config), not a linked
per-user account, so it works for *any* user in *any* room/thread and the media is
posted **as the bot**. The download itself runs on the posterchanai backend (one
shared `yt-dlp` path); audio (MP3) is the default and `ytdl video <url>` fetches
MP4 (capped at 1080p, ~95 MB to stay under the upload limit). Cookies/SSL come from
the global `ytdl_*` admin settings.

For a video download you can append `clip <start> <end>` and/or `compress` (e.g.
`ytdl video <url> clip 0:10 0:30 compress`) — the backend trims then compresses the
result before returning it, so the ~95 MB cap is applied to the *final* file (a clip
of a large source can therefore succeed). Times accept seconds or `M:SS` / `H:MM:SS`.
On **Telegram** the same actions are also offered as buttons after a `ytdl video`
download or the auto-detected 🎬 Movie prompt (Send as-is / Compress / Clip / Clip+Compress).

- The **Pleroma** listener calls the generic `/api/media/ytdl` endpoint
  (authenticated by `X-API-Key`, like the bots' `compress`/`screenshot` calls) and
  attach the result with `audio_bytes`/`video_bytes`.

## TTS/Video Configuration

The `/narrate` command creates an MP4 video with the bot's avatar and TTS audio. Videos are encoded with H.264 baseline profile for iPhone/Android compatibility. Configure per-bot:

```python
"my-bot": {
    ...
    "tts_voice": "en-US-RogerNeural",  # Deep male voice
    "tts_pitch": "-10Hz",               # Lower pitch
    "auto_narrate": True,               # Auto-narrate ALL replies (not just /narrate)
}
```

Auto-narrate generates video with TTS audio for all bot messages including:
- Chat bot replies (Pleroma)
- Blockbot notifications
- Welcome messages
- Unfollow notifications
- Report notifications

Uses `poster-chan.png` as fallback avatar if URL fetch fails.

### Available Voices

| Voice | Description |
|-------|-------------|
| `en-US-AnaNeural` | Young girl (cute, default) |
| `en-US-AriaNeural` | Young woman |
| `en-US-GuyNeural` | Adult male |
| `en-US-RogerNeural` | Adult male (deep, serious) |
| `en-US-ChristopherNeural` | Adult male (authoritative) |
| `en-GB-RyanNeural` | British male (stern) |
| `ja-JP-NanamiNeural` | Japanese female |

### GPU-Accelerated Video Encoding

TTS video generation supports hardware acceleration. Set the `VIDEO_ENCODER` environment variable:

| Value | Description |
|-------|-------------|
| `auto` | (default) Tries GPU encoders first, falls back to software |
| `h264_nvenc` | NVIDIA NVENC (requires NVIDIA GPU + drivers) |
| `h264_amf` | AMD AMF (requires AMD GPU + ROCm/AMDGPU drivers) |
| `h264_vaapi` | Intel/AMD VAAPI (Linux hardware acceleration) |
| `libx264` | Software encoding (always works, slower) |

Example in systemd service:
```ini
Environment="VIDEO_ENCODER=h264_nvenc"
```

Or in bots_config.py per-bot:
```python
"my-bot": {
    "video_encoder": "h264_amf",  # AMD GPU
    ...
}
```

## Image Regeneration (regen)

The `regen` command performs img2img transformation on images in the conversation thread:

```
@bot regen black skin
@bot regen blonde hair, blue eyes
@bot regen add sunglasses
```

**How it works:**
1. Finds the image in the thread (checks replied-to message first)
2. Gets tags via WD14 Tagger (strips character names that override changes)
3. Applies weighted modifications `(your request:1.3)` for stronger effect
4. Auto-detects anime (higher denoise) vs realistic (lower denoise)
5. Generates new image using img2img

**Denoise Values (auto-detected by AI):**
| Modification | Denoise | Example |
|--------------|---------|---------|
| Body size | 0.50 | `big breasts`, `small breasts` |
| Combined (nude+body) | 0.65 | `nude small breasts` |
| Style | 0.65 | `anime`, `realistic` |
| Objects | 0.70 | `holding gun`, `holding coffee` |
| Background | 0.75 | `beach`, `city` |
| Color/Naked | 0.80 | `red hair`, `black skin`, `naked` |

**Tips:**
- Use commas to separate multiple changes: `regen dark skin, red hair`
- Add `,anime` to force anime model: `regen red dress ,anime`
- Add `,d0.7` to set denoise strength: `regen nude ,d0.7`
- Combine modifiers: `regen cat ears ,anime ,d0.6`
- Works with any image (bot-generated or external)
- Supports PNG, JPEG, WebP, and GIF formats (GIF uses first frame)
- Avoid images with text overlays (text gets corrupted)

## Configuration

### Multi-Host Setup

Bots can run on different hosts. Set the `host` field in each bot config:

```python
TEXT_BOTS = {
    "bot1": {"host": "router", ...},  # Runs on router
    "bot2": {"host": "nas", ...},     # Runs on nas
}
```

Only bots matching the current hostname will start.

### Per-Bot Prompt & Image Overrides

The daemon bots (blockbot, welcome, report, unfollow) ship with sensible default
prompts and images defined in `config.py`. To override them for a specific bot,
set the matching key in that bot's `bots_config.py` entry. If a key is omitted,
the `config.py` default is used.

| Bot mode | Per-bot keys |
|----------|--------------|
| `--blockbot` | `block_image`, `block_prompt` |
| `--welcome` | `welcome_image`, `welcome_message`, `welcome_prompt`, `welcome_lookback_minutes` |
| `--report` | `report_image`, `report_prompt` |
| `--unfollowbot` | `unfollow_image`, `unfollow_silent_mode` |

```python
"posterchan-report": {
    "platform": "pleroma",
    "modes": ['--report'],
    "report_image": "/home/verita84/posterchan/bot.png",
    "report_prompt": "Summarize this user report ... Report details: {report_details}",
    ...
}
```

These are passed to the bot as the corresponding uppercase environment variables
(`BLOCK_PROMPT`, `WELCOME_PROMPT`, `REPORT_IMAGE`, etc.).

### Nitter RSS Reposting

The `--nitter` mode reposts new items from [Nitter](https://github.com/zedeus/nitter)
RSS feeds (a Twitter/X front-end) to the bot's
**fediverse timeline** (Pleroma). Feeds are listed in the bot's
`nitter_feeds` array:

```python
"nitter_feeds": [
    {"rss": "https://nitter.net/PoweroftheTruth/rss"},
    {"rss": "https://nitter.net/Andywarski/rss"},
],
"nitter_poll_seconds": 300,   # how often to check (default 300)
```

Add `--nitter` to the bot's `modes`. It can run **alongside** the main listener,
e.g. `"modes": ['--pleroma', '--nitter']` runs the Pleroma chat bot and the
reposter in the same process.

Behavior:

- **Destination:** each feed posts to the fediverse account this bot is configured for.
- **No backlog flood:** the first time a feed is seen, its current items are
  recorded as "seen" without posting. Only genuinely new items posted afterward
  are sent. Seen state lives in `.nitter_seen.json`.
- Each host tracks its own seen-state, so the same feed on two hosts is independent.
- `nitter_poll_seconds` is clamped to a 60s minimum.

> **Note:** public Nitter instances are frequently rate-limited or down. If posts
> stop, it's usually the instance — point the feed at a working one.

### Running Without AI

Most features work without OpenAI configured:

| Feature | Without AI |
|---------|------------|
| Chat Bot | **Requires AI** |
| Blockbot | Posts plain message: "@user blocked @user2" |
| Welcome Bot | Uses `welcome_message` from config |
| Shamebot | Uses hardcoded insult fallback |
| Report Bot | Posts: "New user report: {details}" |
| Image Generation | Works (uses ComfyUI, not OpenAI) |

## Prerequisites

### Required

- Python 3.10+
- PostgreSQL (for database features)
- OpenAI-compatible API (Ollama, Open-WebUI, etc.)
- Pillow (for GIF/image format conversion)
- ffmpeg with libx264 (for mobile-compatible video narration)
- edge-tts (for voice synthesis)

### Optional

- ComfyUI (for image generation)
- ffmpeg with h264_nvenc/h264_amf (for GPU-accelerated encoding)

## Troubleshooting

**Bots not starting?**
- Check `python3 botctl.py status`
- View logs: `sudo journalctl -u posterchan -f`

**Database errors?**
- Verify SQL_USER, SQL_PASS, SQL_DATABASE in config
- For Unix socket, leave SQL_HOST empty

**Image generation fails?**
- Check ComfyUI is running: `curl http://192.168.0.85:8188/system_stats`
- Verify model files exist

**Video encoding issues?**
- Verify ffmpeg is installed: `ffmpeg -version`
- Check GPU encoder support: `ffmpeg -encoders | grep h264`
- For NVIDIA: Ensure nvidia-smi shows GPU
- For AMD: Verify ROCm/AMDGPU drivers are installed
- Fall back to software encoding: Set `VIDEO_ENCODER=libx264`

**TTS not working?**
- Verify edge-tts is installed: `pip install edge-tts`
- Test voice: `edge-tts --list-voices | grep "en-US"`
- Check internet connection (edge-tts requires online access)

**AI API errors?**
- Verify AI_API_URL and AI_API_KEY are correct
- Test connection: `curl -H "Authorization: Bearer $AI_API_KEY" $AI_API_URL`
- Check API endpoint supports OpenAI-compatible format
