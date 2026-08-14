"""
Native LLM Service using llama-cpp-python with GPU acceleration.
Supports Intel Arc (SYCL), NVIDIA (CUDA), and CPU fallback.
"""
import asyncio
import json
import logging
import os as _os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncGenerator, Optional, List, Dict, Any
from sqlalchemy.orm import Session

# Gated context-reset helper: resets only on the SYCL/Arc build that needs the crash workaround,
# a no-op on CUDA (nas) so the prompt-prefix cache is kept (much faster prefill). tool_calling has
# no app-level imports, so this module-level import can't create a cycle.
from app.services.tool_calling import reset_context_if_needed

# Configure logging
logger = logging.getLogger("llama_service")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [LLAMA] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(handler)


# Global model instance (singleton)
_llama_instance: Optional["LlamaService"] = None
_executor = ThreadPoolExecutor(max_workers=8)  # More workers to match concurrency

# Concurrency control - semaphore allows N concurrent inferences
_inference_semaphore: Optional[threading.Semaphore] = None
_current_max_concurrent = 1

# Idle timeout tracking
_last_used: float = 0
_idle_check_thread: Optional[threading.Thread] = None
_idle_check_stop = threading.Event()

# Request tracking for smart unloading
_pending_requests: int = 0
_request_counter_lock = threading.Lock()


def _start_idle_check():
    """Start the background idle check thread"""
    global _idle_check_thread
    if _idle_check_thread is not None and _idle_check_thread.is_alive():
        return
    _idle_check_stop.clear()
    _idle_check_thread = threading.Thread(target=_idle_check_loop, daemon=True)
    _idle_check_thread.start()
    logger.info("LLM idle check thread started")


def _idle_check_loop():
    """Background loop to check for idle timeout and unload model"""
    global _llama_instance, _last_used
    while not _idle_check_stop.wait(30):  # Check every 30 seconds
        if _llama_instance is not None and _llama_instance._model is not None:
            # NEVER unload while a request is in flight. A single long generation (huge context →
            # slow prefill, or a rambling agentic step) can run LONGER than the idle window, and
            # `_last_used` is only refreshed when a generation COMPLETES — so without this guard the
            # idle clock goes stale mid-stream and we unload the model out from under the live
            # request, dropping the connection (client sees RemoteProtocolError and "just stops").
            # `_pending_requests` brackets the whole generation (inc at start, dec in finally), so
            # >0 means a stream is still running; skip this tick and re-check in 30s.
            with _request_counter_lock:
                pending = _pending_requests
            if pending > 0:
                continue
            idle_time = time.time() - _last_used
            timeout = _llama_instance._idle_timeout
            if timeout > 0 and idle_time > timeout:
                logger.info(f"LLM idle for {idle_time:.0f}s (>{timeout}s), unloading to free VRAM")
                # Free UNDER _request_counter_lock and re-check pending atomically with it. Unlike
                # the image/video services, _ensure_model_loaded takes no internal lock (load/unload
                # is serialized by the external GPU lock, which this idle thread does NOT hold) — so
                # a plain re-check would still leave a use-after-free window where a just-incremented
                # request reads the model between our check and _close_llama_safe(). Holding the lock
                # across the free closes it: requests increment _pending_requests under this same lock
                # before any inference, so while we hold it none can start, and any that already did
                # make pending>0 (we skip). See diffusers/video for the _load_lock analog.
                _llama_instance._idle_unload_if_free()


def _get_inference_semaphore(max_concurrent: int = 1) -> threading.Semaphore:
    """Get or create inference semaphore with specified concurrency"""
    global _inference_semaphore, _current_max_concurrent
    if _inference_semaphore is None or _current_max_concurrent != max_concurrent:
        _inference_semaphore = threading.Semaphore(max_concurrent)
        _current_max_concurrent = max_concurrent
        logger.info(f"Inference concurrency set to {max_concurrent}")
    return _inference_semaphore


def _close_llama_safe(model: Any) -> None:
    """Close llama-cpp-python model without raising. Handles missing 'sampler' in some versions."""
    if model is None:
        return
    try:
        if hasattr(model, "close") and callable(getattr(model, "close")):
            model.close()
    except (AttributeError, Exception) as e:
        # LlamaModel.close() can raise AttributeError if internal 'sampler' is missing (library bug)
        logger.debug("Model close() raised (ignored): %s", e)


def _read_gguf_metadata(path: str) -> Dict[str, Any]:
    """Minimal, dependency-free GGUF metadata reader.

    Returns a dict of scalar/string metadata key-values (arrays are skipped but
    the file pointer is advanced correctly). Only the metadata section at the
    head of the file is read - never the tensor data. Returns {} on any problem.
    """
    import struct
    # gguf value type -> (struct format, byte size)
    _scalar = {0: ('<B', 1), 1: ('<b', 1), 2: ('<H', 2), 3: ('<h', 2),
               4: ('<I', 4), 5: ('<i', 4), 6: ('<f', 4), 7: ('<?', 1),
               10: ('<Q', 8), 11: ('<q', 8), 12: ('<d', 8)}
    meta: Dict[str, Any] = {}
    try:
        with open(path, 'rb') as f:
            if f.read(4) != b'GGUF':
                return meta
            struct.unpack('<I', f.read(4))[0]              # version
            struct.unpack('<Q', f.read(8))[0]              # tensor_count
            kv_count = struct.unpack('<Q', f.read(8))[0]

            def read_str() -> str:
                n = struct.unpack('<Q', f.read(8))[0]
                return f.read(n).decode('utf-8', 'replace')

            def skip(t: int) -> None:
                if t == 8:                                  # string
                    n = struct.unpack('<Q', f.read(8))[0]
                    f.seek(n, 1)
                elif t == 9:                                # array
                    sub = struct.unpack('<I', f.read(4))[0]
                    cnt = struct.unpack('<Q', f.read(8))[0]
                    if sub in _scalar:
                        f.seek(_scalar[sub][1] * cnt, 1)
                    else:
                        for _ in range(cnt):
                            skip(sub)
                elif t in _scalar:
                    f.seek(_scalar[t][1], 1)
                else:
                    raise ValueError(f"unknown gguf type {t}")

            for _ in range(kv_count):
                key = read_str()
                vtype = struct.unpack('<I', f.read(4))[0]
                if vtype == 8:
                    meta[key] = read_str()
                elif vtype == 9:
                    skip(9)
                elif vtype in _scalar:
                    meta[key] = struct.unpack(_scalar[vtype][0], f.read(_scalar[vtype][1]))[0]
                else:
                    break  # unknown type, cannot safely continue
    except Exception as e:
        logger.debug("GGUF metadata read failed for %s: %s", path, e)
        return {}
    return meta


def _detect_free_vram_mb() -> Optional[int]:
    """Best-effort free-VRAM detection for the active backend.

    NVIDIA (CUDA/nas) via nvidia-smi; Intel Arc (SYCL) via torch.xpu. Returns the
    most-constrained GPU's free MB, or None if it can't be determined.
    """
    try:
        import subprocess
        r = subprocess.run(
            ['nvidia-smi', '--query-gpu=memory.free', '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            vals = [int(x) for x in r.stdout.strip().split('\n') if x.strip()]
            if vals:
                return min(vals)
    except Exception:
        pass
    try:
        import torch
        if hasattr(torch, 'xpu') and torch.xpu.is_available():
            # Newer runtimes expose true free VRAM; older IPEX torch does not, so fall
            # back to total device memory (the GPU may be shared with the image service,
            # so this is an upper bound - the load retry loop covers any shortfall).
            if hasattr(torch.xpu, 'mem_get_info'):
                free, _total = torch.xpu.mem_get_info()
                return int(free / (1024 * 1024))
            props = torch.xpu.get_device_properties(0)
            total = getattr(props, 'total_memory', 0)
            if total:
                return int(total / (1024 * 1024))
    except Exception:
        pass
    return None


def _detect_free_ram_mb() -> Optional[int]:
    """Best-effort free system-RAM detection (Linux ``/proc/meminfo`` MemAvailable).

    Used to size the agentic context window when a model is larger than VRAM: the overflow
    weights and their KV cache spill to system RAM, so RAM — not VRAM — is the real ceiling
    in that case. Returns available MB, or None if it can't be read.
    """
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) // 1024  # kB -> MB
    except Exception:
        pass
    return None


def _compute_autofit_gpu_layers(model_path: str, file_size: int, n_ctx: int, n_batch: int = 512,
                                flash_attn: bool = False):
    """Decide GPU layers for a model when admin left llm_gpu_layers at -1 ("all").

    Returns (gpu_layers, reason). gpu_layers == -1 means "fits, keep all on GPU /
    obey admin"; a non-negative int means the model+KV won't fit at the configured
    context, so offload to that many GPU layers for THIS load only (context kept).

    ``n_batch`` matters: the SYCL/CUDA compute graph allocates per-op temp buffers
    (e.g. the matmul "reorder" buffer) that scale with batch size and model width and
    live in VRAM ON TOP of weights+KV. Under-reserving for them lets autofit fill VRAM
    so full that the FIRST MUL_MAT can't allocate its temp buffer -> the backend aborts
    and the whole process dies (observed: a 14B at batch 1024 needed ~640MB+ just for
    one reorder buffer, but only ~6% headroom was left). So scale the reserve with batch.
    """
    free_mb = _detect_free_vram_mb()
    if not free_mb:
        return -1, "VRAM unknown; keeping admin setting (-1)"

    meta = _read_gguf_metadata(model_path)

    def find(suffix: str):
        for k, v in meta.items():
            if k.endswith('.' + suffix):
                return v
        return None

    n_layer = find('block_count')
    n_head_kv = find('attention.head_count_kv')
    n_head = find('attention.head_count')
    n_embd = find('embedding_length')

    # Per-head K/V dims: prefer the explicit key_length/value_length (some archs,
    # e.g. Qwen3.5-MoE, set head_dim != embedding_length/head_count). Fall back to
    # embedding_length/head_count only when the explicit values are absent.
    head_dim_fallback = (n_embd / n_head) if (n_embd and n_head) else None
    k_len = find('attention.key_length') or head_dim_fallback
    v_len = find('attention.value_length') or k_len

    weights_mb = file_size / (1024 * 1024)
    # Compute-graph headroom. Base context + fragmentation, PLUS a term that scales with the
    # actual VRAM the per-op temp buffers need: ~batch * width * fp16, times a fudge factor that
    # covers the largest intermediate (FFN/reorder) plus the rest of the graph. Without this a big
    # model at a large batch fills VRAM and the first matmul's temp alloc fails -> backend abort.
    compute_mb = 0.0
    if n_embd:
        compute_mb = (n_batch * n_embd * 2 / (1024 * 1024)) * 96.0
    buffer_mb = 700.0 + compute_mb
    usable_mb = free_mb * 0.94        # safety margin

    kv_mb = None
    if n_layer and n_head_kv and k_len and v_len:
        kv_bytes = n_layer * n_ctx * n_head_kv * (k_len + v_len) * 2  # K+V cache, f16
        kv_mb = kv_bytes / (1024 * 1024)
        # Flash attention stores the KV cache far more compactly than the raw f16 figure: on a
        # 3060 (FA on) this model's measured non-weight VRAM @32k was ~1.25 GB vs the 4.0 GB f16
        # estimate (~0.25x). Without accounting for that, FA-enabled cards look "full" and the
        # autotuner needlessly offloads layers to CPU as context grows. Scale the reserve by 0.5
        # when FA is on — below the f16 figure but well above the measured 0.25x, so we free the
        # phantom headroom while keeping margin against the first-matmul OOM the buffer guards.
        if flash_attn:
            kv_mb *= 0.5

    if kv_mb is None or not n_layer:
        # Insufficient metadata for a precise split; only the load fallback can help.
        return -1, (f"metadata incomplete (weights {weights_mb:.0f}MB, free {free_mb}MB); "
                    f"keeping -1, relying on load fallback")

    total_need = weights_mb + kv_mb + buffer_mb
    if total_need <= usable_mb:
        return -1, (f"fits: ~{total_need:.0f}MB (w{weights_mb:.0f}+kv{kv_mb:.0f}+buf{buffer_mb:.0f}) "
                    f"<= {usable_mb:.0f}MB (free {free_mb}MB) -> full GPU")

    denom = weights_mb + kv_mb
    frac = max(0.0, (usable_mb - buffer_mb) / denom)
    fit = max(0, min(int(n_layer), int(frac * n_layer)))
    return fit, (f"AUTO-TUNE: need ~{total_need:.0f}MB > {usable_mb:.0f}MB (free {free_mb}MB) -> "
                 f"{fit}/{int(n_layer)} GPU layers, ctx {n_ctx} preserved (config unchanged)")


def _compute_autofit_ctx(model_path: str, file_size: int, flash_attn: bool = False,
                         n_batch: int = 512, ctx_floor: int = 4096, ctx_cap: int = 131072,
                         offload_ctx_target: int = 16384, offload_ctx_cap: int = 65536,
                         ram_kv_fraction: float = 0.5, offload_vram_tok_per_mb: float = 1.5):
    """Pick the largest context window that fits THIS GPU's VRAM at full layers.

    Used when ``ollama_num_ctx`` is "auto" (the default): a self-hosted user on a 6GB card
    gets a small full-GPU window; a 24GB card gets a large one — without anyone hand-tuning
    per machine. Mirrors ``_compute_autofit_gpu_layers``' memory model so that whatever ctx we
    pick here, the layer-autofit then confirms "full GPU" (same weights+KV+buffer estimate).
    Result is clamped to ``[ctx_floor, min(model train ctx, ctx_cap)]`` and rounded down to a
    2048 multiple. Falls back to a safe small window when VRAM/metadata can't be read.

    When the model is *larger* than this GPU's VRAM (weights won't fit at full layers), the
    "fits at full layers" math goes negative. The layer-autofit spills the overflow weights to
    system RAM, and the KV cache for those CPU-resident layers lives in RAM too — so the largest
    usable (agentic) window in this case is bounded by **system RAM, not VRAM**. So instead of a
    fixed guess (which left every machine at 16384 regardless of size — too small for opencode's
    large reads/writes on a roomy box, needlessly ambitious on a memory-starved one), size the
    window to what this host can actually hold: (free RAM − overflow weights) × ``ram_kv_fraction``
    worth of KV, clamped to ``offload_ctx_cap``. This auto-scales with the machine — a small-VRAM
    /big-RAM node still gets a big agentic window; a tiny box stays modest. Falls back to
    ``offload_ctx_target`` only when free RAM can't be read.
    """
    free_mb = _detect_free_vram_mb()
    if not free_mb:
        return ctx_floor * 2  # VRAM unknown: conservative 8192

    meta = _read_gguf_metadata(model_path)

    def find(suffix: str):
        for k, v in meta.items():
            if k.endswith('.' + suffix):
                return v
        return None

    n_layer = find('block_count')
    n_head_kv = find('attention.head_count_kv')
    n_head = find('attention.head_count')
    n_embd = find('embedding_length')
    head_dim_fallback = (n_embd / n_head) if (n_embd and n_head) else None
    k_len = find('attention.key_length') or head_dim_fallback
    v_len = find('attention.value_length') or k_len
    train_ctx = find('context_length') or ctx_cap

    if not (n_layer and n_head_kv and k_len and v_len):
        return ctx_floor * 2  # can't size precisely; safe small window

    weights_mb = file_size / (1024 * 1024)
    compute_mb = (n_batch * n_embd * 2 / (1024 * 1024)) * 96.0 if n_embd else 0.0
    buffer_mb = 700.0 + compute_mb
    usable_mb = free_mb * 0.94

    # f16 KV per token (same basis as the layer-autofit). Flash attention stores it more
    # compactly -> scale to match (see _compute_autofit_gpu_layers).
    kv_per_tok_mb = n_layer * n_head_kv * (k_len + v_len) * 2 / (1024 * 1024)
    if flash_attn:
        kv_per_tok_mb *= 0.5

    cap = min(int(train_ctx), ctx_cap)
    if kv_per_tok_mb <= 0:
        return ctx_floor  # can't size KV; smallest window

    avail_for_kv = usable_mb - weights_mb - buffer_mb
    if avail_for_kv <= 0:
        # Model is bigger than this GPU: the layer-autofit spills overflow weights to system
        # RAM, and the KV for those CPU-resident layers lives in RAM too — so the agentic
        # window is bounded by RAM here, not VRAM. Size it to what this host can hold so it
        # auto-scales with the machine (see docstring), falling back to the fixed target only
        # when free RAM is unreadable.
        weights_overflow_mb = weights_mb - max(0.0, usable_mb - buffer_mb)
        free_ram_mb = _detect_free_ram_mb()
        if not free_ram_mb:
            target = min(offload_ctx_target, cap)
        else:
            ram_for_kv = max(0.0, (free_ram_mb - weights_overflow_mb) * ram_kv_fraction)
            target = int(ram_for_kv / kv_per_tok_mb) if ram_for_kv > 0 else ctx_floor
            target = min(target, offload_ctx_cap, cap)
        # VRAM ceiling — only WITHOUT flash attention. Without FA the GPU-resident layers' KV and
        # the attention/compute scratch (which grows with the window) stay in VRAM uncompressed; a
        # purely RAM-sized window then OOMs the backend mid-matmul and crashes the process (observed:
        # 64k on a 16GB Arc/SYCL, FA off, died with UR_RESULT_ERROR_OUT_OF_HOST_MEMORY at OP MUL). So
        # clamp by what VRAM can carry — ~offload_vram_tok_per_mb tokens per free MB (16384 ran fine
        # at ~16GB, 65536 was fatal; 1.5 keeps margin and scales down on smaller cards). With FA on,
        # the KV/scratch is compact enough that the RAM-sized window is safe (observed: a 12GB 3060
        # with FA loads & serves 65536 fine), so clamping there would needlessly starve agentic
        # sessions — skip it and let the RAM/cap bound apply.
        if not flash_attn:
            target = min(target, int(free_mb * offload_vram_tok_per_mb))
        target = max(ctx_floor, target)
        return (target // 2048) * 2048

    max_ctx = int(avail_for_kv / kv_per_tok_mb)
    max_ctx = max(ctx_floor, min(max_ctx, cap))
    max_ctx = (max_ctx // 2048) * 2048  # round down to a clean multiple
    return max(ctx_floor, max_ctx)


def resolve_model_path(requested: Optional[str], default_path: str) -> str:
    """Map a client-requested model name to a local .gguf path.

    Returns default_path when no specific model is requested (web UI / Telegram send
    'native'/'default'/empty), or when the requested name isn't a real model file
    (accept-any: never error, just fall back to the configured default). An exact
    .gguf basename in the same models dir - or an absolute .gguf path - selects that
    model, so an API client like opencode can request a different model per call.
    """
    if not requested:
        return default_path
    req = requested.strip()
    if not req or req.lower() in ("native", "default"):
        return default_path
    if _os.path.isabs(req) and req.endswith(".gguf") and _os.path.isfile(req):
        return req
    cand = _os.path.join(_os.path.dirname(default_path), _os.path.basename(req))
    if cand.endswith(".gguf") and _os.path.isfile(cand):
        return cand
    return default_path


class LlamaService:
    """
    Native LLM inference service using llama-cpp-python.
    Keeps model loaded in memory for fast inference.
    """

    def __init__(self, db: Session):
        self.db = db
        self._model = None
        self._model_path: Optional[str] = None
        self._configured_num_ctx: int = 4096  # Track configured context size
        self._load_settings()
        _start_idle_check()

    def _load_settings(self):
        """Load settings from database"""
        from app.database import safe_query_settings
        self._settings = safe_query_settings(self.db)
        
        # Helper to get setting with fallback for empty strings
        def get_setting(key: str, default: str) -> str:
            val = self._settings.get(key, default)
            return val if val else default

        # Model settings
        self.model_path = get_setting("llm_model_path", "/home/verita84/models/model.gguf")
        self.default_model = get_setting("ollama_model", "native")

        # Context size: "auto" (the default) sizes the window to fit this GPU at full layers;
        # an explicit integer is used verbatim. "auto" is resolved at model-load time (needs the
        # model's KV dims + free VRAM), so here we only flag it and keep a safe fallback so the
        # not-yet-loaded state has a sane value.
        raw_ctx = get_setting("ollama_num_ctx", "auto").strip().lower()
        self._auto_ctx = raw_ctx in ("auto", "0", "")
        configured_num_ctx = 8192 if self._auto_ctx else int(raw_ctx)
        logger.info(f"[LLAMA] _load_settings: configured_num_ctx={'auto' if self._auto_ctx else configured_num_ctx}, _model is None: {self._model is None}")

        # Only update num_ctx when model not loaded (avoid triggering reloads). For auto this is
        # just a pre-load fallback; the loader overwrites it with the VRAM-fitted value.
        if self._model is None:
            self.num_ctx = configured_num_ctx

        # For auto, leave _configured_num_ctx tracking the resolved value (set by the loader) so
        # the reload-detection in _ensure_model_loaded doesn't see a phantom change every request.
        if not self._auto_ctx:
            self._configured_num_ctx = configured_num_ctx
        self.num_predict = int(get_setting("ollama_num_predict", "2048"))

        # GPU settings
        self.n_gpu_layers = int(get_setting("llm_gpu_layers", "-1"))  # -1 = all layers on GPU
        self.max_concurrent = int(get_setting("llm_max_concurrent", "1"))  # Max concurrent inferences

        # CPU settings - auto-detect threads if set to 0
        n_threads_setting = int(get_setting("llm_n_threads", "0"))
        if n_threads_setting <= 0:
            cpu_count = _os.cpu_count() or 4
            # Use physical cores (cpu_count // 2) for better performance
            # SMT/hyperthreading can cause contention during inference
            self.n_threads = max(1, cpu_count // 2)
            logger.info(f"Auto-detected CPU threads: {self.n_threads} (physical cores from {cpu_count} logical)")
        else:
            self.n_threads = n_threads_setting

        # CPU optimization settings
        self.cpu_mode = get_setting("llm_cpu_mode", "false").lower() == "true"
        self.n_batch = int(get_setting("llm_n_batch", "2048"))
        # Intel Arc / SYCL guard: a large prompt-eval batch blows the level_zero HOST-memory pool
        # during the quantize step (UR_RESULT_ERROR_OUT_OF_HOST_MEMORY → the whole process crashes on
        # big agentic/opencode prompts), even though VRAM fits fine — the autofit budget is VRAM-only
        # and can't see that host pool. 1024 was fatal, 256 is proven stable, so cap SYCL to 256 (a
        # lower per-node llm_n_batch is still honored). CUDA/nas (no torch.xpu) is unaffected.
        try:
            import torch as _torch
            if hasattr(_torch, "xpu") and _torch.xpu.is_available() and self.n_batch > 256:
                logger.info(f"  [SYCL] capping n_batch {self.n_batch}->256 (level_zero host-mem OOM guard)")
                self.n_batch = 256
        except Exception:
            pass
        self.use_mmap = get_setting("llm_use_mmap", "true").lower() == "true"
        self.use_mlock = get_setting("llm_use_mlock", "true").lower() == "true"
        self.flash_attn = get_setting("llm_flash_attn", "false").lower() == "true"
        self.disable_thinking = get_setting("llm_disable_thinking", "false").lower() == "true"
        # Opt-in OpenAI function-calling: loads a function-calling chat handler so `tools`
        # are surfaced to the model and tool_calls are parsed back. Off by default so the
        # production chat path is unchanged unless explicitly enabled.
        self.function_calling = get_setting("llm_function_calling", "false").lower() == "true"

        # Sampling settings
        self.temperature = float(get_setting("ollama_temperature", "0.2"))
        self.top_p = float(get_setting("ollama_top_p", "0.9"))
        self.top_k = int(get_setting("ollama_top_k", "40"))
        self.repeat_penalty = float(get_setting("ollama_repeat_penalty", "1.1"))

        # Advanced settings
        self.mirostat = int(get_setting("ollama_mirostat", "0"))
        self.mirostat_eta = float(get_setting("ollama_mirostat_eta", "0.1"))
        self.mirostat_tau = float(get_setting("ollama_mirostat_tau", "5.0"))
        seed_str = get_setting("ollama_seed", "")
        self.seed = int(seed_str) if seed_str.strip() else -1

        # Stop sequences — start with user-configured values.
        user_stop = [s.strip() for s in get_setting("ollama_stop", "").split(",") if s.strip()]

        # For Mistral-family models add the correct end-of-turn stop strings.
        # Use the full token strings, never bare "[" or "]" — those would cut off
        # mid-generation whenever the model outputs any bracketed text.
        model_name_lower = _os.path.basename(self.model_path).lower()
        if "mistral" in model_name_lower:
            mistral_stops = ["[INST]", "[/INST]", "</s>"]
            self.stop_sequences = list(dict.fromkeys(user_stop + mistral_stops))  # preserve order, no dupes
        else:
            self.stop_sequences = user_stop

        # Idle timeout for automatic unloading (0 = disabled)
        self._idle_timeout = int(get_setting("llm_idle_timeout", "0"))

        # Token timeout for streaming (max seconds between tokens)
        self.token_timeout = int(get_setting("llm_token_timeout", "600"))

        # System prompt
        self.system_prompt = get_setting("ollama_system_prompt", "You are a helpful, friendly AI assistant.")

    def _ensure_model_loaded(self, target_path: Optional[str] = None):
        """Load model if not already loaded, path changed, or configured context size changed.

        target_path is the per-request model to load (resolved from the API `model` field).
        Using a passed-in local instead of the shared self.model_path makes concurrent
        mixed-model requests race-free: the load decision and target never depend on shared
        mutable state (self._model_path is written only here, serialized by the GPU lock).
        """
        target_path = target_path or self.model_path
        # Check if configured context differs from what model was loaded with.
        # llama.cpp rounds n_ctx up (e.g. 32024 -> 32256), so only reload when the loaded
        # context is SMALLER than configured (can't serve the request). A rounded-up larger
        # ctx is fine - comparing != caused a needless 13GB reload on every request.
        actual_model_ctx = self._model.n_ctx() if self._model is not None else 0
        configured_changed = self._model is not None and (self._configured_num_ctx != self.num_ctx or actual_model_ctx < self.num_ctx)
        if self._model is not None and self._model_path == target_path and not configured_changed:
            return

        if configured_changed:
            logger.info(f"Configured context size changed from {self._configured_num_ctx} to {self.num_ctx}, reloading model...")

        # THE VRAM SWAP BELONGS HERE — under whichever GPU lock the caller is holding, and next to
        # the load it exists to make room for. It used to be the CALLER's job (`prepare_vram_for_llm`
        # in chat_service), where it ran with no lock at all and unloaded the image model out from
        # under a diffusers run that held one: image job dead, llama.cpp aborted on the wreckage,
        # whole service core-dumped (2026-08-14 09:13). Here it is reached only from
        # chat_completion / chat_completion_stream / stream_chat_content, all of which hold the lock
        # across this call — and only on the branch that is ACTUALLY going to load, so a warm model
        # still costs nothing. Never raises: a swap that fails must not take inference with it.
        try:
            from app.services.vram_manager import prepare_for_llm
            prepare_for_llm(self.db)
        except Exception as e:
            logger.warning(f"VRAM prepare for LLM failed, loading anyway: {e}")

        # Unload previous model
        if self._model is not None:
            logger.info(f"Unloading previous model: {self._model_path}")
            _close_llama_safe(self._model)
            self._model = None

        logger.info(f"Loading model: {target_path}")
        logger.info(f"  Context size: {self.num_ctx}")
        logger.info(f"  GPU layers: {self.n_gpu_layers}")
        logger.info(f"  CPU threads: {self.n_threads}")

        # Validate model file before attempting to load
        import os
        from pathlib import Path

        model_path_obj = Path(target_path)
        if not model_path_obj.exists():
            error_msg = f"Model file does not exist: {self.model_path}"
            logger.error(error_msg)
            raise FileNotFoundError(error_msg)
        
        if not model_path_obj.is_file():
            error_msg = f"Model path is not a file: {self.model_path}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        # Check file permissions
        if not _os.access(self.model_path, _os.R_OK):
            error_msg = f"Model file is not readable: {self.model_path}"
            logger.error(error_msg)
            logger.error(f"  File permissions: {oct(model_path_obj.stat().st_mode)}")
            logger.error(f"  File owner: UID={model_path_obj.stat().st_uid}, GID={model_path_obj.stat().st_gid}")
            raise PermissionError(error_msg)
        
        # Check file size (should be > 0)
        file_size = model_path_obj.stat().st_size
        logger.info(f"  Model file size: {file_size:,} bytes ({file_size / (1024**3):.2f} GB)")
        if file_size == 0:
            error_msg = f"Model file is empty: {self.model_path}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        
        # Resolve absolute path to avoid path issues
        resolved_path = str(model_path_obj.resolve())
        if resolved_path != self.model_path:
            logger.info(f"  Resolved path: {resolved_path}")
        
        try:
            from llama_cpp import Llama
            import llama_cpp.llama_cpp as llama_cpp_lib

            # Initialize CUDA backend before loading model
            llama_cpp_lib.llama_backend_init()

            # Resolve "auto" context now that the model's KV dims + this GPU's free VRAM are
            # known: size the window to fit this card at full layers. Pin _configured_num_ctx to
            # the resolved value so reload-detection doesn't see a phantom change each request.
            if getattr(self, "_auto_ctx", False) and not self.cpu_mode:
                fitted = _compute_autofit_ctx(resolved_path, file_size, flash_attn=self.flash_attn)
                logger.info(f"  [auto-ctx] sized context to {fitted} for this GPU "
                            f"(flash_attn={self.flash_attn})")
                self.num_ctx = fitted
                self._configured_num_ctx = fitted

            # Use admin-configured (or auto-fitted) context size
            logger.info(f"  Using context size: {self.num_ctx}")

            # Determine GPU layers - force 0 if CPU mode enabled
            gpu_layers = 0 if self.cpu_mode else self.n_gpu_layers
            # Auto-tune: when admin leaves gpu_layers at -1 ("all"), check whether the
            # model + KV cache (at the configured context) actually fits this node's VRAM.
            # If it fits -> keep -1 (obey admin / full GPU). If too big -> override the
            # GPU/CPU split for THIS load only (saved config is never changed), keeping
            # the configured context size fixed.
            if not self.cpu_mode and self.n_gpu_layers == -1:
                autofit_layers, autofit_reason = _compute_autofit_gpu_layers(
                    resolved_path, file_size, self.num_ctx, flash_attn=self.flash_attn)
                logger.info(f"  [autofit] {autofit_reason}")
                gpu_layers = autofit_layers
            logger.info(f"  GPU layers: {gpu_layers} (CPU mode: {self.cpu_mode})")
            logger.info(f"  Batch size: {self.n_batch}, mmap: {self.use_mmap}, mlock: {self.use_mlock}, flash_attn: {self.flash_attn}")

            # Validate context size - warn if very large
            if self.num_ctx > 8192:
                logger.warning(f"  WARNING: Large context size ({self.num_ctx}) may cause memory issues")
                logger.warning(f"  Consider reducing ollama_num_ctx to 4096 or 2048 if you encounter 'Failed to create llama_context' errors")
            
            # Check available GPU memory if using GPU
            if gpu_layers > 0:
                try:
                    import subprocess
                    result = subprocess.run(['nvidia-smi', '--query-gpu=memory.free', '--format=csv,noheader,nounits'], 
                                          capture_output=True, text=True, timeout=5)
                    if result.returncode == 0:
                        free_memory_mb = int(result.stdout.strip().split('\n')[0])
                        logger.info(f"  Available GPU memory: {free_memory_mb} MB")
                        # Better estimate: KV cache for 14B model ≈ context_size * 2 bytes * layers * hidden_dim
                        # Simplified: ~0.2-0.3 MB per 1000 tokens for 14B models (varies by quantization)
                        # For Q4_K_M 14B: roughly 0.25 MB per 1000 tokens
                        estimated_kv_cache_mb = int((self.num_ctx / 1000) * 0.25)
                        if estimated_kv_cache_mb > free_memory_mb * 0.5:
                            logger.warning(f"  WARNING: Context size {self.num_ctx} may require ~{estimated_kv_cache_mb} MB for KV cache")
                            logger.warning(f"  Available: {free_memory_mb} MB (model weights need ~8-9GB for 14B Q4_K_M)")
                            logger.warning(f"  Consider reducing context size to 4096-8192 for 12GB GPUs")
                except Exception:
                    pass  # nvidia-smi not available or failed, skip check

            # Try loading with automatic context size reduction on failure
            context_sizes_to_try = [self.num_ctx]
            # Add fallback sizes for memory issues
            if self.num_ctx > 32768:
                context_sizes_to_try.extend([32768, 16384, 8192, 4096, 2048])
            elif self.num_ctx > 16384:
                context_sizes_to_try.extend([16384, 8192, 4096, 2048])
            elif self.num_ctx > 8192:
                context_sizes_to_try.extend([8192, 4096, 2048])
            elif self.num_ctx > 4096:
                context_sizes_to_try.extend([4096, 2048])
            elif self.num_ctx > 2048:
                context_sizes_to_try.append(2048)
            
            last_error = None
            # If GPU layers is -1 (all layers) and we have a large model, this might fail
            # 14B Q4_K_M is ~8-9GB, which with system overhead might not fit in 12GB GPU
            if gpu_layers == -1 and file_size > 8_000_000_000:  # > 8GB model file
                logger.warning(f"  WARNING: Large model ({file_size / 1e9:.1f}GB) with all GPU layers (-1) may not fit in VRAM")
                logger.warning(f"  Consider setting llm_gpu_layers to 20-30 for 12GB GPUs")
            
            for attempt_ctx in context_sizes_to_try:
                try:
                    # Use resolved path for loading
                    if attempt_ctx != self.num_ctx:
                        logger.warning(f"  Retrying with reduced context size: {attempt_ctx} (original: {self.num_ctx})")
                    else:
                        logger.info(f"  Attempting to load model from: {resolved_path}")
                    
                    chat_handler = None
                    _model_lower = _os.path.basename(resolved_path).lower()
                    if self._should_use_mistral_template():
                        try:
                            from llama_cpp.llama_chat_format import get_chat_completion_handler
                            chat_handler = get_chat_completion_handler("mistral")
                            logger.info("  Using mistral chat handler for template")
                        except Exception as e:
                            logger.warning(f"  Could not load mistral chat handler: {e}")
                        _chat_format = None
                    elif "qwen3" in _model_lower:
                        # Always plain chatml. Tool-calling is handled by the tool_calling layer
                        # (app/services/tool_calling.py) - llama-cpp's chatml-function-calling
                        # handler mismatches Qwen's native <tool_call> format.
                        _chat_format = "chatml"
                    else:
                        _chat_format = None

                    self._model = Llama(
                        model_path=resolved_path,
                        n_ctx=attempt_ctx,
                        n_gpu_layers=gpu_layers,
                        n_threads=self.n_threads,
                        n_threads_batch=self.n_threads,
                        n_batch=self.n_batch,
                        use_mmap=self.use_mmap,
                        use_mlock=self.use_mlock,
                        flash_attn=self.flash_attn,
                        offload_kqv=True,
                        verbose=False,
                        chat_handler=chat_handler,
                        chat_format=_chat_format,
                    )
                    logger.info(f"[LLAMA] Model loaded with n_ctx={self._model.n_ctx()}")
                    # Success - update num_ctx if we used a smaller value
                    if attempt_ctx != self.num_ctx:
                        logger.warning(f"  Model loaded with reduced context size: {attempt_ctx} (configured: {self.num_ctx})")
                        logger.warning(f"  Consider updating ollama_num_ctx in admin settings to {attempt_ctx} to avoid this warning")
                        # Update the instance variable so it uses the working context size
                        self.num_ctx = attempt_ctx
                    break  # Success, exit retry loop
                except ValueError as ve:
                    # Catch ValueError specifically for llama_context errors
                    error_msg = str(ve)
                    logger.error(f"[LLAMA] ValueError caught: {error_msg}")
                    if "llama_context" in error_msg.lower() or "create" in error_msg.lower():
                        last_error = ve
                        if attempt_ctx == context_sizes_to_try[-1]:
                            # Last attempt failed
                            logger.error(f"Failed to create llama context with all attempted sizes:")
                            logger.error(f"  Tried context sizes: {context_sizes_to_try}")
                            logger.error(f"  GPU layers: {gpu_layers}")
                            logger.error(f"  Model: {resolved_path}")
                            logger.error("This usually means even the minimum context size is too large for available memory.")
                            logger.error("Try:")
                            logger.error("  - Reducing GPU layers (llm_gpu_layers) - try 20-30 instead of -1")
                            logger.error("  - Setting llm_cpu_mode to true to use CPU instead")
                            logger.error("  - Checking GPU memory: nvidia-smi")
                            logger.error("  - Reducing batch size (llm_n_batch)")
                            raise RuntimeError(f"Failed to create llama context even with reduced sizes. Last error: {ve}. Try reducing GPU layers or using CPU mode.")
                        # Try next smaller context size
                        continue
                    else:
                        # Not a context error, re-raise
                        raise
                except Exception as e:
                    error_msg = str(e)
                    error_type = type(e).__name__
                    import traceback
                    logger.error(f"Failed to load model (attempting context size {attempt_ctx}): {error_type}: {error_msg}")
                    logger.debug(f"Full exception traceback: {traceback.format_exc()}")
                    logger.error(f"  Model path: {resolved_path}")
                    logger.error(f"  File exists: {model_path_obj.exists()}")
                    logger.error(f"  File readable: {_os.access(self.model_path, _os.R_OK)}")
                    logger.error(f"  File size: {file_size:,} bytes")
                    logger.error(f"  Context size: {attempt_ctx} (configured: {self.num_ctx})")
                    logger.error(f"  GPU layers: {gpu_layers}")
                    
                    # Check if this is a memory-related error that might be fixed by reducing GPU layers
                    is_memory_error = (
                        "memory" in error_msg.lower() or 
                        "allocation" in error_msg.lower() or
                        "cuda" in error_msg.lower() or
                        "out of memory" in error_msg.lower() or
                        "load model from file" in error_msg.lower()
                    )
                    
                    if is_memory_error and attempt_ctx == context_sizes_to_try[-1]:
                        # Last attempt failed - might be GPU layers issue, not just context
                        logger.error("All context size attempts failed. This might be a GPU memory issue.")
                        logger.error("Possible causes:")
                        logger.error("  1. GPU layers (llm_gpu_layers) too high - model weights don't fit in VRAM")
                        logger.error("  2. Context size still too large even after reduction")
                        logger.error("  3. Model file corrupted or incompatible")
                        logger.error("Try:")
                        logger.error(f"  - Reducing GPU layers: Set llm_gpu_layers to 20-30 (currently: {gpu_layers})")
                        logger.error(f"  - Using CPU mode: Set llm_cpu_mode to true")
                        logger.error(f"  - Check GPU memory: nvidia-smi")
                        logger.error(f"  - Verify model file: ls -lh {resolved_path}")
                        raise RuntimeError(f"Failed to load model after trying all context sizes. Last error: {error_type}: {error_msg}. Try reducing GPU layers or using CPU mode.")
                    elif is_memory_error:
                        # Not the last attempt, try next context size
                        logger.warning(f"  Memory-related error with context size {attempt_ctx}, trying smaller size...")
                        continue
                    else:
                        # Non-memory error - likely file corruption or format issue
                        logger.error("This appears to be a non-memory error (file corruption or format issue)")
                        logger.error("Try:")
                        logger.error("  - Verifying model file integrity (re-download if needed)")
                        logger.error("  - Checking file format compatibility with llama-cpp-python version")
                        logger.error(f"  - Checking file: ls -lh {resolved_path}")
                        logger.error(f"  - Testing file: file {resolved_path}")
                        raise
            self._model_path = target_path
            # Initialize last_used time when model loads
            global _last_used
            _last_used = time.time()
            logger.info("Model loaded successfully")

        except Exception as e:
            error_msg = str(e)
            error_type = type(e).__name__
            logger.error(f"Original error loading model: {error_type}: {error_msg}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            
            # Don't re-wrap RuntimeErrors that already have helpful messages
            if isinstance(e, RuntimeError) and ("Try reducing" in error_msg or "Try:" in error_msg or "after trying all" in error_msg):
                # This is already a helpful error message from inner handler, just re-raise
                raise
            
            # Provide helpful error messages for common issues
            if "memory" in error_msg.lower() or "dnnl" in error_msg.lower() or "oneDNN" in error_msg:
                logger.error(f"Memory allocation failed loading model: {e}")
                logger.error("This usually means insufficient GPU/system memory. Try:")
                logger.error("  - Reducing context size (ollama_num_ctx)")
                logger.error("  - Reducing batch size (llm_n_batch)")
                logger.error("  - Using a smaller model")
                logger.error("  - Closing other applications")
                raise RuntimeError(f"Memory allocation failed: {e}. Try reducing context/batch size or using a smaller model.")
            elif "llama_context" in error_msg.lower() or "create.*context" in error_msg.lower():
                logger.error(f"Failed to create llama context: {e}")
                logger.error("This usually means:")
                logger.error("  - Context size (ollama_num_ctx) is too large for available memory")
                logger.error("  - GPU memory is insufficient or fragmented")
                logger.error("  - Model file may be corrupted")
                logger.error("Try:")
                logger.error("  - Reducing context size (ollama_num_ctx) - try 2048 or 4096")
                logger.error("  - Reducing GPU layers (llm_gpu_layers) - try 20-30 instead of -1")
                logger.error("  - Setting llm_cpu_mode to true to use CPU instead")
                logger.error("  - Checking GPU memory: nvidia-smi")
                raise RuntimeError(f"Failed to create llama context: {e}. Try reducing context size or GPU layers.")
            elif ("No such file" in error_msg or "not found" in error_msg.lower()) and not _os.path.exists(self.model_path):
                logger.error(f"Model file not found: {self.model_path}")
                raise FileNotFoundError(f"Model file not found: {self.model_path}")
            elif "load model from file" in error_msg.lower() or "failed to load" in error_msg.lower():
                logger.error(f"Failed to load model from file: {e}")
                logger.error(f"  Model path: {self.model_path}")
                if _os.path.exists(self.model_path):
                    stat = _os.stat(self.model_path)
                    logger.error(f"  File exists: Yes")
                    logger.error(f"  File size: {stat.st_size:,} bytes ({stat.st_size / (1024**3):.2f} GB)")
                    logger.error(f"  File readable: {_os.access(self.model_path, _os.R_OK)}")
                    logger.error(f"  File permissions: {oct(stat.st_mode)}")
                else:
                    logger.error(f"  File exists: No")
                logger.error("Possible causes:")
                logger.error("  1. Model file is corrupted or incomplete")
                logger.error("  2. Insufficient GPU/system memory")
                logger.error("  3. Model format incompatible with llama-cpp-python version")
                logger.error("  4. Context size too large for available memory")
                logger.error("Try:")
                logger.error("  - Verifying model file: file " + self.model_path)
                logger.error("  - Reducing context size (ollama_num_ctx) - try 2048 or 4096")
                logger.error("  - Reducing GPU layers (llm_gpu_layers) - try 20-30 instead of -1")
                logger.error("  - Using CPU mode temporarily (llm_cpu_mode=true)")
                raise RuntimeError(f"Failed to load model from file: {e}. Check file integrity and memory settings.")
            else:
                logger.error(f"Failed to load model: {e}")
                raise

    def _get_sampling_params(self, **overrides) -> Dict[str, Any]:
        """Get sampling parameters with optional overrides"""
        params = {
            "temperature": overrides.get("temperature", self.temperature),
            "top_p": overrides.get("top_p", self.top_p),
            "top_k": overrides.get("top_k", self.top_k),
            "repeat_penalty": overrides.get("repeat_penalty", self.repeat_penalty),
            "max_tokens": overrides.get("max_tokens", self.num_predict),
        }

        # Pass through OpenAI function-calling args when present (handled by the
        # function-calling chat handler the model was loaded with).
        if overrides.get("tools"):
            params["tools"] = overrides["tools"]
        if overrides.get("tool_choice") is not None:
            params["tool_choice"] = overrides["tool_choice"]

        # Add mirostat if enabled
        if self.mirostat > 0:
            params["mirostat_mode"] = self.mirostat
            params["mirostat_eta"] = self.mirostat_eta
            params["mirostat_tau"] = self.mirostat_tau

        # Add seed if set
        if self.seed >= 0:
            params["seed"] = self.seed

        # Add stop sequences — merge caller overrides with model-specific stops so that
        # cross-model requests (e.g. a Qwen stop token sent to a Mistral model) don't
        # silently drop the model's own end-of-turn tokens.
        override_stop = overrides.get("stop") or []
        if isinstance(override_stop, str):
            override_stop = [override_stop]
        stop = list(dict.fromkeys(list(override_stop) + list(self.stop_sequences)))

        if stop:
            params["stop"] = stop

        return params

    def strip_thinking_tags(self, response: str) -> str:
        """Strip thinking tags from AI response"""
        from app.services.text_utils import strip_thinking_tags
        return strip_thinking_tags(response)

    def _should_use_mistral_template(self) -> bool:
        """Check if Mistral chat template should be used for this model."""
        model_name = _os.path.basename(self.model_path).lower()
        return "mistral" in model_name or "mistral" in self._settings.get("chat_template", "").lower()

    def _embed_system_for_mistral(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Embed system message content into the first user message for Mistral models.

        Mistral's chat_handler applies [INST]...[/INST] formatting but ignores the
        'system' role in many llama-cpp-python builds.  Prepending system content
        to the first user message ensures it lands inside the first [INST] block
        without double-templating (the handler still does the actual formatting).
        """
        if not self._should_use_mistral_template():
            return messages

        system_content = ""
        filtered = []
        for msg in messages:
            if msg.get("role") == "system":
                system_content += msg.get("content", "") + "\n\n"
            else:
                filtered.append(dict(msg))  # copy so we don't mutate caller's list

        if not system_content:
            return messages  # nothing to embed

        system_content = system_content.strip()

        if filtered and filtered[0].get("role") == "user":
            filtered[0]["content"] = system_content + "\n\n" + filtered[0].get("content", "")
        else:
            filtered.insert(0, {"role": "user", "content": system_content + "\n\nRespond helpfully."})

        return filtered

    def _format_mistral_template(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Format messages for Mistral-style models using llama-cpp-python's built-in handler."""
        if not self._should_use_mistral_template():
            return messages
        
        system_content = ""
        filtered_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                system_content += msg.get("content", "") + "\n\n"
            else:
                filtered_messages.append(msg)
        
        if system_content:
            system_content = system_content.strip()
            if filtered_messages and filtered_messages[0].get("role") == "user":
                first_user = filtered_messages[0]
                first_user["content"] = system_content + first_user.get("content", "")
            else:
                filtered_messages.insert(0, {"role": "user", "content": system_content + " Respond helpfully."})
        
        if not filtered_messages:
            return [{"role": "user", "content": "Hello"}]
        
        try:
            from llama_cpp.llama_chat_format import get_chat_completion_handler
            handler = get_chat_completion_handler("mistral")
            
            formatted = handler.format_messages(filtered_messages)
            return [{"role": "user", "content": formatted}]
        except Exception as e:
            logger.warning(f"Mistral template handler failed: {e}, falling back to manual format")
            return self._manual_format_mistral(messages)

    def _manual_format_mistral(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Manual formatting if handler fails."""
        formatted = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if not content:
                continue
            
            if role == "system":
                formatted.append({
                    "role": "system", 
                    "content": f"<<sys>>\n{content.strip()}\n<</sys>>"
                })
            elif role == "user":
                formatted.append({
                    "role": "user",
                    "content": f"[INST] {content.strip()} [/INST]"
                })
            elif role == "assistant":
                formatted.append({
                    "role": "assistant",
                    "content": content.strip()
                })
        return formatted

    def _build_no_think_prompt(self, messages: List[Dict[str, Any]]) -> str:
        """Build a raw ChatML prompt with an empty <think> block pre-filled."""
        parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            if role == "user":
                content = content.replace(" /no_think", "").replace("\n/no_think", "").strip()
            parts.append(f"<|im_start|>{role}\n{content}<|im_end|>")
        parts.append("<|im_start|>assistant\n<think>\n\n</think>\n\n")
        return "\n".join(parts)

    def _sync_chat_completion_no_unload(self, messages: List[Dict[str, Any]], target_path: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        """Synchronous chat completion without unloading (caller handles unload)"""
        self._ensure_model_loaded(target_path)
        params = self._get_sampling_params(**kwargs)
        # Embed system message into first user message for Mistral (chat_handler ignores system role).
        # The handler then applies [INST]...[/INST] — we do NOT call format_messages() to avoid double-templating.
        messages = self._embed_system_for_mistral(messages)

        _model_lower = _os.path.basename(target_path or self.model_path).lower()
        # The raw-prompt prefill path can't carry tools, so fall back to the chat path
        # (function-calling handler) whenever tool definitions are present.
        # "coder" = non-thinking Qwen3 (Qwen3-Coder); the <think></think> prefill makes it emit
        # an immediate stop -> empty output. Only thinking-capable qwen3 models need the prefill.
        use_prefill = self.disable_thinking and "qwen3" in _model_lower and "coder" not in _model_lower and not kwargs.get("tools")
        with _get_inference_semaphore(self.max_concurrent):
            try:
                if use_prefill:
                    raw_prompt = self._build_no_think_prompt(messages)
                    try:  # clean context: 0.3.28 SYCL mishandles cross-request reuse (broadcast error)
                        reset_context_if_needed(self._model)
                    except Exception:
                        pass
                    result_raw = self._model.create_completion(prompt=raw_prompt, **params)
                    content = result_raw["choices"][0]["text"]
                    content = self.strip_thinking_tags(content)
                    result = {
                        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                        "object": "chat.completion",
                    }
                elif params.get("tools"):
                    # Qwen/Hermes tool-calling: inject tools, plain-chatml generate, parse.
                    from app.services.tool_calling import generate_message
                    _tools = params.pop("tools"); params.pop("tool_choice", None)
                    _m, _finish = generate_message(self._model, messages, _tools, params, self.strip_thinking_tags, self.disable_thinking)
                    result = {
                        "choices": [{"message": _m, "finish_reason": _finish}],
                        "object": "chat.completion",
                    }
                else:
                    # SYCL/Arc: reset the reused context before the chat path too (the prefill and
                    # tools paths already do; without it cross-request reuse can yield empty output).
                    reset_context_if_needed(self._model)
                    result = self._model.create_chat_completion(messages=messages, **params)
                    # content is None when the model returns tool_calls - only strip text.
                    _msg = result["choices"][0]["message"]
                    if _msg.get("content"):
                        _msg["content"] = self.strip_thinking_tags(_msg["content"])

                # Update last used time for idle timeout
                global _last_used
                _last_used = time.time()

                return result

            except Exception as e:
                logger.error(f"Chat completion error: {e}")
                return {
                    "error": {
                        "message": str(e),
                        "type": "inference_error"
                    }
                }

    def _sync_chat_completion(self, messages: List[Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        """Synchronous chat completion (runs in thread pool) - legacy, unloads after"""
        result = self._sync_chat_completion_no_unload(messages, **kwargs)
        self.unload_model()
        return result

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        stream: bool = False,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Non-streaming chat completion.
        Returns OpenAI-compatible response format.
        """
        global _pending_requests

        # Resolve the client-requested model (e.g. opencode) to a per-request local; falls
        # back to the admin-configured default. Threaded through as a parameter (not via
        # shared self.model_path) so concurrent mixed-model requests can't clobber it.
        target_path = resolve_model_path(model, self.model_path)

        # Track pending requests
        with _request_counter_lock:
            _pending_requests += 1

        try:
            # Acquire shared GPU lock to prevent LLM and image from running simultaneously
            from app.services.locks import GPUResourceLock
            request_id = f"LLAMA-{uuid.uuid4().hex[:8]}"
            async with GPUResourceLock("LLM", request_id, cpu_mode=self.cpu_mode):
                loop = asyncio.get_event_loop()

                # Run synchronous inference in thread pool (without unloading)
                result = await loop.run_in_executor(
                    _executor,
                    lambda: self._sync_chat_completion_no_unload(messages, target_path=target_path, **kwargs)
                )

                return result
        finally:
            with _request_counter_lock:
                _pending_requests -= 1
                pending_after = _pending_requests

            # Warm-keep: when an idle timeout is configured, leave the model loaded between
            # requests (the idle-check thread unloads after real inactivity). This avoids a
            # full reload on every agent step - essential for slow/large models in a loop.
            # With idle timeout 0, keep the old behavior (unload immediately to free VRAM).
            if pending_after == 0 and self._idle_timeout == 0:
                self.unload_model()
            else:
                logger.info(f"[{request_id}] Keeping model loaded (pending={pending_after}, idle_timeout={self._idle_timeout})")

    async def chat_completion_stream(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """
        Streaming chat completion.
        Yields SSE-formatted chunks compatible with OpenAI API.
        Uses async queue to avoid blocking the event loop.
        """
        global _pending_requests

        # Resolve the client-requested model to a per-request local (not shared state).
        target_path = resolve_model_path(model, self.model_path)

        # Track pending requests
        with _request_counter_lock:
            _pending_requests += 1

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        model_name = model or self.default_model

        queue = asyncio.Queue()
        loop = asyncio.get_event_loop()

        # Acquire shared GPU lock to prevent LLM and image from running simultaneously
        from app.services.locks import GPUResourceLock
        # uuid is already imported at module level
        request_id = f"LLAMA-STREAM-{uuid.uuid4().hex[:8]}"
        try:
            async with GPUResourceLock("LLM", request_id, cpu_mode=self.cpu_mode):
                # MOVED INSIDE THE LOCK. These three ran BEFORE it, and the first of them loads a
                # model — which on a shared GPU means unloading whatever else is resident. So a chat
                # request arriving mid-image-generation tore the image model out from under a run
                # that HELD the lock, and the half-torn-down SYCL context took the whole process
                # with it (ggml_abort → core dump, 2026-08-14 09:13). The lock's entire purpose is
                # that only one model touches the GPU at a time, and a LOAD is exactly the moment
                # that matters. `_ensure_model_loaded`'s own docstring already says it is
                # "serialized by the GPU lock" — on this path it was not.
                self._ensure_model_loaded(target_path)
                params = self._get_sampling_params(**kwargs)
                # Embed system message into first user message for Mistral (chat_handler ignores system role).
                messages = self._embed_system_for_mistral(messages)

                def run_streaming():
                    """Run synchronous generation in thread, put SSE chunks in queue"""
                    token_timeout = self.token_timeout
                    last_token_time = time.time()
                    _mlower = _os.path.basename(target_path).lower()
                    # Tools can't go through the raw-prompt prefill path - use the chat path.
                    _use_pf = self.disable_thinking and "qwen3" in _mlower and "coder" not in _mlower and not params.get("tools")

                    with _get_inference_semaphore(self.max_concurrent):
                        try:
                            if params.get("tools"):
                                # Qwen/Hermes tool-calling: generate (plain chatml) + parse,
                                # then synthesize SSE chunks (tool_calls can't be streamed live).
                                from app.services.tool_calling import generate_message, tool_sse_chunks
                                _tools = params.pop("tools"); params.pop("tool_choice", None)
                                msg, finish = generate_message(self._model, messages, _tools, params, self.strip_thinking_tags, self.disable_thinking)
                                for _line in tool_sse_chunks(completion_id, created, model_name, msg, finish):
                                    loop.call_soon_threadsafe(queue.put_nowait, _line)
                                return
                            if _use_pf:
                                _prompt = self._build_no_think_prompt(messages)
                                try:  # clean context (SYCL 0.3.28 cross-request reuse broadcast)
                                    reset_context_if_needed(self._model)
                                except Exception:
                                    pass
                                _iter = self._model.create_completion(prompt=_prompt, stream=True, **params)
                                def _get_delta(c):
                                    t = c.get("choices", [{}])[0].get("text", "")
                                    return {"content": t} if t else None
                            else:
                                reset_context_if_needed(self._model)  # SYCL/Arc: clean context for the chat path
                                _iter = self._model.create_chat_completion(messages=messages, stream=True, **params)
                                def _get_delta(c):
                                    return c.get("choices", [{}])[0].get("delta") or None
                            for chunk in _iter:
                                # Check for timeout between tokens
                                current_time = time.time()
                                if current_time - last_token_time > token_timeout:
                                    logger.error(f"Streaming timeout: no token in {token_timeout}s")
                                    loop.call_soon_threadsafe(
                                        queue.put_nowait,
                                        f"data: {json.dumps({'error': {'message': f'Generation timed out after {token_timeout}s', 'type': 'timeout_error'}})}\n\n"
                                    )
                                    return
                                last_token_time = current_time

                                # Forward content and/or tool_calls deltas (function calling).
                                delta = _get_delta(chunk)
                                if delta and (delta.get("content") or delta.get("tool_calls")):
                                    sse_chunk = {
                                        "id": completion_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": model_name,
                                        "choices": [{
                                            "index": 0,
                                            "delta": delta,
                                            "finish_reason": None
                                        }]
                                    }
                                    loop.call_soon_threadsafe(
                                        queue.put_nowait,
                                        f"data: {json.dumps(sse_chunk)}\n\n"
                                    )

                                # Check for finish
                                if "choices" in chunk and len(chunk["choices"]) > 0:
                                    finish_reason = chunk["choices"][0].get("finish_reason")
                                    if finish_reason:
                                        # Emit a terminal chunk carrying finish_reason so clients
                                        # can close a tool call (e.g. 'tool_calls'). Gated behind
                                        # the opt-in flag to leave production streaming unchanged.
                                        if self.function_calling:
                                            loop.call_soon_threadsafe(
                                                queue.put_nowait,
                                                f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model_name, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish_reason}]})}\n\n"
                                            )
                                        break

                            loop.call_soon_threadsafe(queue.put_nowait, "data: [DONE]\n\n")
                        except Exception as e:
                            logger.error(f"Streaming error: {e}")
                            error_chunk = {
                                "error": {
                                    "message": str(e),
                                    "type": "inference_error"
                                }
                            }
                            loop.call_soon_threadsafe(
                                queue.put_nowait,
                                f"data: {json.dumps(error_chunk)}\n\n"
                            )
                        finally:
                            # Update last used time for idle timeout
                            global _last_used
                            _last_used = time.time()
                            # Don't unload here - let the outer finally handle it
                            loop.call_soon_threadsafe(queue.put_nowait, None)

                # Start streaming in background thread
                _executor.submit(run_streaming)

                # Yield from queue as chunks arrive. If generation goes quiet (a slow model
                # generating before the synthesized tool-call chunk), emit SSE keepalive
                # comments so the client's stream-idle timeout doesn't fire and abort.
                while True:
                    try:
                        chunk = await asyncio.wait_for(queue.get(), timeout=10.0)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if chunk is None:
                        break
                    yield chunk
        except (TimeoutError, Exception) as e:
            logger.error(f"[{request_id}] GPU lock error: {e}")
            error_chunk = {"error": {"message": str(e), "type": "gpu_lock_error"}}
            yield f"data: {json.dumps(error_chunk)}\n\n"
        finally:
            with _request_counter_lock:
                _pending_requests -= 1
                pending_after = _pending_requests

            # Warm-keep: when an idle timeout is configured, leave the model loaded between
            # requests (the idle-check thread unloads after real inactivity). This avoids a
            # full reload on every agent step - essential for slow/large models in a loop.
            # With idle timeout 0, keep the old behavior (unload immediately to free VRAM).
            if pending_after == 0 and self._idle_timeout == 0:
                self.unload_model()
            else:
                logger.info(f"[{request_id}] Keeping model loaded (pending={pending_after}, idle_timeout={self._idle_timeout})")

    def stream_chat_content(
        self,
        messages: List[Dict[str, Any]],
        **kwargs
    ):
        """
        Direct content streaming generator (no SSE formatting).
        For internal use by web UI - more efficient than parsing SSE.
        """
        self._ensure_model_loaded()
        params = self._get_sampling_params(**kwargs)
        # Embed system message into first user message for Mistral (chat_handler ignores system role).
        messages = self._embed_system_for_mistral(messages)
        token_timeout = self.token_timeout
        last_token_time = time.time()

        _mlower = _os.path.basename(self.model_path).lower()
        _use_pf = self.disable_thinking and "qwen3" in _mlower and "coder" not in _mlower
        with _get_inference_semaphore(self.max_concurrent):
            try:
                if _use_pf:
                    _prompt = self._build_no_think_prompt(messages)
                    try:  # clean context (SYCL 0.3.28 cross-request reuse broadcast)
                        reset_context_if_needed(self._model)
                    except Exception:
                        pass
                    _iter = self._model.create_completion(prompt=_prompt, stream=True, **params)
                    def _tok(c): return c.get("choices", [{}])[0].get("text", "")
                else:
                    reset_context_if_needed(self._model)  # SYCL/Arc: clean context for the chat path
                    _iter = self._model.create_chat_completion(messages=messages, stream=True, **params)
                    def _tok(c): return c.get("choices", [{}])[0].get("delta", {}).get("content", "")
                for chunk in _iter:
                    # Check for timeout between tokens
                    current_time = time.time()
                    if current_time - last_token_time > token_timeout:
                        logger.error(f"Streaming timeout: no token in {token_timeout}s")
                        yield "\n\n[Generation timed out]"
                        return
                    last_token_time = current_time

                    content = _tok(chunk)
                    if content:
                        yield content

                    finish_reason = chunk.get("choices", [{}])[0].get("finish_reason")
                    if finish_reason:
                        break
            except Exception as e:
                logger.error(f"Stream content error: {e}")
                yield f"Error: {e}"
            finally:
                # Update last used time for idle timeout
                global _last_used
                _last_used = time.time()

    async def list_models(self) -> List[Dict[str, Any]]:
        """List available models (returns the loaded model)"""
        models = []

        # Check model directory for .gguf files
        model_dir = _os.path.dirname(self.model_path)
        if _os.path.isdir(model_dir):
            for filename in _os.listdir(model_dir):
                if filename.endswith(".gguf"):
                    models.append({
                        "name": filename,
                        "model": filename,
                        "size": _os.path.getsize(_os.path.join(model_dir, filename)),
                    })

        return models

    def reload_model(self):
        """Force reload the model (useful after settings change)"""
        if self._model is not None:
            logger.info("Force reloading model...")
            _close_llama_safe(self._model)
            self._model = None
            self._model_path = None
        self._load_settings()
        self._ensure_model_loaded()

    def _idle_unload_if_free(self):
        """Idle-monitor unload: atomically re-check the in-flight counter and free the model while
        holding _request_counter_lock, so a request can't slip in and use a model we're freeing.
        Safe re: deadlock — unload_model() takes no lock, and no _request_counter_lock holder calls
        this (the inc/dec blocks are self-contained)."""
        with _request_counter_lock:
            if _pending_requests > 0:
                return
            self.unload_model()

    def unload_model(self):
        """Unload the model from memory"""
        if self._model is not None:
            logger.info("Unloading model from memory")
            _close_llama_safe(self._model)
            self._model = None
            self._model_path = None
            
            # Reset VRAM mode if unloaded outside of VRAM manager (e.g., idle timeout)
            try:
                from app.services.vram_manager import reset_vram_mode
                reset_vram_mode()
            except Exception:
                pass  # Don't fail if VRAM manager not available

    def get_model_info(self) -> Dict[str, Any]:
        """Get information about the loaded model"""
        return {
            "loaded": self._model is not None,
            "model_path": self._model_path,
            "n_ctx": self.num_ctx,
            "n_gpu_layers": self.n_gpu_layers,
            "n_threads": self.n_threads,
        }


def get_llama_service(db: Session) -> LlamaService:
    """Get or create the global LlamaService instance"""
    global _llama_instance

    if _llama_instance is None:
        _llama_instance = LlamaService(db)
    else:
        # Refresh settings from DB
        _llama_instance.db = db
        _llama_instance._load_settings()

    return _llama_instance


def reload_llama_model(db: Session):
    """Reload the model (call after settings change)"""
    global _llama_instance
    if _llama_instance is not None:
        _llama_instance.db = db
        _llama_instance.reload_model()
