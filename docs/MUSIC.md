# Music Generation (`musicgeni`)

Text-to-song generation with [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5), generated
**in-process** — on the app's own venv, torch and GPU lock, exactly like video generation. There is
no `acestep.service`, no second venv and no HTTP hop. Available in the **web UI** and **Telegram**
(intentionally *not* the Misskey/Pleroma bots — abuse surface).

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

## Why a separate ACE-Step server

ACE-Step needs **Python 3.11–3.12** and a `torch`/`transformers` stack that conflicts with the main
app venv, and it ships its own REST server. So it runs as its own systemd service and the app is an
HTTP client. `./install.sh --music` sets it up.

**In-process generation is NOT available yet.** `diffusers` does ship an `AceStepPipeline`, and the
client for it is written (`app/services/music_local.py`, behind the `music_native` setting, default
**off**) — but no published ACE-Step checkpoint is in diffusers format. None carry `model_index.json`,
so `from_pretrained` 404s: verified against `ACE-Step/Ace-Step1.5`, `acestep-v15-xl-{base,turbo}`,
`ACE-Step-v1-3.5B` and the Comfy-Org mirror, on every branch and PR ref.

The released weights are a **transformers custom-code** model (`auto_map` →
`modeling_acestep_v15_turbo.AceStepConditionGenerationModel`, loaded with `trust_remote_code`) plus a
diffusers VAE. The pipeline wants `AceStepTransformer1DModel` / `AceStepConditionEncoder` — different
classes with a different state-dict layout — so the local checkpoint cannot simply be pointed at it;
it would need a weight port. Flip `music_native` on only once an official diffusers checkpoint exists.

The turbo DiT fits a **12 GB** GPU.

## Load balancing, locking & VRAM swap (same as image gen)

`music_factory` mirrors `image_factory`, **node→node**:

- **`music_server_urls`** (Admin → Music) = OTHER posterchanai NODES (e.g. `http://nas.lan:3051`),
  not acestep servers. They're called via their `/api/generate-music` endpoint, which runs that
  node's own local generation — so the remote node frees ITS GPU (`prepare_for_music`) before it
  generates. This is the same node→node pattern image gen uses (`/api/generate-image`).
- **local** = this process. `music_local` loads ACE-Step's own `AceStepHandler` under the shared
  `GPUResourceLock` (so chat, image AND music all QUEUE on one GPU) after
  `vram_manager.prepare_for_music()` unloads our LLM/image, and idle-unloads afterwards.
- **`music_api_base`** — leave it **EMPTY**. It now means "I really do have an ACE-Step REST server
  over *there*" and forces the legacy HTTP path; a leftover `http://localhost:8001` points at a
  daemon that no longer exists (a plain localhost value is ignored for exactly that reason).

Each request **round-robins across [remote nodes…, local]**, so songs spread over every node; a
failed node falls through to the next (and finally local). `_music_gen_lock` serializes music per
node so concurrent requests queue instead of OOMing one GPU. A node may safely list **itself** in
`music_server_urls` (a self-call just generates locally) — no loops.

> **Critical:** a single 12–16 GB GPU can't hold the chat/image models AND a music generation at
> once. That's why the local path **unloads** (`prepare_for_music`) and everything **queues** on the
> shared GPU lock — one model on the GPU at a time. Because the model now lives in OUR address space,
> unloading is our job: `music_local` drops the handler's model refs explicitly (it is not an
> `nn.Module`, so `.to("cpu")` does nothing) and idle-unloads after `music_idle_timeout`.
> Measured on the Arc: ~6.3 GB held while loaded, 100% reclaimed on unload.

## Setup

```bash
./install.sh --music     # clones ACE-Step, installs it into the APP venv with --no-deps
```

`--no-deps` is load-bearing: ACE-Step's pyproject pins `torch==2.10.0+cu128` and gradio, and letting
pip resolve those would replace a hand-built torch-XPU/ROCm install and break image gen on the same
box. Its real inference deps live in `requirements.txt`; only `torchaudio` is resolved by the
installer, from the same index as the installed torch. The installer also removes any leftover
`acestep.service`.

Then **Admin → Music**: enable music and leave **Local Server URL empty**. Remote Servers fan out to
other nodes. Models download on first use into `<ACE-Step clone>/checkpoints` (several GB).


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
