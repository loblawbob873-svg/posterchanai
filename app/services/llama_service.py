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

from app.models import Setting

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
            idle_time = time.time() - _last_used
            timeout = _llama_instance._idle_timeout
            if timeout > 0 and idle_time > timeout:
                logger.info(f"LLM idle for {idle_time:.0f}s (>{timeout}s), unloading to free VRAM")
                _llama_instance.unload_model()


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

        # Context and generation settings
        # Only update num_ctx if model not loaded yet (preserve actual loaded value)
        configured_num_ctx = int(get_setting("ollama_num_ctx", "4096"))
        logger.info(f"[LLAMA] _load_settings: configured_num_ctx={configured_num_ctx}, _model is None: {self._model is None}")
        
        # Only update num_ctx when model not loaded (avoid triggering reloads)
        if self._model is None:
            self.num_ctx = configured_num_ctx
        
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
        self.use_mmap = get_setting("llm_use_mmap", "true").lower() == "true"
        self.use_mlock = get_setting("llm_use_mlock", "true").lower() == "true"
        self.flash_attn = get_setting("llm_flash_attn", "false").lower() == "true"

        # Sampling settings
        self.temperature = float(get_setting("ollama_temperature", "0.7"))
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

    def _ensure_model_loaded(self):
        """Load model if not already loaded, path changed, or configured context size changed"""
        # Check if configured context differs from what model was loaded with
        actual_model_ctx = self._model.n_ctx() if self._model is not None else 0
        configured_changed = self._model is not None and (self._configured_num_ctx != self.num_ctx or actual_model_ctx != self.num_ctx)
        if self._model is not None and self._model_path == self.model_path and not configured_changed:
            return
        
        if configured_changed:
            logger.info(f"Configured context size changed from {self._configured_num_ctx} to {self.num_ctx}, reloading model...")

        # Unload previous model
        if self._model is not None:
            logger.info(f"Unloading previous model: {self._model_path}")
            _close_llama_safe(self._model)
            self._model = None

        logger.info(f"Loading model: {self.model_path}")
        logger.info(f"  Context size: {self.num_ctx}")
        logger.info(f"  GPU layers: {self.n_gpu_layers}")
        logger.info(f"  CPU threads: {self.n_threads}")

        # Validate model file before attempting to load
        import os
        from pathlib import Path
        
        model_path_obj = Path(self.model_path)
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

            # Use admin-configured context size
            logger.info(f"  Using context size: {self.num_ctx}")

            # Determine GPU layers - force 0 if CPU mode enabled
            gpu_layers = 0 if self.cpu_mode else self.n_gpu_layers
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
                    if self._should_use_mistral_template():
                        try:
                            from llama_cpp.llama_chat_format import get_chat_completion_handler
                            chat_handler = get_chat_completion_handler("mistral")
                            logger.info("  Using mistral chat handler for template")
                        except Exception as e:
                            logger.warning(f"  Could not load mistral chat handler: {e}")
                    
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
            self._model_path = self.model_path
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

    def _sync_chat_completion_no_unload(self, messages: List[Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        """Synchronous chat completion without unloading (caller handles unload)"""
        self._ensure_model_loaded()
        params = self._get_sampling_params(**kwargs)
        # Embed system message into first user message for Mistral (chat_handler ignores system role).
        # The handler then applies [INST]...[/INST] — we do NOT call format_messages() to avoid double-templating.
        messages = self._embed_system_for_mistral(messages)

        with _get_inference_semaphore(self.max_concurrent):
            try:
                result = self._model.create_chat_completion(
                    messages=messages,
                    **params
                )

                # Strip thinking tags from response
                content = result["choices"][0]["message"]["content"]
                content = self.strip_thinking_tags(content)
                result["choices"][0]["message"]["content"] = content

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
                    lambda: self._sync_chat_completion_no_unload(messages, **kwargs)
                )

                return result
        finally:
            with _request_counter_lock:
                _pending_requests -= 1
                pending_after = _pending_requests
            
            # Only unload if no other requests waiting
            if pending_after == 0:
                self.unload_model()
            else:
                logger.info(f"[{request_id}] Keeping model loaded for {pending_after} pending request(s)")

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
        
        # Track pending requests
        with _request_counter_lock:
            _pending_requests += 1
        
        self._ensure_model_loaded()
        params = self._get_sampling_params(**kwargs)
        # Embed system message into first user message for Mistral (chat_handler ignores system role).
        messages = self._embed_system_for_mistral(messages)
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
                def run_streaming():
                    """Run synchronous generation in thread, put SSE chunks in queue"""
                    token_timeout = self.token_timeout
                    last_token_time = time.time()

                    with _get_inference_semaphore(self.max_concurrent):
                        try:
                            for chunk in self._model.create_chat_completion(
                                messages=messages,
                                stream=True,
                                **params
                            ):
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

                                content = ""
                                if "choices" in chunk and len(chunk["choices"]) > 0:
                                    delta = chunk["choices"][0].get("delta", {})
                                    content = delta.get("content", "")

                                if content:
                                    sse_chunk = {
                                        "id": completion_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": model_name,
                                        "choices": [{
                                            "index": 0,
                                            "delta": {"content": content},
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

                # Yield from queue as chunks arrive
                while True:
                    chunk = await queue.get()
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
            
            # Only unload if no other requests waiting
            if pending_after == 0:
                self.unload_model()
            else:
                logger.info(f"[{request_id}] Keeping model loaded for {pending_after} pending request(s)")

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

        with _get_inference_semaphore(self.max_concurrent):
            try:
                for chunk in self._model.create_chat_completion(
                    messages=messages,
                    stream=True,
                    **params
                ):
                    # Check for timeout between tokens
                    current_time = time.time()
                    if current_time - last_token_time > token_timeout:
                        logger.error(f"Streaming timeout: no token in {token_timeout}s")
                        yield "\n\n[Generation timed out]"
                        return
                    last_token_time = current_time

                    if "choices" in chunk and len(chunk["choices"]) > 0:
                        delta = chunk["choices"][0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content

                        finish_reason = chunk["choices"][0].get("finish_reason")
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
