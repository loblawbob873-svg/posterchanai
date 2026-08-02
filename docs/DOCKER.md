# Running PosterChanAI in Docker

One Ubuntu-based `Dockerfile` builds for **CPU, NVIDIA (CUDA), AMD (ROCm), Intel
Arc (XPU), or Nostr-only** — pick with the `GPU` build-arg. BuildKit only pulls the
base image for the backend you choose.

> **Just want a Nostr relay + client, no AI?** Use `GPU=nostr` — a small (~2 GB vs ~70 GB)
> image with no torch/llama/diffusers. See [Nostr-only](#nostr-only-no-ai) below.

**Postgres is the one and only database** — it backs both the app and the built-in Nostr relay, so
it is **required in every setup**. The **recommended (and simplest) way to run is the bundled
`docker compose` file**: it starts a `postgres` service and wires it up automatically. A bare
`docker run` has **no database** and will exit on startup with `connection refused` — only use it
if you point `DATABASE_URL` / `NOSTR_RELAY_PG_DSN` at your own external Postgres.

The image is **turnkey**: on first run it comes up on the `native` LLM + image
backends, auto-downloads the recommended chat model, and (on AMD) auto-detects the
GPU override and persists the MIOpen kernel cache. The GPU **userspace** (CUDA libs
/ ROCm / oneAPI runtime + matching PyTorch + a `llama-cpp-python` compiled for that
backend) is baked into the image. The GPU **kernel driver always comes from the
host** — Docker just exposes the device.

| Backend | Host requirement | Run flag | Linux | Windows | macOS |
|---------|------------------|----------|:-----:|:-------:|:-----:|
| `cpu`   | nothing | — | ✅ | ✅ | ✅ |
| `nostr` | nothing (no AI) | — | ✅ | ✅ | ✅ |
| `cuda`  | NVIDIA driver + [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) | `--gpus all` | ✅ | ⚠️ WSL2 only | ❌ |
| `rocm`  | host `amdgpu` kernel driver ([AMD docs](https://rocm.docs.amd.com/projects/install-on-linux/en/latest/how-to/docker.html)) | `--device /dev/kfd --device /dev/dri` | ✅ | ❌ | ❌ |
| `intel` | host `i915` (Alchemist) or `xe` (Battlemage+) driver + render nodes | `--device /dev/dri` | ✅ | ❌ | ❌ |

The container runs as root, so it reaches the GPU render node without group flags.

### Windows / macOS: use `cpu` or `nostr`

**GPU passthrough is a Linux-kernel feature.** `rocm` and `intel` pass through `/dev/kfd` and
`/dev/dri`, which are created by the host's `amdgpu` / `i915` kernel driver — Docker Desktop runs
containers inside a Linux VM that has no such devices, so those profiles **cannot** work on Windows
or macOS no matter how the GPU is configured. `cuda` is the one exception, and only on Windows via
the WSL2 backend with an NVIDIA driver that supports it.

If you pick a GPU profile anyway, Docker fails at container-create with:

```
Error response from daemon: error gathering device information while adding
custom device "/dev/kfd": no such file or directory
```

That message means exactly one thing: **you're on a host without that device** — almost always
Docker Desktop on Windows/macOS, or a Linux box whose AMD driver isn't loaded. It is not a
configuration problem you can fix with flags. To test the app on Windows or macOS, run:

```bash
docker compose --profile cpu   up -d --build   # full app, LLM/image on CPU (slow but works everywhere)
docker compose --profile nostr up -d --build   # relay + client + Blossom, no AI (~2 GB, fast)
```

On Linux, `/dev/kfd` missing means the `amdgpu` kernel module isn't loaded (`lsmod | grep amdgpu`)
or you're on an unsupported GPU — check `ls -l /dev/kfd /dev/dri` before filing a bug.

## Install — one `docker compose` command

Pick the profile for your hardware. Compose **builds the image, starts PostgreSQL (required), and
wires it all together** — there's no separate build/run step to manage:

```bash
docker compose --profile cpu   up -d --build   # no GPU
docker compose --profile cuda  up -d --build   # NVIDIA   — needs the NVIDIA Container Toolkit
docker compose --profile rocm  up -d --build   # AMD      — needs the host amdgpu driver
docker compose --profile intel up -d --build   # Intel Arc — needs host i915/xe + /dev/dri
docker compose --profile nostr up -d --build   # Nostr relay + client + Blossom, NO AI (~2 GB)
```

Then open **http://localhost:3051/client**. The first build pulls only the base image for your
profile; the recommended chat model auto-downloads in the background on first run. **To update:**
`git pull` and re-run the same command.

Each profile builds its **own image tag** (`posterchanai:<TAG>-<backend>`, `TAG` defaults to
`local`), so profiles never alias one another — `--profile nostr` always runs the lean Nostr-only
image even on a box where you previously built `cuda`.

> Override the password in a `.env` next to the compose file: `POSTGRES_PASSWORD=…` (used by both
> the `postgres` service and the app's `DATABASE_URL`/`NOSTR_RELAY_PG_DSN`).

## Advanced: manual build / bring-your-own-Postgres

You normally never need this — compose does it. But if you build/run by hand:

```bash
docker build -t posterchanai:cuda --build-arg GPU=cuda --build-arg BASE_IMAGE=nvidia/cuda:12.5.1-devel-ubuntu24.04 .
# GPU = cpu | cuda | rocm | intel | nostr.  cpu/nostr/rocm need only GPU; cuda/intel also pass BASE_IMAGE.
```

The Dockerfile uses **one** parametrized base (`BASE_IMAGE`, default `ubuntu:24.04`), so a
`cpu`/`nostr` build never pulls the cuda/intel/rocm images (even on the legacy builder). ROCm builds
the HIP `llama-cpp-python` (**requires ROCm ≥ 6.3**); the SYCL/Intel build is RAM-hungry — narrow the
AMD build with `--build-arg AMDGPU_TARGETS=gfx1100`.

A bare `docker run` has **no database** — you must point it at your own Postgres or it exits with
`connection refused`:

```bash
docker run -d --gpus all -p 3051:3051 -v pc-data:/var/lib/posterchanai -v pc-rag:/app/data \
  -e DATABASE_URL='postgresql+psycopg2://user:pw@host:5432/posterchan_relay' \
  -e NOSTR_RELAY_PG_DSN='host=host port=5432 dbname=posterchan_relay user=user password=pw' \
  posterchanai:cuda
```

## Nostr-only (no AI)

A self-hosted **Nostr relay + the web client + Blossom**, with **no AI stack** (no
torch/llama/diffusers) — a small, fast image for people who don't care about AI. The relay and
the AI-hidden UI are turned on for you.

```bash
docker compose --profile nostr up -d --build
# open http://<host>:3051/client  •  relay at ws://<host>:3052/relay
```

Or build/run by hand:

```bash
docker build -t posterchanai:nostr --build-arg GPU=nostr --build-arg INSTALL_BROWSER=false .
```

The app boots fine without the AI libraries (every ML import is lazy); the AI tab is hidden via
`POSTERCHANAI_NOSTR_ONLY=1`. You can switch a node to a full GPU profile later without losing data
(same volumes).

## Git server (git-over-nostr)

The built-in git host is off by default. Turn it on with one flag:

```bash
POSTERCHANAI_GIT=1 POSTERCHANAI_GIT_PUBLIC_BASE=https://your-domain/git \
  docker compose --profile cuda up -d
```

That enables the host on **:3053** (bound `0.0.0.0` so the published port works) and puts the public
base into the clone URLs it hands out. Repos live on the `pc-data` volume
(`/var/lib/posterchanai/git_repos`), and `git` + `git-http-backend` are already in the image.

In production don't publish 3053 — front it with nginx `location /git/` (see [NGINX.md](NGINX.md)) so
clones/pushes go over TLS. Full walkthrough: [GIT.md](GIT.md).

## Splitting the stack (one component per container)

By default a container runs `POSTERCHANAI_ROLE=all`: the web app **plus** the Nostr relay, the
background worker, mediamtx/pion-turn and the bots it supervises. That is the single-container layout
and nothing about it has changed — `docker compose up` behaves exactly as before.

The cost of `all` is that restarting the container to ship a code change also drops every connected
Nostr client, kills any live stream **mid-broadcast**, drops active calls and restarts the bots. If
that matters, run one component per container instead:

| role | what it runs |
|------|--------------|
| `app` | the web app — plus the **bot manager**, which cannot be split (see below) |
| `relay` | the Nostr relay (`:3052`) |
| `worker` | background pollers/schedulers |
| `media` | mediamtx (streams) + pion-turn (calls) |
| `tor` | the Tor daemons (.onion + SOCKS egress) |
| `proxy` | the HTTP proxy fronting Tor |
| `git` | the GRASP git host |

```bash
# Bring up the split layout. `split` services publish NO ports (only the app does), so there is
# nothing to collide, and they are REAL services — `docker compose down` removes them.
POSTERCHANAI_ROLE=app,bots docker compose --profile cpu --profile split up -d

# …and down, cleanly:
docker compose --profile cpu --profile split down
```

> **Do not use `docker compose run` for these.** `run` creates a ONE-OFF container that
> `docker compose down` does not manage, so it stays attached to the project network and `down`
> reports `Network posterchanai_default  Resource is still in use` while the container lingers
> invisibly. (An earlier version of this page recommended exactly that — it was wrong.) If you have
> orphans from that, clear them with `docker compose down --remove-orphans`.


**The bots stay with the web app, deliberately.** Admin → Bots reads the manager's *in-process*
registry and drives start/stop/publish through it. Run the manager elsewhere and the admin UI shows
every bot as stopped while they are running perfectly, and a button press makes the app spawn a
SECOND copy of every bot. Roles are comma-separated for exactly this reason — the app runs
`app,bots`.

Both halves are required. Running the role containers **without** setting the app to `app` gives you
two of everything; setting the app to `app` **without** the role containers leaves those components
running nowhere. Two-of-everything fails loudly (they bind the same ports) rather than corrupting
anything, but neither half alone is a state to leave a deployment in.

The equivalent on bare metal is `scripts/install_services.sh`, which writes the matching systemd
units and flips the main unit to `--role app` in one step (`--revert` undoes it).

## Production (HTTPS / TLS)

The container serves plain HTTP/WS — front it with **nginx** to get HTTPS, your own domain, and a
proper `wss://…/relay`. A ready-to-edit template + guide:
[`nginx/posterchanai.conf.example`](../nginx/posterchanai.conf.example) and [NGINX.md](NGINX.md).

Open `http://<host>:3051/client` and log in with your **Nostr key** (NIP-07 browser extension or
NIP-46 remote signer like Amber). The first admin is configured in Admin → Users.

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
- `/app/data` — app data directory.

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
| `POSTERCHANAI_TOR_ENABLED` / `_PROXY_ENABLED` / `_BT_ENABLED` | `false` | Tor + outbound HTTP proxy + torrenting (binaries ship in the image; set `true` to opt in) |

**Tor / HTTP proxy / torrenting are OFF by default** and opt-in: the binaries ship in the image,
but nothing auto-starts a Tor process at boot. When you enable Tor, **PosterChanAI starts and
manages the daemon itself** (SOCKS5 :9052), along with the outbound HTTP proxy (:8118 → Tor) and
the libtorrent client (routed through that proxy). They're a chain (torrents → the :8118 proxy →
Tor), so they default off together. Opt in per piece with
`-e POSTERCHANAI_TOR_ENABLED=true` / `_PROXY_ENABLED=true` / `_BT_ENABLED=true`, or toggle in
the matching Admin tab. (Only seeded on first run; existing nodes keep their current settings.)

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

**Recommended model for agentic coding:** the turnkey image ships the lightweight 9B as the
default, but for serious opencode use download
[`Qwen3-Coder-30B-A3B-Instruct`](https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF)
(`IQ4_XS`, ~16 GB) into the models volume and point opencode's `model` at its basename. It's a
MoE (~3 B active) that reliably 1-shots small apps where 8–14 B models stall. Needs a 12 GB+ GPU
(partial CPU offload on 12–16 GB cards; leave `ollama_num_ctx` on `auto` — it auto-sizes context
even when the weights spill to CPU).

Point the **agentic/tools model** at it server-wide via **Admin → AI Settings → Agentic / Tools
Model** (`llm_tools_model`) or the **`POSTERCHANAI_LLM_TOOLS_MODEL`** env var on first run — it's
used for tool-bearing `/v1` requests and the `node agent` command. The image ships the lightweight
9B as the default; if the configured tools GGUF isn't in the models volume it transparently falls
back to the default model, so this is safe to leave at the default.
