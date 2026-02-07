"""
IPEX-LLM Service for Intel Arc GPU acceleration.
Uses Intel's optimized LLM inference for maximum performance on Arc GPUs.
"""
import asyncio
import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncGenerator, Optional, List, Dict, Any
from sqlalchemy.orm import Session

from app.models import Setting

# Configure logging
logger = logging.getLogger("ipex_service")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [IPEX] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(handler)


# Track if we've already checked/setup the oneAPI environment
_oneapi_checked = False
_oneapi_available = False


def _check_and_setup_oneapi():
    """
    Check if Intel oneAPI environment is available and try to auto-configure if not.
    This helps when the service is started without sourcing setvars.sh.
    """
    global _oneapi_checked, _oneapi_available

    if _oneapi_checked:
        return _oneapi_available

    _oneapi_checked = True

    # Check if oneAPI libraries are already in LD_LIBRARY_PATH
    ld_path = os.environ.get("LD_LIBRARY_PATH", "")
    if "/opt/intel/oneapi" in ld_path or "/intel/oneapi" in ld_path:
        logger.info("Intel oneAPI environment detected in LD_LIBRARY_PATH")
        _oneapi_available = True
        return True

    # Try to find and configure oneAPI automatically
    oneapi_roots = [
        "/opt/intel/oneapi/2025.0",
        "/opt/intel/oneapi/2024.2",
        "/opt/intel/oneapi",
        os.path.expanduser("~/intel/oneapi"),
    ]

    oneapi_root = None
    for root in oneapi_roots:
        if os.path.isdir(root) and os.path.isdir(os.path.join(root, "lib")):
            oneapi_root = root
            break

    if oneapi_root is None:
        logger.warning("=" * 60)
        logger.warning("Intel oneAPI NOT FOUND!")
        logger.warning("IPEX-LLM requires Intel oneAPI for GPU acceleration.")
        logger.warning("Install oneAPI or start the service with: ./run-ipex.sh")
        logger.warning("=" * 60)
        _oneapi_available = False
        return False

    # Auto-configure the environment
    logger.warning("=" * 60)
    logger.warning("Intel oneAPI found but environment not configured!")
    logger.warning(f"Auto-configuring from: {oneapi_root}")
    logger.warning("For best results, start with: ./run-ipex.sh")
    logger.warning("=" * 60)

    # Set LD_LIBRARY_PATH
    lib_paths = [
        os.path.join(oneapi_root, "lib"),
        os.path.join(oneapi_root, "compiler", "lib"),
        os.path.join(oneapi_root, "mkl", "lib"),
    ]

    existing_ld_path = os.environ.get("LD_LIBRARY_PATH", "")
    new_paths = [p for p in lib_paths if os.path.isdir(p)]
    if new_paths:
        os.environ["LD_LIBRARY_PATH"] = ":".join(new_paths + [existing_ld_path] if existing_ld_path else new_paths)
        logger.info(f"Updated LD_LIBRARY_PATH with oneAPI libraries")

    # Set other Intel environment variables
    os.environ["ONEAPI_ROOT"] = oneapi_root

    # OCL_ICD_FILENAMES for OpenCL
    ocl_path = os.path.join(oneapi_root, "lib", "libintelocl.so")
    if os.path.exists(ocl_path):
        os.environ["OCL_ICD_FILENAMES"] = ocl_path

    # IPEX-LLM optimizations
    os.environ.setdefault("ENABLE_SDP_FUSION", "1")
    os.environ.setdefault("SYCL_CACHE_PERSISTENT", "1")
    os.environ.setdefault("BIGDL_LLM_XMX_DISABLED", "1")
    # Disable Level Zero Sysman to avoid background GPU polling that maxes a CPU core
    os.environ.setdefault("ZES_ENABLE_SYSMAN", "0")

    _oneapi_available = True
    return True


def check_xpu_available() -> tuple[bool, str]:
    """
    Check if Intel XPU (GPU) is available for acceleration.
    Returns (is_available, message).
    """
    # First ensure oneAPI environment is set up
    _check_and_setup_oneapi()

    try:
        import torch
        if hasattr(torch, 'xpu') and torch.xpu.is_available():
            device_count = torch.xpu.device_count()
            return True, f"XPU available with {device_count} device(s)"
    except Exception as e:
        pass

    # Check if llama-cpp-python is available for GGUF models
    try:
        from llama_cpp import Llama
        return True, "llama-cpp-python backend available"
    except ImportError as e:
        error_msg = str(e)
        if "libsvml.so" in error_msg or "cannot open shared object" in error_msg:
            return False, f"Intel oneAPI libraries not loaded. Start with ./run-ipex.sh or source oneAPI environment first."
        elif "intel_extension_for_pytorch" in error_msg:
            return False, f"intel_extension_for_pytorch not installed. Run setup-ipex.sh or use venv-ipex."
        return False, f"IPEX-LLM not available: {e}"
    except Exception as e:
        return False, f"Error checking IPEX-LLM: {e}"


# Global model instance (singleton)
_ipex_instance: Optional["IPEXService"] = None
# Use more workers to match max concurrency
_executor = ThreadPoolExecutor(max_workers=8)
_model_load_lock = threading.Lock()  # Separate lock for model loading

# Concurrency control - semaphore allows N concurrent inferences
_inference_semaphore: Optional[threading.Semaphore] = None
_current_max_concurrent = 1
_semaphore_lock = threading.Lock()

# Request tracking for debugging
_request_counter = 0
_request_counter_lock = threading.Lock()
_pending_requests = 0
_current_request: Optional[str] = None

# Idle timeout tracking
_last_used: float = 0
_idle_check_thread: Optional[threading.Thread] = None
_idle_check_stop = threading.Event()


def _start_idle_check():
    """Start the background idle check thread"""
    global _idle_check_thread
    if _idle_check_thread is not None and _idle_check_thread.is_alive():
        return
    _idle_check_stop.clear()
    _idle_check_thread = threading.Thread(target=_idle_check_loop, daemon=True)
    _idle_check_thread.start()
    logger.info("IPEX idle check thread started")


def _idle_check_loop():
    """Background loop to check for idle timeout and unload model"""
    global _ipex_instance, _last_used
    while not _idle_check_stop.wait(30):  # Check every 30 seconds
        if _ipex_instance is not None and _ipex_instance._model is not None:
            idle_time = time.time() - _last_used
            timeout = _ipex_instance._idle_timeout
            if timeout > 0 and idle_time > timeout:
                logger.info(f"IPEX model idle for {idle_time:.0f}s (>{timeout}s), unloading to free VRAM")
                _ipex_instance.unload_model()


def _get_inference_semaphore(max_concurrent: int = 1) -> threading.Semaphore:
    """Get or create inference semaphore with specified concurrency"""
    global _inference_semaphore, _current_max_concurrent
    with _semaphore_lock:
        if _inference_semaphore is None or _current_max_concurrent != max_concurrent:
            _inference_semaphore = threading.Semaphore(max_concurrent)
            _current_max_concurrent = max_concurrent
            logger.info(f"Inference concurrency set to {max_concurrent}")
        return _inference_semaphore


class IPEXService:
    """
    IPEX-LLM inference service using Intel's optimized backend.
    Keeps model loaded in memory for fast inference.
    """

    def __init__(self, db: Session):
        self.db = db
        self._model = None
        self._tokenizer = None
        self._model_path: Optional[str] = None
        self._is_gguf: bool = False
        self._load_settings()
        _start_idle_check()

    def _load_settings(self):
        """Load settings from database"""
        from app.database import safe_query_settings
        settings = safe_query_settings(self.db)
        
        # Helper to get setting with fallback for empty strings
        def get_setting(key: str, default: str) -> str:
            val = settings.get(key, default)
            return val if val else default

        # Model settings - normalize path to prevent spurious reloads
        self.model_path = os.path.normpath(get_setting("llm_model_path", "/home/verita84/models/model.gguf").strip())
        self.default_model = get_setting("ollama_model", "ipex")

        # Context and generation settings
        self.num_ctx = int(get_setting("ollama_num_ctx", "4096"))
        self.num_predict = int(get_setting("ollama_num_predict", "2048"))
        self.n_batch = int(get_setting("llm_n_batch", "2048"))  # Batch size for prompt processing
        self.n_gpu_layers = int(get_setting("llm_gpu_layers", "-1"))  # -1 = all layers on GPU
        self.max_concurrent = int(get_setting("llm_max_concurrent", "1"))  # Max concurrent inferences

        # CPU settings - auto-detect threads if set to 0
        n_threads_setting = int(get_setting("llm_n_threads", "0"))
        if n_threads_setting <= 0:
            cpu_count = os.cpu_count() or 4
            # Use physical cores (cpu_count // 2) for better performance
            # SMT/hyperthreading can cause contention during inference
            self.n_threads = max(1, cpu_count // 2)
            logger.info(f"Auto-detected CPU threads: {self.n_threads} (physical cores from {cpu_count} logical)")
        else:
            self.n_threads = n_threads_setting

        # CPU optimization settings
        self.cpu_mode = get_setting("llm_cpu_mode", "false").lower() == "true"
        self.use_mmap = get_setting("llm_use_mmap", "true").lower() == "true"
        self.use_mlock = get_setting("llm_use_mlock", "true").lower() == "true"

        # Sampling settings
        self.temperature = float(get_setting("ollama_temperature", "0.7"))
        self.top_p = float(get_setting("ollama_top_p", "0.9"))
        self.top_k = int(get_setting("ollama_top_k", "40"))
        self.repeat_penalty = float(get_setting("ollama_repeat_penalty", "1.1"))

        # System prompt
        self.system_prompt = get_setting("ollama_system_prompt", "You are a helpful, friendly AI assistant.")

        # Idle timeout for automatic unloading (0 = disabled)
        self._idle_timeout = int(get_setting("llm_idle_timeout", "0"))

        # Token timeout for streaming (max seconds between tokens)
        self.token_timeout = int(get_setting("llm_token_timeout", "600"))

        # Inference timeout (seconds) - prevents hung requests
        self.inference_timeout = int(get_setting("ollama_timeout", "300000")) // 1000  # Convert ms to seconds

        # Thinking mode control
        self.disable_thinking = get_setting("llm_disable_thinking", "false").lower() == "true"

    def _ensure_model_loaded(self):
        """Load model if not already loaded or if path changed"""
        # Quick check without lock
        if self._model is not None and self._model_path == self.model_path:
            return

        # Use lock for actual loading to prevent race conditions
        with _model_load_lock:
            # Double-check after acquiring lock
            if self._model is not None and self._model_path == self.model_path:
                return

            # Check and setup oneAPI environment before loading
            _check_and_setup_oneapi()

            # Unload previous model
            if self._model is not None:
                logger.info(f"Unloading previous model: {self._model_path}")
                try:
                    del self._model
                except AttributeError as e:
                    # Handle incomplete model objects (e.g., sampler not initialized)
                    logger.warning(f"Error during model cleanup (ignored): {e}")
                try:
                    del self._tokenizer
                except (AttributeError, TypeError) as e:
                    logger.warning(f"Error during tokenizer cleanup (ignored): {e}")
                self._model = None
                self._tokenizer = None

            # Determine GPU layers - force 0 if CPU mode enabled
            gpu_layers = 0 if self.cpu_mode else self.n_gpu_layers

            logger.info(f"Loading model with IPEX-LLM: {self.model_path}")
            logger.info(f"  ctx: {self.num_ctx}, batch: {self.n_batch}, gpu_layers: {gpu_layers}, threads: {self.n_threads}")
            logger.info(f"  CPU mode: {self.cpu_mode}, mmap: {self.use_mmap}, mlock: {self.use_mlock}")

            try:
                # Check if GGUF model - use llama-cpp-python
                # Note: llama-cpp-python needs to be compiled with SYCL for Intel Arc GPU
                # Install with: CMAKE_ARGS="-DGGML_SYCL=ON" pip install llama-cpp-python
                if self.model_path.endswith('.gguf'):
                    try:
                        from llama_cpp import Llama
                        import llama_cpp
                        gpu_offload = llama_cpp.llama_supports_gpu_offload()
                        logger.info(f"Using llama-cpp-python for GGUF model (GPU offload support: {gpu_offload})")
                        if gpu_layers != 0 and not self.cpu_mode:
                            logger.info(f"GPU layers requested: {gpu_layers}")
                            if not gpu_offload:
                                logger.warning("GPU layers requested but llama-cpp-python has no GPU support!")
                                logger.warning("Rebuild with: CMAKE_ARGS='-DGGML_SYCL=ON' pip install llama-cpp-python --force-reinstall")
                    except ImportError as e:
                        raise ImportError(
                            f"llama-cpp-python not available: {e}. "
                            f"Install with: pip install llama-cpp-python"
                        )

                    self._model = Llama(
                        model_path=self.model_path,
                        n_ctx=self.num_ctx,
                        n_gpu_layers=gpu_layers if not self.cpu_mode else 0,
                        n_batch=self.n_batch,
                        n_threads=self.n_threads,
                        n_threads_batch=self.n_threads,
                        use_mmap=self.use_mmap,
                        use_mlock=self.use_mlock,
                        verbose=False,
                    )
                    self._tokenizer = None  # llama.cpp handles tokenization
                    self._is_gguf = True
                    logger.info("GGUF model loaded successfully")
                    # Intel SYCL (ggml-sycl) can crash with "UR error" when multiple inferences run
                    # concurrently. Recommend max_concurrent=1 to avoid process exit.
                    if gpu_layers != 0 and self.max_concurrent > 1:
                        logger.warning(
                            "[IPEX] GGUF with GPU (SYCL): llm_max_concurrent=%d. "
                            "Intel SYCL often crashes (UR error) with concurrent requests. "
                            "Set Admin → Settings → llm_max_concurrent to 1 to avoid crashes.",
                            self.max_concurrent,
                        )
                else:
                    # Load HuggingFace model with IPEX-LLM
                    import torch
                    from ipex_llm.transformers import AutoModelForCausalLM
                    from transformers import AutoTokenizer

                    self._model = AutoModelForCausalLM.from_pretrained(
                        self.model_path,
                        load_in_4bit=True,
                        trust_remote_code=True,
                        optimize_model=True,
                        use_cache=True,
                    )
                    self._model = self._model.to('xpu')
                    self._tokenizer = AutoTokenizer.from_pretrained(
                        self.model_path,
                        trust_remote_code=True
                    )
                    self._is_gguf = False
                    logger.info("HuggingFace model loaded with IPEX-LLM")

                self._model_path = self.model_path

            except Exception as e:
                error_msg = str(e)
                logger.error(f"Original error loading model: {e}")
                # Provide helpful error messages for common issues
                if "memory" in error_msg.lower() or "dnnl" in error_msg.lower() or "oneDNN" in error_msg:
                    logger.error(f"Memory allocation failed loading model: {e}")
                    logger.error("This usually means insufficient GPU/system memory. Try:")
                    logger.error("  - Reducing context size (ollama_num_ctx)")
                    logger.error("  - Reducing batch size (llm_n_batch)")
                    logger.error("  - Using a smaller model")
                    logger.error("  - Closing other applications")
                    raise RuntimeError(f"Memory allocation failed: {e}. Try reducing context/batch size or using a smaller model.")
                elif ("No such file" in error_msg or "not found" in error_msg.lower()) and not os.path.exists(self.model_path):
                    logger.error(f"Model file not found: {self.model_path}")
                    raise FileNotFoundError(f"Model file not found: {self.model_path}")
                else:
                    logger.error(f"Failed to load model: {e}")
                    raise

    def strip_thinking_tags(self, response: str) -> str:
        """Strip thinking tags from AI response"""
        from app.services.text_utils import strip_thinking_tags
        return strip_thinking_tags(response)

    def _generate_response(self, messages: List[Dict[str, Any]], **kwargs) -> str:
        """Generate a response synchronously with retry for transient errors"""
        logger.info("[DEBUG] _generate_response called at 10:19")
        try:
            self._ensure_model_loaded()
        except Exception as e:
            logger.error(f"[DEBUG] Model loading failed: {e}")
            raise

        if self._is_gguf:
            # Use llama.cpp API for GGUF models with retry for transient errors
            max_retries = 2
            last_error = None

            # Build stop sequences
            stop = list(kwargs.get("stop", []) or [])

            # Handle thinking mode for Qwen3-style models
            messages_to_use = messages
            if self.disable_thinking:
                # For Qwen3-abliterated and similar thinking models:
                # Add strong system instruction + modify user message to reinforce
                model_name = self.model_path.lower()
                if "qwen3" in model_name or "deepseek-r1" in model_name or "reasoning" in model_name:
                    logger.info(f"Detected thinking model: {self.model_path}")
                    logger.info("Adding instructions to suppress thinking mode")

                    messages_to_use = messages.copy()

                    # Add/modify system message
                    system_instruction = (
                        "You must respond directly without thinking tags. "
                        "Do NOT use <think> or show reasoning. "
                        "Give ONLY the final answer immediately."
                    )

                    if messages_to_use and messages_to_use[0].get("role") == "system":
                        messages_to_use[0]["content"] = system_instruction + "\n\n" + messages_to_use[0]["content"]
                    else:
                        messages_to_use.insert(0, {"role": "system", "content": system_instruction})

                    # Also reinforce in the last user message
                    for i in range(len(messages_to_use) - 1, -1, -1):
                        if messages_to_use[i].get("role") == "user":
                            messages_to_use[i]["content"] += "\n(Respond directly without <think> tags)"
                            break
                else:
                    logger.info(f"Thinking mode disabled (disable_thinking={self.disable_thinking})")

            logger.info(f"Generating response with max_tokens={kwargs.get('max_tokens', self.num_predict)}, stop={stop}")

            for attempt in range(max_retries + 1):
                try:
                    response = self._model.create_chat_completion(
                        messages=messages_to_use,
                        max_tokens=kwargs.get("max_tokens", self.num_predict),
                        temperature=kwargs.get("temperature", self.temperature),
                        top_p=kwargs.get("top_p", self.top_p),
                        top_k=kwargs.get("top_k", self.top_k),
                        repeat_penalty=kwargs.get("repeat_penalty", self.repeat_penalty),
                        stop=stop if stop else None,
                    )
                    content = response["choices"][0]["message"]["content"]
                    return self.strip_thinking_tags(content)
                except IndexError as e:
                    # Handle transient "index out of bounds" errors in llama-cpp-python
                    last_error = e
                    if attempt < max_retries:
                        logger.warning(f"Inference IndexError (attempt {attempt + 1}/{max_retries + 1}): {e}, retrying...")
                        continue
                    raise

            raise last_error
        else:
            # Use HuggingFace API
            import torch
            prompt = self._build_prompt(messages)
            inputs = self._tokenizer(prompt, return_tensors="pt").to('xpu')

            with torch.inference_mode():
                outputs = self._model.generate(
                    **inputs,
                    max_new_tokens=kwargs.get("max_tokens", self.num_predict),
                    temperature=kwargs.get("temperature", self.temperature),
                    top_p=kwargs.get("top_p", self.top_p),
                    top_k=kwargs.get("top_k", self.top_k),
                    repetition_penalty=kwargs.get("repeat_penalty", self.repeat_penalty),
                    do_sample=True,
                    pad_token_id=self._tokenizer.eos_token_id,
                )

            response = self._tokenizer.decode(
                outputs[0][inputs['input_ids'].shape[1]:],
                skip_special_tokens=True
            )
            return self.strip_thinking_tags(response)

    def _build_prompt(self, messages: List[Dict[str, Any]]) -> str:
        """Build prompt string from messages using chat template (HuggingFace only)"""
        if self._tokenizer and hasattr(self._tokenizer, 'apply_chat_template'):
            return self._tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True
            )
        else:
            # Fallback: simple format
            prompt = ""
            for msg in messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role == "system":
                    prompt += f"System: {content}\n\n"
                elif role == "user":
                    prompt += f"User: {content}\n\n"
                elif role == "assistant":
                    prompt += f"Assistant: {content}\n\n"
            prompt += "Assistant: "
            return prompt

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        stream: bool = False,
        **kwargs
    ) -> Dict[str, Any]:
        """Non-streaming chat completion with timeout."""
        global _request_counter, _pending_requests, _current_request
        loop = asyncio.get_running_loop()

        # Generate request ID and track
        with _request_counter_lock:
            _request_counter += 1
            request_id = _request_counter
            _pending_requests += 1

        # Get preview of user message for logging
        user_msg = next(((m.get("content") or "")[:50] for m in reversed(messages) if m.get("role") == "user"), "?")
        logger.info(f"[REQ-{request_id}] Queued: \"{user_msg}...\" (pending: {_pending_requests})")
        start_time = time.time()

        # Acquire shared GPU lock to prevent LLM and image from running simultaneously
        from app.services.locks import GPUResourceLock
        try:
            async with GPUResourceLock("LLM", f"REQ-{request_id}"):
                def run_with_lock():
                    global _current_request
                    with _get_inference_semaphore(self.max_concurrent):
                        _current_request = f"REQ-{request_id}"
                        logger.info(f"[REQ-{request_id}] Processing started")
                        try:
                            return self._generate_response(messages, **kwargs)
                        finally:
                            _current_request = None

                try:
                    # Use wait_for to add timeout
                    response = await asyncio.wait_for(
                        loop.run_in_executor(_executor, run_with_lock),
                        timeout=self.inference_timeout
                    )
                    elapsed = time.time() - start_time
                    logger.info(f"[REQ-{request_id}] Completed in {elapsed:.1f}s (pending: {_pending_requests - 1})")
                    # Update last used time for idle timeout
                    global _last_used
                    _last_used = time.time()
                except asyncio.TimeoutError:
                    elapsed = time.time() - start_time
                    logger.error(f"[REQ-{request_id}] Timed out after {elapsed:.1f}s")
                    with _request_counter_lock:
                        _pending_requests -= 1
                    return {
                        "error": {"message": f"Inference timed out after {self.inference_timeout} seconds", "type": "timeout_error"}
                    }
                except Exception as e:
                    elapsed = time.time() - start_time
                    logger.error(f"[REQ-{request_id}] Error after {elapsed:.1f}s: {e}")
                    with _request_counter_lock:
                        _pending_requests -= 1
                    return {
                        "error": {"message": str(e), "type": "inference_error"}
                    }
            
            # Success - response is available here
            with _request_counter_lock:
                _pending_requests -= 1

            return {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model or self.default_model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": response},
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            }
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"[REQ-{request_id}] GPU lock error after {elapsed:.1f}s: {e}")
            with _request_counter_lock:
                _pending_requests -= 1
            return {
                "error": {"message": str(e), "type": "gpu_lock_error"}
            }

    async def chat_completion_stream(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """Streaming chat completion using async queue."""
        global _request_counter, _pending_requests
        self._ensure_model_loaded()

        # Generate request ID and track
        with _request_counter_lock:
            _request_counter += 1
            request_id = _request_counter
            _pending_requests += 1

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        model_name = model or self.default_model

        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        # Build stop sequences
        stop = list(kwargs.get("stop", []) or [])

        # Handle thinking mode for Qwen3-style models (same as non-streaming)
        messages_to_use = messages
        if self.disable_thinking:
            model_name_lower = self.model_path.lower()
            if "qwen3" in model_name_lower or "deepseek-r1" in model_name_lower or "reasoning" in model_name_lower:
                messages_to_use = messages.copy()

                # Add/modify system message
                system_instruction = (
                    "You must respond directly without thinking tags. "
                    "Do NOT use <think> or show reasoning. "
                    "Give ONLY the final answer immediately."
                )

                if messages_to_use and messages_to_use[0].get("role") == "system":
                    messages_to_use[0]["content"] = system_instruction + "\n\n" + messages_to_use[0]["content"]
                else:
                    messages_to_use.insert(0, {"role": "system", "content": system_instruction})

                # Reinforce in last user message
                for i in range(len(messages_to_use) - 1, -1, -1):
                    if messages_to_use[i].get("role") == "user":
                        messages_to_use[i]["content"] += "\n(Respond directly without <think> tags)"
                        break

        # Acquire shared GPU lock to prevent LLM and image from running simultaneously
        from app.services.locks import GPUResourceLock
        async with GPUResourceLock("LLM", f"STREAM-{request_id}"):
            def run_streaming():
                """Run generation in thread, put tokens in queue"""
                with _get_inference_semaphore(self.max_concurrent):
                    try:
                        if self._is_gguf:
                            # Use llama.cpp streaming for GGUF
                            for chunk in self._model.create_chat_completion(
                                messages=messages_to_use,
                                max_tokens=kwargs.get("max_tokens", self.num_predict),
                                temperature=kwargs.get("temperature", self.temperature),
                                top_p=kwargs.get("top_p", self.top_p),
                                top_k=kwargs.get("top_k", self.top_k),
                                repeat_penalty=kwargs.get("repeat_penalty", self.repeat_penalty),
                                stop=stop if stop else None,
                                stream=True,
                            ):
                                delta = chunk.get("choices", [{}])[0].get("delta", {})
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
                        else:
                            # Use HuggingFace streaming
                            import torch
                            from transformers import TextIteratorStreamer

                            prompt = self._build_prompt(messages)
                            inputs = self._tokenizer(prompt, return_tensors="pt").to('xpu')

                            streamer = TextIteratorStreamer(
                                self._tokenizer,
                                skip_prompt=True,
                                skip_special_tokens=True
                            )

                            generation_kwargs = {
                                **inputs,
                                "max_new_tokens": kwargs.get("max_tokens", self.num_predict),
                                "temperature": kwargs.get("temperature", self.temperature),
                                "top_p": kwargs.get("top_p", self.top_p),
                                "top_k": kwargs.get("top_k", self.top_k),
                                "repetition_penalty": kwargs.get("repeat_penalty", self.repeat_penalty),
                                "do_sample": True,
                                "pad_token_id": self._tokenizer.eos_token_id,
                                "streamer": streamer,
                            }

                            gen_thread = threading.Thread(
                                target=lambda: self._model.generate(**generation_kwargs)
                            )
                            gen_thread.start()

                            for token in streamer:
                                if token:
                                    sse_chunk = {
                                        "id": completion_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": model_name,
                                        "choices": [{
                                            "index": 0,
                                            "delta": {"content": token},
                                            "finish_reason": None
                                        }]
                                    }
                                    loop.call_soon_threadsafe(
                                        queue.put_nowait,
                                        f"data: {json.dumps(sse_chunk)}\n\n"
                                    )
                            gen_thread.join()

                        loop.call_soon_threadsafe(queue.put_nowait, "data: [DONE]\n\n")

                    except Exception as e:
                        logger.error(f"Streaming error: {e}")
                        error_chunk = {"error": {"message": str(e), "type": "inference_error"}}
                        loop.call_soon_threadsafe(
                            queue.put_nowait,
                            f"data: {json.dumps(error_chunk)}\n\n"
                        )
                    finally:
                        # Update last used time for idle timeout
                        global _last_used
                        _last_used = time.time()
                        loop.call_soon_threadsafe(queue.put_nowait, None)

            _executor.submit(run_streaming)

            try:
                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        break
                    yield chunk
            finally:
                # Update request counter when stream completes
                with _request_counter_lock:
                    _pending_requests -= 1

    def stream_chat_content(
        self,
        messages: List[Dict[str, Any]],
        **kwargs
    ):
        """Direct content streaming generator for web UI with timeout protection."""
        global _request_counter, _pending_requests, _current_request, _request_counter_lock

        # Generate request ID and track
        with _request_counter_lock:
            _request_counter += 1
            request_id = _request_counter
            _pending_requests += 1

        # Get preview of user message for logging
        user_msg = next(((m.get("content") or "")[:50] for m in reversed(messages) if m.get("role") == "user"), "?")
        logger.info(f"[STREAM-{request_id}] Queued: \"{user_msg}...\" (pending: {_pending_requests})")
        start_time = time.time()

        self._ensure_model_loaded()

        # Per-token timeout (seconds) - if no token in this time, abort
        token_timeout = self.token_timeout

        with _get_inference_semaphore(self.max_concurrent):
            _current_request = f"STREAM-{request_id}"
            logger.info(f"[STREAM-{request_id}] Processing started")

            # Handle thinking mode for Qwen3-style models (same as non-streaming)
            messages_to_use = messages
            if self.disable_thinking:
                model_name_lower = self.model_path.lower()
                if "qwen3" in model_name_lower or "deepseek-r1" in model_name_lower or "reasoning" in model_name_lower:
                    messages_to_use = messages.copy()

                    # Add/modify system message
                    system_instruction = (
                        "You must respond directly without thinking tags. "
                        "Do NOT use <think> or show reasoning. "
                        "Give ONLY the final answer immediately."
                    )

                    if messages_to_use and messages_to_use[0].get("role") == "system":
                        messages_to_use[0]["content"] = system_instruction + "\n\n" + messages_to_use[0]["content"]
                    else:
                        messages_to_use.insert(0, {"role": "system", "content": system_instruction})

                    # Reinforce in last user message
                    for i in range(len(messages_to_use) - 1, -1, -1):
                        if messages_to_use[i].get("role") == "user":
                            messages_to_use[i]["content"] += "\n(Respond directly without <think> tags)"
                            break

            try:
                if self._is_gguf:
                    # Build stop sequences
                    stop = list(kwargs.get("stop", []) or [])

                    # Use llama.cpp streaming for GGUF with error recovery
                    last_token_time = time.time()
                    try:
                        for chunk in self._model.create_chat_completion(
                            messages=messages_to_use,
                            max_tokens=kwargs.get("max_tokens", self.num_predict),
                            temperature=kwargs.get("temperature", self.temperature),
                            top_p=kwargs.get("top_p", self.top_p),
                            top_k=kwargs.get("top_k", self.top_k),
                            repeat_penalty=kwargs.get("repeat_penalty", self.repeat_penalty),
                            stop=stop if stop else None,
                            stream=True,
                        ):
                            # Check for timeout between tokens
                            current_time = time.time()
                            if current_time - last_token_time > token_timeout:
                                logger.error(f"Streaming timeout: no token in {token_timeout}s")
                                yield "\n\n[Generation timed out]"
                                return
                            last_token_time = current_time

                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield content
                    except Exception as stream_error:
                        error_msg = str(stream_error)
                        # Handle llama_decode errors gracefully
                        if "llama_decode" in error_msg or isinstance(stream_error, (RuntimeError, IndexError)):
                            logger.error(f"[STREAM-{request_id}] Inference error: {error_msg}")
                            yield f"\n\n[Generation error: {error_msg}]"
                            return
                        raise
                else:
                    # Use HuggingFace streaming
                    import torch
                    from transformers import TextIteratorStreamer

                    prompt = self._build_prompt(messages)
                    inputs = self._tokenizer(prompt, return_tensors="pt").to('xpu')

                    streamer = TextIteratorStreamer(
                        self._tokenizer,
                        skip_prompt=True,
                        skip_special_tokens=True,
                        timeout=token_timeout  # Add timeout to streamer
                    )

                    generation_kwargs = {
                        **inputs,
                        "max_new_tokens": kwargs.get("max_tokens", self.num_predict),
                        "temperature": kwargs.get("temperature", self.temperature),
                        "top_p": kwargs.get("top_p", self.top_p),
                        "top_k": kwargs.get("top_k", self.top_k),
                        "repetition_penalty": kwargs.get("repeat_penalty", self.repeat_penalty),
                        "do_sample": True,
                        "pad_token_id": self._tokenizer.eos_token_id,
                        "streamer": streamer,
                    }

                    gen_thread = threading.Thread(
                        target=lambda: self._model.generate(**generation_kwargs)
                    )
                    gen_thread.start()

                    last_token_time = time.time()
                    for token in streamer:
                        if token:
                            last_token_time = time.time()
                            yield token
                        elif time.time() - last_token_time > token_timeout:
                            logger.error(f"HuggingFace streaming timeout: no token in {token_timeout}s")
                            yield "Error: Generation timed out"
                            break

                    if gen_thread.is_alive():
                        logger.warning(f"[STREAM-{request_id}] Generation thread still running after timeout")
                    gen_thread.join(timeout=5)  # Don't wait forever for thread

                # Log completion
                elapsed = time.time() - start_time
                logger.info(f"[STREAM-{request_id}] Completed in {elapsed:.1f}s")

            except Exception as e:
                elapsed = time.time() - start_time
                logger.error(f"[STREAM-{request_id}] Error after {elapsed:.1f}s: {e}")
                yield f"Error: {e}"
            finally:
                _current_request = None
                with _request_counter_lock:
                    _pending_requests -= 1
                # Update last used time for idle timeout
                global _last_used
                _last_used = time.time()

    async def list_models(self) -> List[Dict[str, Any]]:
        """List available models"""
        models = []
        model_dir = os.path.dirname(self.model_path)

        if os.path.isdir(model_dir):
            for filename in os.listdir(model_dir):
                if filename.endswith((".gguf", ".bin", ".safetensors")):
                    models.append({
                        "name": filename,
                        "model": filename,
                        "size": os.path.getsize(os.path.join(model_dir, filename)),
                    })

        return models

    def reload_model(self):
        """Force reload the model"""
        if self._model is not None:
            logger.info("Force reloading model...")
            try:
                del self._model
            except AttributeError as e:
                # Handle incomplete model objects (e.g., sampler not initialized)
                logger.warning(f"Error during model cleanup (ignored): {e}")
            try:
                del self._tokenizer
            except (AttributeError, TypeError) as e:
                logger.warning(f"Error during tokenizer cleanup (ignored): {e}")
            self._model = None
            self._tokenizer = None
            self._model_path = None
        self._load_settings()
        self._ensure_model_loaded()

    def unload_model(self):
        """Unload the model from memory"""
        if self._model is not None:
            logger.info("Unloading model from memory")
            try:
                del self._model
            except AttributeError as e:
                # Handle incomplete model objects (e.g., sampler not initialized)
                logger.warning(f"Error during model cleanup (ignored): {e}")
            try:
                del self._tokenizer
            except (AttributeError, TypeError) as e:
                logger.warning(f"Error during tokenizer cleanup (ignored): {e}")
            self._model = None
            self._tokenizer = None
            self._model_path = None

    def get_model_info(self) -> Dict[str, Any]:
        """Get information about the loaded model"""
        return {
            "loaded": self._model is not None,
            "model_path": self._model_path,
            "n_ctx": self.num_ctx,
            "backend": "ipex-llm",
        }


def get_ipex_service(db: Session) -> IPEXService:
    """Get or create the global IPEXService instance"""
    global _ipex_instance

    if _ipex_instance is None:
        _ipex_instance = IPEXService(db)
    else:
        _ipex_instance.db = db
        _ipex_instance._load_settings()

    return _ipex_instance


def reload_ipex_model(db: Session):
    """Reload the model"""
    global _ipex_instance
    if _ipex_instance is not None:
        _ipex_instance.db = db
        _ipex_instance.reload_model()
