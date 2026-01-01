# Native Image Generation Implementation Plan

## Overview

Replace ComfyUI dependency with native `diffusers` integration, similar to how Ollama was replaced with native llama-cpp-python.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        posterchanai                             │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────────┐              ┌─────────────────────────┐   │
│  │   LLM Backend   │              │   Image Backend         │   │
│  ├─────────────────┤              ├─────────────────────────┤   │
│  │ • native        │              │ • native (diffusers)    │   │
│  │ • ipex          │              │ • comfyui (external)    │   │
│  │ • ollama        │              │                         │   │
│  └─────────────────┘              └─────────────────────────┘   │
│           │                                  │                   │
│           └──────────┬───────────────────────┘                   │
│                      │                                           │
│              ┌───────▼───────┐                                   │
│              │ VRAM Manager  │                                   │
│              │ (swap models) │                                   │
│              └───────────────┘                                   │
└─────────────────────────────────────────────────────────────────┘
```

## Phases

### Phase 1: Core Diffusers Service

Create `app/services/diffusers_service.py`:

```python
class DiffusersService:
    def __init__(self):
        self.pipe = None
        self.model_path = None
        self.device = None

    def load_model(self, model_path: str):
        """Load a Stable Diffusion model (safetensors/checkpoint)"""

    def unload_model(self):
        """Free VRAM"""

    def txt2img(self, prompt, negative_prompt, steps, cfg, width, height, seed):
        """Generate image from text"""

    def img2img(self, image, prompt, negative_prompt, steps, cfg, strength, seed):
        """Transform image with prompt"""

    def get_model_info(self):
        """Return model status for health check"""
```

**Supported models:**
- SD 1.5 (512x512 base)
- SDXL (1024x1024 base)
- SD3 / SD3.5
- Flux (if VRAM allows)

### Phase 2: Multi-GPU Backend Support

Auto-detect and use available GPU:

```python
def detect_device():
    if torch.cuda.is_available():
        # Check if it's AMD ROCm or NVIDIA
        return "cuda"
    elif hasattr(torch, "xpu") and torch.xpu.is_available():
        return "xpu"  # Intel Arc
    else:
        return "cpu"
```

**Dependencies per backend:**

| Backend | PyTorch Install |
|---------|-----------------|
| NVIDIA | `pip install torch torchvision` |
| Intel Arc | `pip install torch intel-extension-for-pytorch` (from Intel repo) |
| AMD ROCm | `pip install torch torchvision --index-url https://download.pytorch.org/whl/rocm5.6` |
| CPU | `pip install torch torchvision` |

### Phase 3: Admin UI and Settings

New settings in `app/schemas.py`:

```python
# Image generation settings
image_backend: str = "comfyui"  # "native" or "comfyui"
image_model_path: str = ""
image_default_steps: str = "20"
image_default_cfg: str = "7.0"
image_default_width: str = "512"
image_default_height: str = "512"
image_gpu_device: str = "auto"  # "auto", "cuda", "cuda:0", "xpu", "cpu"
```

Admin UI fieldset:
- Backend Type dropdown (Native / ComfyUI)
- Model Path (file picker or text)
- Default Steps, CFG, dimensions
- GPU Device selector
- Reload Model button
- Model status display

### Phase 4: VRAM Management

For single-GPU systems running both LLM and Image gen:

```python
class VRAMManager:
    def __init__(self):
        self.current_mode = None  # "llm" or "image"

    def prepare_for_llm(self):
        """Unload image model if loaded, ensure LLM is ready"""
        if diffusers_service.is_loaded():
            diffusers_service.unload_model()
        llm_service.ensure_loaded()
        self.current_mode = "llm"

    def prepare_for_image(self):
        """Unload LLM if loaded, ensure image model is ready"""
        if llm_service.is_loaded():
            llm_service.unload_model()
        diffusers_service.ensure_loaded()
        self.current_mode = "image"
```

Settings:
- `vram_mode: "shared"` - Swap models (single GPU)
- `vram_mode: "dedicated"` - Keep both loaded (dual GPU or lots of VRAM)

### Phase 5: Modular Installer

Update `install.sh`:

```bash
echo "What would you like to install?"
echo ""
echo "  [1] Full Stack (LLM + Image Generation)"
echo "  [2] LLM Only"
echo "  [3] Image Generation Only"
echo "  [4] Lightweight (External Services)"
echo ""
read -p "Select option [1-4]: " INSTALL_MODE
```

**Distro-specific packages:**

```bash
detect_distro() {
    if [ -f /etc/gentoo-release ]; then
        echo "gentoo"
    elif [ -f /etc/debian_version ]; then
        echo "debian"
    elif [ -f /etc/fedora-release ]; then
        echo "fedora"
    elif [ -f /etc/arch-release ]; then
        echo "arch"
    fi
}

install_image_deps() {
    case $(detect_distro) in
        gentoo)
            echo "emerge -n media-libs/opencv media-gfx/imagemagick"
            ;;
        debian)
            echo "apt install -y libgl1-mesa-glx libglib2.0-0"
            ;;
        fedora)
            echo "dnf install -y mesa-libGL glib2"
            ;;
        arch)
            echo "pacman -S --noconfirm opencv"
            ;;
    esac
}
```

**GPU-specific installs:**

```bash
install_pytorch_for_gpu() {
    GPU_TYPE=$1
    case $GPU_TYPE in
        nvidia)
            pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
            ;;
        intel)
            pip install torch==2.1.0a0 intel-extension-for-pytorch==2.1.30+xpu \
                --extra-index-url https://pytorch-extension.intel.com/release-whl/stable/xpu/us/
            ;;
        amd)
            pip install torch torchvision --index-url https://download.pytorch.org/whl/rocm5.6
            ;;
        cpu)
            pip install torch torchvision
            ;;
    esac

    pip install diffusers transformers accelerate safetensors
}
```

### Phase 6: Worker Mode

Add `--worker` flag to `run.py`:

```bash
# LLM worker only
python run.py --worker llm

# Image worker only
python run.py --worker image

# Full stack (default)
python run.py
```

Worker mode:
- Skips loading unused components
- Optimized for single-purpose operation
- Exposes API endpoints for remote access

**Image worker endpoints:**
- `POST /api/image/txt2img` - Generate from text
- `POST /api/image/img2img` - Transform image
- `GET /api/image/status` - Model status
- `POST /api/image/reload` - Reload model

Main instance config:
```
image_backend: remote
image_remote_url: http://image-worker:3051
```

## File Structure

```
posterchanai/
├── app/
│   └── services/
│       ├── llama_service.py      # Native LLM (existing)
│       ├── ipex_service.py       # IPEX LLM (existing)
│       ├── diffusers_service.py  # NEW: Native image gen
│       ├── comfyui_service.py    # Existing ComfyUI client
│       ├── image_factory.py      # NEW: Backend selector
│       └── vram_manager.py       # NEW: VRAM swap logic
├── install.sh                    # Updated modular installer
└── docs/
    └── NATIVE-IMAGE-PLAN.md      # This file
```

## Migration Path

1. Default behavior unchanged (ComfyUI backend)
2. Users opt-in to native backend via admin setting
3. Installer offers choice for new installations
4. Existing `geni` and `img2img` commands work with both backends

## Requirements

**Python packages:**
```
diffusers>=0.27.0
transformers>=4.37.0
accelerate>=0.27.0
safetensors>=0.4.0
```

**System packages (for OpenCV/image processing):**
- Gentoo: `media-libs/opencv`
- Debian/Ubuntu: `libgl1-mesa-glx libglib2.0-0`
- Fedora: `mesa-libGL glib2`
- Arch: `opencv`

## Timeline

| Phase | Description | Estimate |
|-------|-------------|----------|
| 1 | Core diffusers service | Foundation |
| 2 | Multi-GPU support | +GPU detection |
| 3 | Admin UI | +Settings |
| 4 | VRAM management | +Swap logic |
| 5 | Modular installer | +Distro support |
| 6 | Worker mode | +Distributed |

## Notes

- Keep backwards compatibility with ComfyUI
- LoRA support via diffusers' `load_lora_weights()`
- Consider adding ControlNet later (Phase 7)
- Model download helper (like Ollama pull) could be Phase 8
