# Music Generation (`musicgeni`)

Text-to-song generation via a self-hosted [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5)
server. Available in the **web UI** and **Telegram** (intentionally *not* the Misskey/Pleroma/Matrix
bots — abuse surface).

```
musicgeni <description>                 # auto-writes lyrics → a song WITH vocals
musicgeni <style>, instrumental         # no vocals
musicgeni <style> | <your own lyrics>   # supply exact lyrics ([verse]/[chorus] tags work)
```

Examples:
- `musicgeni a gangster rap about self-hosting your own AI` → vocal rap (lyrics auto-written)
- `musicgeni dreamy lofi piano, instrumental` → instrumental
- `musicgeni pop punk anthem | I won't back down tonight / we're gonna make it right`

## Output: a branded video

Each song is delivered as an **MP4**, not raw audio: the song plays over a generic PosterChan
background, then the branded end-card outro (the same "watermark" used on effect videos) is
appended. The web UI shows an inline `<video>` player with a download button; Telegram sends it as
a video. The prompt text is **not** shown on the background. If video wrapping isn't possible (e.g.
no `ffmpeg`), it falls back to delivering the raw audio. The end-card is gated by
`music_watermark_enabled`.

## Vocals

Vocals require lyrics. When you don't pass `| lyrics`, PosterChanAI's own LLM writes lyrics (and a
style caption) from your description, so plain `musicgeni <description>` produces a song with
singing. Add `instrumental` / `no vocals` to the description (or end with a bare `|`) to skip
lyrics.

## Why a separate ACE-Step server?

ACE-Step needs **Python 3.11–3.12** and a `torch`/`transformers` stack that conflicts with both the
main app venv and the image-gen venv. It also ships its own REST server (`acestep-api`). So it runs
as a **separate process**, and the app talks to it over HTTP — like the external image servers and
the Budget Manager. The main app needs no extra Python deps (just `httpx`). See
`app/services/music_service.py` (per-server REST client) and `app/services/music_factory.py`
(orchestration). ACE-Step is **not on PyPI** — it's installed from its git repo with `uv`.

ACE-Step's turbo DiT model fits a **12 GB** GPU (e.g. an RTX 3060 renders a ~15 s clip in ~10 s,
generating two takes per request).

## Load balancing, locking & VRAM swap (same as image gen)

`music_factory` mirrors `image_factory`:

- **Remote servers** — `music_server_urls` (Admin → Music): comma-separated ACE-Step servers on
  other nodes, tried round-robin. No local GPU lock (they own their own GPUs).
- **Local server** — `music_api_base`: a co-located ACE-Step server. Requests to it take the shared
  `GPUResourceLock` (so only one GPU task runs at a time — chat, image, *or* music) and
  `vram_manager.prepare_for_music()` swaps the LLM/image model out of VRAM first. Skipped in
  `dedicated` VRAM mode.

> **Per-node config:** each node's **Local** URL should be its own `localhost:8001`, and its
> **Remote** list should contain the *other* nodes — never itself (that would bypass the local GPU
> lock/swap).

## Setup

### Bare metal

```bash
./install.sh --music
```

Installs `uv` (user-local), clones ACE-Step, and runs `uv sync` (provisions Python 3.12 + a CUDA
torch wheel + all deps). Start the server:

```bash
cd ~/ACE-Step-1.5 && ACESTEP_API_HOST=0.0.0.0 ACESTEP_API_PORT=8001 uv run acestep-api
```

The DiT model auto-downloads on the first song. **NVIDIA/CUDA works out of the box.** For AMD ROCm
or Intel XPU, reinstall torch from the matching index after `uv sync` (and matching
`torchvision`/`torchaudio` — ACE-Step pins CUDA-era versions, so a plain torch swap can break the
`torchvision`/`torchaudio` ABI).

### Docker

The `acestep` service is opt-in via the `music` profile (NVIDIA only; needs nvidia-container-toolkit):

```bash
docker compose --profile cuda --profile music up -d --build
```

Then in **Admin → Music**: enable music and set the Local Server URL to `http://acestep:8001`. The
model auto-downloads into the `pc-acestep` volume on first request. Pin a repo ref with
`ACESTEP_REF` if upstream changes.

## Admin → Music settings

| Setting | Meaning |
|---|---|
| `music_enabled` | Master on/off (default off) |
| `music_api_base` | Local ACE-Step server URL (GPU-locked + VRAM-swapped) |
| `music_server_urls` | Remote ACE-Step servers, round-robin (no local lock) |
| `music_gpu_device` | `auto`/`cuda`/`xpu`/`cpu` — GPU vs CPU lock for the local server |
| `music_model` | DiT model name/path (blank = server default, `acestep-v15-turbo`) |
| `music_default_duration` | Seconds (10–600) |
| `music_default_steps` | Diffusion steps (turbo ≈ 8, base up to ~200) |
| `music_format` | `mp3`/`wav`/`flac`/`opus`/`aac` |
| `music_timeout` | Request timeout (ms) |
| `music_watermark_enabled` | Append the branded end-card to the song video |

## REST contract (validated against ACE-Step 1.5)

```
POST /release_task   {prompt, lyrics, audio_duration, inference_steps, format}
                     -> {"data": {"task_id": "...", "status": "queued"}, "code": 200}
POST /query_result   {"task_id_list": ["..."]}     # note: task_id_LIST, not task_ids
                     -> {"data": [{"task_id","result": "<JSON string>", "status"}]}
                        result (parsed) = [{"file": "/v1/audio?path=...", "stage": "succeeded", ...}]
GET  /v1/audio?path=...                              -> audio bytes
```

> ACE-Step's repo can change between releases. If a build/download/parse fails, check the
> [upstream repo](https://github.com/ace-step/ACE-Step-1.5).
