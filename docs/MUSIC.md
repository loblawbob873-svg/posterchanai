# Music Generation (`musicgeni`)

Text-to-song generation with [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5), running
**natively in-process** on the same torch stack as image and video gen — no separate server.
Available in the **web UI** and **Telegram** (intentionally *not* the Misskey/Pleroma bots — abuse
surface).

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

## No separate server (was: the ACE-Step service)

Music generation is **in-process**: `diffusers` ships `AceStepPipeline`, so ACE-Step loads exactly
like the Wan/LTX video pipelines (`app/services/music_local.py`) — same venv, same torch, same GPU
lock.

It used to be a separate process, because ACE-Step needed Python 3.11–3.12 and a conflicting
torch/transformers stack, wasn't on PyPI, and shipped its own REST server. That meant a git clone
into `~/ACE-Step-1.5`, a `uv`-provisioned interpreter, an `acestep.service` systemd unit, an HTTP
hop, and hand-swapped torch on Intel XPU / AMD ROCm with torchcodec dropped. All of that is gone.

The turbo DiT is an 8-step model and fits a **12 GB** GPU. Weights land in `~/.cache/huggingface` on
first use (`./install.sh --music` can prefetch them).

**Falling back to an external server:** set `music_api_base` and the old REST path is used instead.
That exists for a diffusers too old to carry the pipeline, or a node still running the service —
nothing new depends on it.

## Load balancing, locking & VRAM swap (same as image gen)

`music_factory` mirrors `image_factory`, **node→node**:

- **`music_server_urls`** (Admin → Music) = OTHER posterchanai NODES (e.g. `http://nas.lan:3051`),
  not acestep servers. They're called via their `/api/generate-music` endpoint, which runs that
  node's own local generation — so the remote node frees ITS GPU (`prepare_for_music`) before its
  acestep generates. This is the same node→node pattern image gen uses (`/api/generate-image`).
- **`music_api_base`** = this node's own acestep server (`http://localhost:8001`). The local path
  takes the shared `GPUResourceLock` (so chat, image AND music all QUEUE on one GPU) and runs
  `vram_manager.prepare_for_music()` (unloads our LLM/image first).

Each request **round-robins across [remote nodes…, local]**, so songs spread over every node; a
failed node falls through to the next (and finally local). `_music_gen_lock` serializes music per
node so concurrent requests queue instead of OOMing one GPU. A node may safely list **itself** in
`music_server_urls` (a self-call just generates locally) — no loops.

> **Critical:** a single 12–16 GB GPU can't hold the chat/image models AND an acestep generation at
> once. That's why the local path **unloads** (`prepare_for_music`) and everything **queues** on the
> shared GPU lock — one model on the GPU at a time. acestep loads its model on demand (idle ≈ 0.5 GB).

## Setup

Nothing to install — `diffusers` is already in `requirements.txt`.

```bash
./install.sh --music     # optional: prefetch the weights + retire a legacy acestep.service
```

Then **Admin → Music**: enable music. Leave Local Server URL **blank** to use the built-in engine.
Set Remote Servers only if you want to fan out to other nodes.


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
