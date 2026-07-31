#!/bin/bash
# Voice cloning (the `voice` command) setup module for install.sh.  Run via:  ./install.sh --voice
#
# Zero-shot voice cloning: a few seconds of reference audio, then arbitrary text in that voice. It
# runs NATIVELY in-process on the SAME torch stack as image/video/music — there is no extra venv and
# no service. Generation shares the app's GPU lock + VRAM swap (prepare_for_voice) and load-balances
# to other nodes via /api/generate-voice.
#
# Portability: stock transformers + torch SDPA, so it runs on CUDA, Intel Arc (XPU) and AMD (ROCm).
# The app forces a CPU map_location during load (see voice_local) because the published checkpoints
# carry CUDA storage tags and would otherwise refuse to load on anything that isn't NVIDIA.
#
# --no-deps IS LOAD-BEARING — do not "fix" the pip warnings it prints.
# chatterbox-tts pins torch==2.6.0, torchaudio==2.6.0, transformers==5.2.0, diffusers==0.29.0 and
# gradio. Letting pip resolve those would replace the GPU torch, downgrade transformers past what
# ACE-Step needs (`transformers<5`) and downgrade diffusers past what video gen needs — one command
# breaking image, music AND video generation on the same box. The API chatterbox actually imports
# (LlamaModel/GPT2Model/AutoTokenizer/GenerationMixin, diffusers' Attention + LoRACompatibleLinear)
# all exist in the versions we already pin, which is why --no-deps works rather than merely "runs".
# s3tokenizer gets the same treatment for a different reason: it declares pre-commit, virtualenv and
# nodeenv as RUNTIME dependencies, which have no business on a server.

# Pick the venv that holds the torch/diffusers stack (intel = venv-unified; otherwise venv).
_voice_pick_venv() {
    local c
    for c in "${CHAT_VENV_NAME:-}" "venv-unified" "venv" "venv-xpu-new" "venv-xpu"; do
        [ -n "$c" ] && [ -x "$SCRIPT_DIR/$c/bin/python" ] && { echo "$c"; return 0; }
    done
    return 1
}

setup_voice_deps() {
    print_step "Installing voice cloning dependencies..."
    local VENV_NAME
    VENV_NAME="$(_voice_pick_venv)" || { print_warning "No torch venv found — run the image-generation install first, then ./install.sh --voice"; return 1; }
    print_step "Using venv: $VENV_NAME"
    source "$SCRIPT_DIR/$VENV_NAME/bin/activate"

    # Record what must not move, then hand pip a constraints file. Without this a transitive dep is
    # free to pull a different torch/transformers/diffusers in behind our back — which is exactly how
    # a node once drifted to transformers 5.14.1 and stopped generating music, with no error until
    # the next song. With it, pip FAILS instead of silently breaking three other features.
    local CONSTRAINTS
    CONSTRAINTS="$(mktemp)"
    python - "$CONSTRAINTS" <<'PY' || { print_warning "Could not pin the existing stack; aborting rather than risk it"; deactivate; return 1; }
import sys
from importlib.metadata import version, PackageNotFoundError
out = []
for pkg in ("torch", "torchaudio", "transformers", "diffusers", "numpy", "safetensors"):
    try:
        out.append(f"{pkg}=={version(pkg)}")
    except PackageNotFoundError:
        pass
open(sys.argv[1], "w").write("\n".join(out) + "\n")
print("pinning:", ", ".join(out))
PY

    pip install -q --no-deps chatterbox-tts==0.1.7 s3tokenizer resemble-perth \
        || { print_error "chatterbox-tts install failed"; rm -f "$CONSTRAINTS"; deactivate; return 1; }
    pip install -q -c "$CONSTRAINTS" librosa==0.11.0 conformer==0.3.2 pykakasi==2.3.0 pyloudnorm omegaconf \
        || { print_error "voice runtime deps failed"; rm -f "$CONSTRAINTS"; deactivate; return 1; }
    rm -f "$CONSTRAINTS"

    # resemble-perth (the watermarker chatterbox constructs on EVERY load) imports pkg_resources, which
    # setuptools REMOVED in 81. On Python 3.11 boxes with an older setuptools this is invisible; on a
    # 3.12 box with setuptools>=81 perth swallows the ImportError, exports PerthImplicitWatermarker as
    # None, and the model dies at construction with "'NoneType' object is not callable" — after the
    # 6GB download, with nothing pointing at setuptools. Only downgrade when it is genuinely missing,
    # so a node that already works is left alone.
    if ! python -c "import pkg_resources" >/dev/null 2>&1; then
        print_step "pkg_resources is missing (setuptools>=81) — pinning setuptools for the watermarker..."
        pip install -q "setuptools<81" || print_warning "couldn't pin setuptools; the watermarker may fail to load"
    fi

    # Prove the stack survived AND that the model code imports against it. A green pip is not enough:
    # --no-deps means pip cannot tell us whether chatterbox can actually run on the versions present.
    python - <<'PY' || { print_error "post-install check FAILED — the torch stack or chatterbox is broken"; deactivate; return 1; }
import sys, torch, transformers, diffusers
print(f"torch {torch.__version__} | transformers {transformers.__version__} | diffusers {diffusers.__version__}")
assert transformers.__version__.split(".")[0] == "4", "transformers moved past 4.x — ACE-Step (music) will break"
import chatterbox.tts  # noqa: F401
# Constructing the model calls perth.PerthImplicitWatermarker(). perth exports it as None when its own
# import failed, so checking `import perth` alone passes on a box where every generation will crash.
import perth
assert perth.PerthImplicitWatermarker is not None, (
    "the perth watermarker did not load (usually pkg_resources / setuptools>=81) — "
    "every generation would fail at model construction")
print("chatterbox + watermarker import cleanly")
PY
    print_success "Voice deps installed"

    # Optional prefetch (~6GB). Otherwise the weights download on the first `voice` request, or from
    # the Download button in Admin -> Voice.
    local DL_VOICE="${VOICE_PREFETCH:-}"
    if [ -z "$DL_VOICE" ]; then
        read -p "Pre-download the voice model now (~6GB)? It downloads on first use otherwise. [y/N]: " DL_VOICE
    fi
    if [[ "$DL_VOICE" =~ ^[Yy] ]]; then
        print_step "Downloading the voice model into the Hugging Face cache..."
        python -c "
import torch
from chatterbox.tts import ChatterboxTTS
_o = torch.load
torch.load = lambda *a, **k: _o(*a, **{**k, 'map_location': 'cpu'})
ChatterboxTTS.from_pretrained(device='cpu')
" && print_success "Voice model cached" \
          || print_warning "Prefetch failed — it will download on first use instead"
    fi
    deactivate
    print_success "Voice cloning ready — enable it in Admin -> Voice and use 'voice <name> <text>'"
    return 0
}
