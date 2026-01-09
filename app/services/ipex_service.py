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
        settings = {s.key: s.value for s in self.db.query(Setting).all()}

        # Model settings - normalize path to prevent spurious reloads
        self.model_path = os.path.normpath(settings.get("llm_model_path", "/home/verita84/models/model.gguf").strip())
        self.default_model = settings.get("ollama_model", "ipex")

        # Context and generation settings
        self.num_ctx = int(settings.get("ollama_num_ctx", "4096"))
        self.num_predict = int(settings.get("ollama_num_predict", "2048"))
        self.n_batch = int(settings.get("llm_n_batch", "2048"))  # Batch size for prompt processing
        self.n_gpu_layers = int(settings.get("llm_gpu_layers", "-1"))  # -1 = all layers on GPU
        self.max_concurrent = int(settings.get("llm_max_concurrent", "1"))  # Max concurrent inferences

        # CPU settings - auto-detect threads if set to 0
        n_threads_setting = int(settings.get("llm_n_threads", "0"))
        if n_threads_setting <= 0:
            cpu_count = os.cpu_count() or 4
            # Use physical cores (cpu_count // 2) for better performance
            # SMT/hyperthreading can cause contention during inference
            self.n_threads = max(1, cpu_count // 2)
            logger.info(f"Auto-detected CPU threads: {self.n_threads} (physical cores from {cpu_count} logical)")
        else:
            self.n_threads = n_threads_setting

        # CPU optimization settings
        self.cpu_mode = settings.get("llm_cpu_mode", "false").lower() == "true"
        self.use_mmap = settings.get("llm_use_mmap", "true").lower() == "true"
        self.use_mlock = settings.get("llm_use_mlock", "true").lower() == "true"

        # Sampling settings
        self.temperature = float(settings.get("ollama_temperature", "0.7"))
        self.top_p = float(settings.get("ollama_top_p", "0.9"))
        self.top_k = int(settings.get("ollama_top_k", "40"))
        self.repeat_penalty = float(settings.get("ollama_repeat_penalty", "1.1"))

        # System prompt
        self.system_prompt = settings.get("ollama_system_prompt", "You are a helpful, friendly AI assistant.")

        # Idle timeout for automatic unloading (0 = disabled)
        self._idle_timeout = int(settings.get("llm_idle_timeout", "0"))

        # Inference timeout (seconds) - prevents hung requests
        self.inference_timeout = int(settings.get("ollama_timeout", "120000")) // 1000  # Convert ms to seconds

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
                # Check if GGUF model - use llama.cpp backend
                if self.model_path.endswith('.gguf'):
                    # Try ipex_llm.llama_cpp first, fall back to regular llama_cpp
                    try:
                        from ipex_llm.llama_cpp import Llama
                        logger.info("Using IPEX-LLM llama.cpp backend")
                    except ImportError as e:
                        logger.warning(f"IPEX-LLM llama.cpp not available ({e}), using standard llama-cpp-python")
                        from llama_cpp import Llama

                    self._model = Llama(
                        model_path=self.model_path,
                        n_ctx=self.num_ctx,
                        n_gpu_layers=gpu_layers,  # Force 0 if CPU mode
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
        self._ensure_model_loaded()

        if self._is_gguf:
            # Use llama.cpp API for GGUF models with retry for transient errors
            max_retries = 2
            last_error = None

            # Build stop sequences
            stop = list(kwargs.get("stop", []) or [])

            for attempt in range(max_retries + 1):
                try:
                    response = self._model.create_chat_completion(
                        messages=messages,
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

    async def chat_completion_stream(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """Streaming chat completion using async queue."""
        self._ensure_model_loaded()

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        model_name = model or self.default_model

        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        # Build stop sequences
        stop = list(kwargs.get("stop", []) or [])

        def run_streaming():
            """Run generation in thread, put tokens in queue"""
            with _get_inference_semaphore(self.max_concurrent):
                try:
                    if self._is_gguf:
                        # Use llama.cpp streaming for GGUF
                        for chunk in self._model.create_chat_completion(
                            messages=messages,
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

        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk

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
        token_timeout = 60  # 60 seconds max between tokens

        with _get_inference_semaphore(self.max_concurrent):
            _current_request = f"STREAM-{request_id}"
            logger.info(f"[STREAM-{request_id}] Processing started")
            try:
                if self._is_gguf:
                    # Build stop sequences
                    stop = list(kwargs.get("stop", []) or [])

                    # Use llama.cpp streaming for GGUF with error recovery
                    last_token_time = time.time()
                    try:
                        for chunk in self._model.create_chat_completion(
                            messages=messages,
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
