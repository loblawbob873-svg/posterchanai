# Running PosterChanAI in Docker

One Ubuntu-based `Dockerfile` builds for **CPU, NVIDIA (CUDA), AMD (ROCm), or
Intel Arc (XPU)** — pick the accelerator with the `GPU` build-arg. BuildKit only
pulls the base image for the backend you choose.

The image is **turnkey**: on first run it comes up on the `native` LLM + image
backends, auto-downloads the recommended chat model, and (on AMD) auto-detects the
GPU override and persists the MIOpen kernel cache. The GPU **userspace** (CUDA libs
/ ROCm / oneAPI runtime + matching PyTorch + a `llama-cpp-python` compiled for that
backend) is baked into the image. The GPU **kernel driver always comes from the
host** — Docker just exposes the device.

| Backend | Host requirement | Run flag |
|---------|------------------|----------|
| `cpu`   | nothing | — |
| `cuda`  | NVIDIA driver + [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) | `--gpus all` |
| `rocm`  | host `amdgpu` kernel driver ([AMD docs](https://rocm.docs.amd.com/projects/install-on-linux/en/latest/how-to/docker.html)) | `--device /dev/kfd --device /dev/dri` |
| `intel` | host `i915` driver + render nodes | `--device /dev/dri` |

The container runs as root, so it reaches the GPU render node without group flags.

## Build

```bash
docker build -t posterchanai:cpu   --build-arg GPU=cpu   .
docker build -t posterchanai:cuda  --build-arg GPU=cuda  .
docker build -t posterchanai:rocm  --build-arg GPU=rocm  .
docker build -t posterchanai:intel --build-arg GPU=intel .
```

ROCm builds the HIP `llama-cpp-python`, which **requires ROCm ≥ 6.3** (the image
installs 6.3.4); the SYCL/Intel build is RAM-hungry (`icpx`) so build it on a box
with enough memory. Narrow the AMD build to just your card for speed:
`--build-arg AMDGPU_TARGETS=gfx1100`.

## Run

```bash
# CPU
docker run -d -p 3051:3051 -v pc-data:/var/lib/posterchanai -v pc-rag:/app/data posterchanai:cpu

# NVIDIA
docker run -d --gpus all -p 3051:3051 \
  -v pc-data:/var/lib/posterchanai -v pc-rag:/app/data posterchanai:cuda

# AMD (ROCm userspace is in the image; driver is on the host)
docker run -d --device /dev/kfd --device /dev/dri --security-opt seccomp=unconfined \
  -p 3051:3051 -v pc-data:/var/lib/posterchanai -v pc-rag:/app/data posterchanai:rocm

# Intel Arc (image gen via torch-XPU)
docker run -d --device /dev/dri -p 3051:3051 \
  -v pc-data:/var/lib/posterchanai -v pc-rag:/app/data posterchanai:intel
```

Or use the bundled compose file: `docker compose --profile cuda up -d --build`
(`cpu` | `cuda` | `rocm` | `intel`).

Open `http://<host>:3051` and log in with **`admin` / `admin`** (change it).

## First-run expectations

- The recommended **chat model** (Qwen3.5-9B, ~5.6 GB) downloads in the background
  on first start — the web UI is up immediately; chat works once it lands (watch
  `docker logs`). Skip with `-e DOWNLOAD_MODEL=0`.
- The **image model** (DreamShaper-8, SD1.5 — fast and fits consumer GPUs next to
  the LLM) is fetched by diffusers on the first image generation.
- **AMD:** the first image generation compiles MIOpen kernels (a one-time ROCm cost,
  ~30 s) and caches them on the volume — every run after is fast (~3 s/image). The
  entrypoint also auto-sets `HSA_OVERRIDE_GFX_VERSION` for consumer RDNA cards.

## Persistence

Two volumes hold all mutable state:

- `/var/lib/posterchanai` — uploads, downloaded models, HF cache, **MIOpen cache**,
  the **sqlite DB** (symlinked here), torrents, Tor data.
- `/app/data` — the ChromaDB RAG store.

## Configuration / opt-ins

Settings live in the DB and are managed in the admin UI. The turnkey defaults are
seeded from env on first run (override at `docker run -e ...`):

| Env | Default | Effect |
|-----|---------|--------|
| `POSTERCHANAI_LLM_BACKEND` | `native` | local llama-cpp (GPU) |
| `POSTERCHANAI_IMAGE_BACKEND` | `native` | local diffusers (GPU) |
| `POSTERCHANAI_IMAGE_MODEL_PATH` | `Lykon/dreamshaper-8` | image model (HF repo) |
| `DOWNLOAD_MODEL` | `1` | auto-download the LLM |
| `POSTERCHANAI_LOW_VRAM` | (unset) | `1` = diffusers model-offload (run SDXL on a small GPU) |
| `POSTERCHANAI_TOR_ENABLED` / `_PROXY_ENABLED` / `_BT_ENABLED` | `false` | Tor + HTTP proxy + torrenting (the binaries ship in the image; opt in here or in the admin UI) |

**Tor/proxy/torrenting are OFF by default.** Enabling the proxy routes outbound
fetches (4chan, news, …) through Tor — only turn it on once Tor finishes
bootstrapping, or those features will fail while Tor is stuck.

## Bring your own image models

The default image model (DreamShaper-8) auto-downloads. To use your own checkpoints
(e.g. CyberRealistic XL, an anime model like Nova 3DCG XL — CivitAI `.safetensors`
that need a manual download), just **copy the files into the models folder**:

- **compose:** files dropped in the host `./models/` folder appear in the container
  (it's bind-mounted to `/var/lib/posterchanai/models`).
- **plain `docker run`:** copy into the volume with
  `docker cp CyberRealisticXL.safetensors <container>:/var/lib/posterchanai/models/`
  (or bind-mount a folder: `-v /path/to/models:/var/lib/posterchanai/models`).

Then point the app at them — in **Admin → Settings**, or at first run via env:

```bash
-e POSTERCHANAI_IMAGE_MODEL_PATH=/var/lib/posterchanai/models/CyberRealisticXL.safetensors \
-e POSTERCHANAI_IMAGE_MODEL_TYPE=sdxl \
-e POSTERCHANAI_IMAGE_ANIME_MODEL_PATH=/var/lib/posterchanai/models/Nova3DCGXL.safetensors
```

A single-file `.safetensors` SDXL checkpoint loads via diffusers `from_single_file`;
set the type to `sdxl` (or `sd15`). The anime model is used for style switching.

## Backends notes

- **No local GPU LLM?** Any image works pointed at an external **Ollama** (set it in
  the admin UI); the backend is then just image gen / the web app.
- **AMD HIP llama** needs ROCm ≥ 6.3; if your card's HIP kernels aren't built it
  falls back to CPU (text gen won't use the GPU) — rebuild with your
  `AMDGPU_TARGETS`, or use Ollama.

## Connecting opencode (or any OpenAI client)

The app exposes an OpenAI-compatible API at `http://<host>:3051/v1`. Create a user
API key (admin UI → API keys, format `sk-...`) and point your client at it. Example
opencode `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "permission": "allow",
  "provider": {
    "posterdocker": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "http://<host>:3051/v1", "apiKey": "sk-..." },
      "models": { "Qwen3.5-9B-abliterated-Q4_K_M.gguf": { "name": "Qwen3.5-9B-abliterated-Q4_K_M.gguf" } }
    }
  },
  "model": "posterdocker/Qwen3.5-9B-abliterated-Q4_K_M.gguf"
}
```

Use the model id from `GET /v1/models`.
