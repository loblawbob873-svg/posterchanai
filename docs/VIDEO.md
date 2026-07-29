# Video Generation (`videogeni`)

Native, in-process **text-to-video** via Hugging Face `diffusers`. Available in the **web UI** and
**Telegram** (intentionally *not* the Pleroma bots — abuse surface).

```
videogeni <prompt>                 # generate a short clip
videogeni <prompt> | <negative>    # optional negative prompt after a |
```

Example: `videogeni a red fox running through snow, cinematic`

## Output: a branded MP4

Each clip is delivered as an **MP4** with the same end-card outro (the "watermark") used on
effect/music videos, optionally upscaled to 720p/1080p. The web UI shows an inline `<video>` player
with a download button; Telegram sends it as a video. The end-card is gated by
`video_watermark_enabled`.

## Native, not a separate server

Unlike music (ACE-Step, which needs a conflicting `torch` stack and runs as its own service), video
models are **stock `diffusers` pipelines** on the *same* torch stack as image generation. So video
runs **in-process**, sharing the venv, the shared `GPUResourceLock` (chat / image / music / video
all queue as one GPU task at a time) and the `vram_manager` model-swap. See
`app/services/video_service.py` (the generator) and `app/services/video_factory.py` (node→node
load balancing). Portability rule: stay on stock diffusers + torch SDPA — **no flash-attn / xformers
/ fp8 / GGUF** (those are CUDA-pinned and break Intel Arc / AMD ROCm).

## Choosing a model for your GPU

The `video_model` setting (Admin → Video) takes **any** diffusers text-to-video model (HF id or
local path) — the loader auto-detects the pipeline and the model auto-downloads on first use. Pick
for your VRAM:

| VRAM | Model | Notes |
|------|-------|-------|
| **12–16 GB** | `Wan-AI/Wan2.1-T2V-1.3B-Diffusers` (default) | ~480p, ~3 s. Fits without offload on 16 GB. |
| **12–16 GB** | `THUDM/CogVideoX-5b` | ~6 s, better quality; needs **CPU-offload** (slower). |
| **24 GB** | `THUDM/CogVideoX1.5-5B` | 10 s, native 768p, good quality. |
| **40 GB+** | `Wan-AI/Wan2.1-T2V-14B-Diffusers` | true HD. |

> **CPU-offload works on CUDA/ROCm, not on Intel XPU** (the offload hooks are CUDA-oriented), so on
> an Intel Arc you're limited to models that fit *fully* in VRAM (e.g. Wan-1.3B on 16 GB).

## Per-GPU settings (Admin → Video)

Every knob is tunable for your hardware — nothing is hardcoded to a particular card:

- `video_width` / `video_height` — generation resolution (use the model's native res).
- `video_num_frames` — clip length = frames ÷ fps. `video_max_frames` is a **hard safety cap**: an
  over-large request is *clamped* instead of OOMing the GPU (set it to the most your card can render).
- `video_fps` — the model's native fps (Wan/LTX 16, CogVideoX 8). Higher without interpolation just
  speeds up motion.
- `video_default_steps`, `video_guidance` — quality knobs.
- `video_upscale_height` — lanczos-upscale the finished clip to 720p/1080p (cheap, no extra VRAM);
  use when the model renders below your target. `Native` = no upscale.
- `video_cpu_offload` — stream weights from RAM so a big model fits a small card (slower). On for
  big models on ≤16 GB CUDA; off on 24 GB+.

## Multi-node load balancing

Mirrors image/music: set `video_server_urls` to other PosterChanAI nodes and the LB round-robins
across them + the local GPU, each node freeing its own GPU first via `/api/generate-video`. A node
that can't generate (local disabled, or OOM) is skipped and the request **falls back** to another
node. Set `video_local_enabled=false` to make a node forward-only.

**Music/video on one GPU:** on a node where the ACE-Step music server and video share a single GPU,
set `video_free_music=true` — a video render then **stops the music service** (`music_service_name`,
default `acestep`) to reclaim its VRAM and restarts it for the next music request (needs passwordless
sudo for `systemctl`).

## Turn-key setup

- **Installer:** `./install.sh` prompts to set up video, or run the add-on `./install.sh --video`
  (installs `sentencepiece` + `ftfy` into the image venv and optionally pre-downloads the model).
- **Docker:** set `POSTERCHANAI_VIDEO=1` (with a `cuda` / `rocm` / `intel` GPU profile) to
  auto-enable video; override the model with `POSTERCHANAI_VIDEO_MODEL`. Deps are baked into the
  image; the model auto-downloads to the persisted HF cache on first use.
