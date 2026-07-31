"""Zero-shot voice cloning, NATIVE in-process — the same shape as music_local/video_service.

What this is: given a few seconds of reference audio, speak arbitrary text in that voice. It is
ZERO-SHOT, not training: no per-voice checkpoint, no GPU-hours, nothing to version. A "voice" is
just a short reference clip on the user's Blossom drive, so adding one costs a recording and the
library syncs across their devices for free.

Why it is not edge-tts. `tts_service` is `edge_tts` — Microsoft's CLOUD voices. It costs us no GPU
and its voice list is fixed, which is exactly why it stays the default for narration. This is the
first LOCAL speech model in the stack, it competes for the same single GPU as chat/image/music/video,
and on an A770 it runs at roughly 10x realtime. So it is only ever reached by an explicit request
(the `voice` command / the AI-chat studio), never by ordinary narration.

PORTABILITY — the whole point, and where upstream gets it wrong.
`ChatterboxTTS.from_local` only forces CPU deserialisation for `device in ["cpu","mps"]`; anything
else is assumed to be CUDA. The published checkpoints carry CUDA storage tags, so on an Arc box the
load dies with *"Attempting to deserialize object on a CUDA device but torch.cuda.is_available() is
False"* before a single weight lands. We therefore force EVERY torch.load in the load path onto the
CPU and let upstream's own `.to(device)` move it. That one change is what makes Arc(XPU), NVIDIA
(CUDA) and AMD(ROCm — which reports as `cuda` to torch) all take the identical code path; none of
them is special-cased, so none of them can silently rot.

Stock transformers + SDPA only, like image/video/music — no flash-attn, no fp8, no GGUF.

Dependency trap: chatterbox-tts pins `torch==2.6.0`, `transformers==5.2.0`, `diffusers==0.29.0` and
`gradio`. Installing it normally REPLACES torch-XPU and breaks music and video generation on the same
box. It is installed `--no-deps` (see install.sh / requirements.txt), which works because the API it
actually touches — LlamaModel/GPT2Model/AutoTokenizer/GenerationMixin, and diffusers' Attention +
LoRACompatibleLinear — is present in the versions we pin.
"""
from __future__ import annotations

import gc
import logging
import os
import threading
from typing import Optional

from sqlalchemy.orm import Session

from app.services import settings_store
from app.services.music_local import _install_torchaudio_save_shim

logger = logging.getLogger(__name__)

# Chatterbox emits 24kHz mono; the real value comes off the loaded model, this is only the fallback
# for callers that need a rate before anything is loaded.
_SAMPLE_RATE_FALLBACK = 24000
_load_lock = threading.Lock()
_available: Optional[bool] = None

DEFAULT_MODEL = "ResembleAI/chatterbox"


def is_available() -> bool:
    """True when THIS process can actually load the voice model — i.e. `chatterbox` is installed.

    Probes the TOP-LEVEL package only and memoises, for the same reason music_local does: importing
    `chatterbox.tts` to check drags in torch, and this is called from settings/status paths that must
    stay cheap. Probe the package the LOAD PATH uses — testing something adjacent is how the music
    node ended up dialling a sidecar that no longer existed.
    """
    global _available
    if _available is None:
        try:
            import importlib.util
            _available = importlib.util.find_spec("chatterbox") is not None
        except Exception:
            _available = False
    return _available


def _get_settings(db: Session) -> dict:
    s = settings_store.all_settings()
    def _f(key, default):
        try:
            return float(s.get(key) or default)
        except (TypeError, ValueError):
            return default
    return {
        "model": (s.get("voice_model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        "device": (s.get("voice_device") or "auto").strip() or "auto",
        # Chatterbox's two dials. exaggeration drives how much emotion is carried over from the
        # reference; cfg_weight trades similarity against stability (higher = closer to the
        # reference but more prone to artefacts).
        "exaggeration": _f("voice_exaggeration", 0.5),
        "cfg_weight": _f("voice_cfg_weight", 0.5),
        "temperature": _f("voice_temperature", 0.8),
        "max_chars": int(_f("voice_max_chars", 800)),
    }


class VoiceService:
    def __init__(self):
        self._model = None
        self._model_id: Optional[str] = None
        self._device: Optional[str] = None
        self._sample_rate = _SAMPLE_RATE_FALLBACK

    def is_loaded(self) -> bool:
        return self._model is not None

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def _resolve_device(self, device_setting: str) -> str:
        """auto → whatever the box has. ROCm reports itself as `cuda` to torch, so it needs no case
        of its own here — and deliberately does not get one, because a backend with a special case is
        a backend that breaks the next time this function is edited."""
        from app.services.diffusers_service import detect_device
        import torch
        if device_setting == "auto":
            return detect_device()
        if device_setting == "cuda" and not torch.cuda.is_available():
            return detect_device()
        if device_setting == "xpu" and not (hasattr(torch, "xpu") and torch.xpu.is_available()):
            return detect_device()
        return device_setting

    def load_model(self, db: Session):
        cfg = _get_settings(db)
        model_id = cfg["model"]
        with _load_lock:
            if self._model is not None and self._model_id == model_id:
                return self._model
            self._unload_internal()
            device = self._resolve_device(cfg["device"])
            _install_torchaudio_save_shim()   # torchaudio>=2.9 routes save() through torchcodec, which we don't ship

            import torch
            from chatterbox.tts import ChatterboxTTS

            logger.info("[voice] loading %s on %s…", model_id, device)
            # See the module docstring: upstream only forces a CPU map_location for cpu/mps, so every
            # other accelerator inherits the checkpoint's CUDA storage tags and dies on load. Forcing
            # it here (rather than passing device="cpu" and moving things ourselves) keeps upstream's
            # own `.to(device)` in charge of placement — we change WHERE the bytes are read, not what
            # the library does with them.
            _orig_load = torch.load

            def _cpu_load(*a, **kw):
                kw["map_location"] = "cpu"
                return _orig_load(*a, **kw)

            torch.load = _cpu_load
            try:
                self._model = ChatterboxTTS.from_pretrained(device=device)
            finally:
                torch.load = _orig_load

            self._model_id = model_id
            self._device = device
            self._sample_rate = int(getattr(self._model, "sr", _SAMPLE_RATE_FALLBACK) or _SAMPLE_RATE_FALLBACK)
            logger.info("[voice] loaded on %s (%dHz)", device, self._sample_rate)
            return self._model

    def unload_model(self):
        with _load_lock:
            self._unload_internal()

    def _unload_internal(self):
        """Caller MUST hold _load_lock (load_model reuses it; the lock is not reentrant).

        ChatterboxTTS is a plain wrapper object, NOT an nn.Module — it has no `.to()` and no
        `.cpu()`. This is the exact trap that left ~6.3GB of ACE-Step resident on the shared GPUs:
        calling a method the object doesn't have raises into an except and frees nothing, precisely
        when we are swapping the LLM or image model back in. The weights hang off these attributes,
        so drop them by name.
        """
        if self._model is None:
            return
        for attr in ("t3", "s3gen", "ve", "conds", "watermarker", "tokenizer"):
            try:
                if getattr(self._model, attr, None) is not None:
                    setattr(self._model, attr, None)
            except Exception:
                pass
        self._model = None
        self._model_id = None
        gc.collect()
        try:
            import torch
            if self._device == "cuda" and torch.cuda.is_available():
                torch.cuda.empty_cache()
            elif self._device == "xpu" and hasattr(torch, "xpu") and torch.xpu.is_available():
                torch.xpu.empty_cache()
        except Exception:
            pass
        try:
            from app.services.vram_manager import reset_vram_mode
            reset_vram_mode()
        except Exception:
            pass
        logger.info("[voice] model unloaded")

    def generate(self, db: Session, text: str, reference_path: str) -> bytes:
        """Speak `text` in the voice of `reference_path`. Returns WAV bytes. BLOCKING — callers run
        it off the event loop and under the shared GPU lock (see voice_factory)."""
        cfg = _get_settings(db)
        text = (text or "").strip()
        if not text:
            raise ValueError("nothing to say")
        # A cap, not a chunker. The model degrades on very long single passes and holds the GPU the
        # whole time; the caller splits paragraphs if it wants more.
        if len(text) > cfg["max_chars"]:
            text = text[:cfg["max_chars"]]
        if not reference_path or not os.path.exists(reference_path):
            raise ValueError("reference clip is missing")

        model = self.load_model(db)
        import io
        import torch
        import soundfile as sf

        with torch.no_grad():
            wav = model.generate(
                text,
                audio_prompt_path=reference_path,
                exaggeration=cfg["exaggeration"],
                cfg_weight=cfg["cfg_weight"],
                temperature=cfg["temperature"],
            )
        # soundfile, not torchaudio.save — same reasoning as the shim above, and it keeps the bytes
        # in memory instead of round-tripping a temp file.
        arr = wav.detach().to("cpu").float().numpy()
        if arr.ndim > 1:
            arr = arr.squeeze(0)
        buf = io.BytesIO()
        sf.write(buf, arr, self._sample_rate, format="WAV", subtype="PCM_16")
        return buf.getvalue()


_service: Optional[VoiceService] = None


def get_voice_service() -> VoiceService:
    global _service
    if _service is None:
        _service = VoiceService()
    return _service


def unload() -> None:
    """Drop the voice model — called by the vram_manager swaps when another task needs the GPU."""
    if _service is not None:
        _service.unload_model()


def download_model(db: Session, model_id: Optional[str] = None) -> dict:
    """Pre-fetch the weights so the first real request isn't a silent multi-GB stall.

    Downloads WITHOUT placing anything on the GPU: it loads on the CPU and immediately drops the
    reference, so this can run while a generation is in flight without fighting for VRAM or the GPU
    lock. Returns {ok, model, bytes} for the admin button.
    """
    if not is_available():
        raise RuntimeError("the voice model package (chatterbox-tts) is not installed on this node")
    model_id = (model_id or _get_settings(db)["model"]).strip() or DEFAULT_MODEL
    _install_torchaudio_save_shim()
    import torch
    from chatterbox.tts import ChatterboxTTS

    _orig_load = torch.load

    def _cpu_load(*a, **kw):
        kw["map_location"] = "cpu"
        return _orig_load(*a, **kw)

    torch.load = _cpu_load
    try:
        m = ChatterboxTTS.from_pretrained(device="cpu")
    finally:
        torch.load = _orig_load
    for attr in ("t3", "s3gen", "ve", "conds", "watermarker", "tokenizer"):
        try:
            setattr(m, attr, None)
        except Exception:
            pass
    del m
    gc.collect()
    return {"ok": True, "model": model_id, "bytes": cache_size()}


def cache_size(model_id: Optional[str] = None) -> int:
    """Bytes the voice weights occupy in the HF cache — what the admin tab shows as "downloaded".

    Derived from the CONFIGURED model id, not hardcoded: pointing `voice_model` at a different repo
    and still measuring the old one would report a model that isn't there as downloaded, and the first
    request would then stall on a multi-GB fetch the admin thought they had already done.
    """
    total = 0
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
        mid = (model_id or settings_store.get("voice_model", "") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        root = os.path.join(HF_HUB_CACHE, "models--" + mid.replace("/", "--"))
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
    except Exception:
        pass
    return total


def is_downloaded() -> bool:
    """Weights already on this node. The threshold is a sanity floor, not a checksum — a partial
    download leaves a few KB of refs behind and would otherwise read as "ready"."""
    return cache_size() > 64 * 1024 * 1024
