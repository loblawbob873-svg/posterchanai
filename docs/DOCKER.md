# Running PosterChanAI in Docker

One Ubuntu-based `Dockerfile` builds for **CPU, NVIDIA (CUDA), AMD (ROCm), or
Intel Arc (XPU)** — you pick the accelerator with the `GPU` build-arg. BuildKit
only pulls the base image for the backend you choose.

The GPU **userspace** (CUDA libs / ROCm / oneAPI runtime + the matching PyTorch
and a `llama-cpp-python` compiled for that backend) is installed *inside* the
image. The GPU **kernel driver always comes from the host** — Docker just exposes
the device. So the host needs the right driver/runtime:

| Backend | Host requirement | Run flag |
|---------|------------------|----------|
| `cpu`   | nothing | — |
| `cuda`  | NVIDIA driver + [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) | `--gpus all` |
| `rocm`  | host `amdgpu` kernel driver ([AMD docs](https://rocm.docs.amd.com/projects/install-on-linux/en/latest/how-to/docker.html)) | `--device /dev/kfd --device /dev/dri` |
| `intel` | host `i915` driver + render nodes | `--device /dev/dri` |

First login is **`admin` / `admin`** (change it in the admin UI). The container
runs as root so it reaches the GPU render node without group flags.

## Build

```bash
docker build -t posterchanai:cpu   --build-arg GPU=cpu   .
docker build -t posterchanai:cuda  --build-arg GPU=cuda  .
docker build -t posterchanai:rocm  --build-arg GPU=rocm  .
docker build -t posterchanai:intel --build-arg GPU=intel .
```

## Run

```bash
# CPU
docker run -d -p 3051:3051 -v pc-data:/var/lib/posterchanai -v pc-rag:/app/data \
  posterchanai:cpu

# NVIDIA
docker run -d --gpus all -p 3051:3051 \
  -v pc-data:/var/lib/posterchanai -v pc-rag:/app/data posterchanai:cuda

# AMD (ROCm userspace is in the image; driver is on the host). The entrypoint
# auto-sets HSA_OVERRIDE_GFX_VERSION for consumer RDNA cards (e.g. RX 6700 XT).
docker run -d --device /dev/kfd --device /dev/dri \
  --security-opt seccomp=unconfined -p 3051:3051 \
  -v pc-data:/var/lib/posterchanai -v pc-rag:/app/data posterchanai:rocm

# Intel Arc (image generation via torch-XPU)
docker run -d --device /dev/dri -p 3051:3051 \
  -v pc-data:/var/lib/posterchanai -v pc-rag:/app/data posterchanai:intel
```

Then open `http://localhost:3051`. Or use the bundled compose file:

```bash
docker compose --profile cuda up -d --build      # cpu | cuda | rocm | intel
```

## Persistence

Two volumes hold all mutable state, so you can recreate the container freely:

- `/var/lib/posterchanai` — uploads, downloaded LLM/image models, the HuggingFace
  cache (`HF_HOME`), torrents, **and the sqlite DB** (the entrypoint symlinks
  `posterchanai.db` here).
- `/app/data` — the ChromaDB RAG vector store.

## Configuration

Settings live in the database and are managed in the admin UI on first run; you
don't need env vars to start. Override the port with `-e POSTERCHANAI_PORT=...`
(keep `3051` to keep the schedulers/bots active — the app gates those on 3051).

## Backend notes

- **Build args you may want:** `--build-arg INSTALL_BROWSER=false` (skip headless
  Chrome / the screenshot command), `--build-arg AMDGPU_TARGETS='gfx1100'` (build
  HIP kernels only for your card — faster build, smaller), `--build-arg
  ROCM_VERSION=6.2.4`, `--build-arg TORCH_CUDA_INDEX=...`, `--build-arg
  LLAMA_CPP_VERSION=...`.
- **AMD:** the image installs ROCm userspace from AMD's repos (no DKMS). If a HIP
  build of `llama-cpp-python` fails for your arch it falls back to a CPU build, so
  the container still runs (text gen just won't use the GPU). Prefer an external
  Ollama if you hit that.
- **Intel Arc:** the `intel` image targets **image generation** (native
  PyTorch-XPU, which bundles its own oneAPI). A SYCL `llama-cpp-python` is also
  built, but running GPU LLM *and* torch-XPU image gen in one process is the
  known-conflicting combo PosterChanAI normally splits across two services. For
  GPU LLM on Intel, set `-e SOURCE_ONEAPI=1` (sources system oneAPI for the SYCL
  runtime) and use that container for chat, or point chat at an external Ollama.
- **No local GPU LLM?** Any image works fine pointed at an external **Ollama**
  (set it in the admin UI) — then the backend is just for image gen / the web app.
```
