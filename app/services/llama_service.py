"""
Native LLM Service using llama-cpp-python with GPU acceleration.
Supports Intel Arc (SYCL), NVIDIA (CUDA), and CPU fallback.
"""
import asyncio
import json
import logging
import os
import re
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


class LlamaService:
    """
    Native LLM inference service using llama-cpp-python.
    Keeps model loaded in memory for fast inference.
    """

    def __init__(self, db: Session):
        self.db = db
        self._model = None
        self._model_path: Optional[str] = None
        self._load_settings()
        _start_idle_check()

    def _load_settings(self):
        """Load settings from database"""
        settings = {s.key: s.value for s in self.db.query(Setting).all()}

        # Model settings
        self.model_path = settings.get("llm_model_path", "/home/verita84/models/model.gguf")
        self.default_model = settings.get("ollama_model", "native")

        # Context and generation settings
        self.num_ctx = int(settings.get("ollama_num_ctx", "4096"))
        self.num_predict = int(settings.get("ollama_num_predict", "2048"))

        # GPU settings
        self.n_gpu_layers = int(settings.get("llm_gpu_layers", "-1"))  # -1 = all layers on GPU
        self.max_concurrent = int(settings.get("llm_max_concurrent", "1"))  # Max concurrent inferences

        # CPU settings - auto-detect threads if set to 0
        n_threads_setting = int(settings.get("llm_n_threads", "0"))
        if n_threads_setting <= 0:
            import os
            cpu_count = os.cpu_count() or 4
            # Use physical cores (cpu_count // 2) for better performance
            # SMT/hyperthreading can cause contention during inference
            self.n_threads = max(1, cpu_count // 2)
            logger.info(f"Auto-detected CPU threads: {self.n_threads} (physical cores from {cpu_count} logical)")
        else:
            self.n_threads = n_threads_setting

        # CPU optimization settings
        self.cpu_mode = settings.get("llm_cpu_mode", "false").lower() == "true"
        self.n_batch = int(settings.get("llm_n_batch", "2048"))
        self.use_mmap = settings.get("llm_use_mmap", "true").lower() == "true"
        self.use_mlock = settings.get("llm_use_mlock", "true").lower() == "true"

        # Sampling settings
        self.temperature = float(settings.get("ollama_temperature", "0.7"))
        self.top_p = float(settings.get("ollama_top_p", "0.9"))
        self.top_k = int(settings.get("ollama_top_k", "40"))
        self.repeat_penalty = float(settings.get("ollama_repeat_penalty", "1.1"))

        # Advanced settings
        self.mirostat = int(settings.get("ollama_mirostat", "0"))
        self.mirostat_eta = float(settings.get("ollama_mirostat_eta", "0.1"))
        self.mirostat_tau = float(settings.get("ollama_mirostat_tau", "5.0"))
        seed_str = settings.get("ollama_seed", "")
        self.seed = int(seed_str) if seed_str.strip() else -1

        # Stop sequences
        self.stop_sequences = [s.strip() for s in settings.get("ollama_stop", "").split(",") if s.strip()]

        # Disable thinking mode (for Qwen3 and similar models)
        self.disable_thinking = settings.get("llm_disable_thinking", "false").lower() == "true"

        # Idle timeout for automatic unloading (0 = disabled)
        self._idle_timeout = int(settings.get("llm_idle_timeout", "0"))

        # System prompt
        self.system_prompt = settings.get("ollama_system_prompt", "You are a helpful, friendly AI assistant.")

    def _ensure_model_loaded(self):
        """Load model if not already loaded or if path changed"""
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
            self._model = None

        logger.info(f"Loading model: {self.model_path}")
        logger.info(f"  Context size: {self.num_ctx}")
        logger.info(f"  GPU layers: {self.n_gpu_layers}")
        logger.info(f"  CPU threads: {self.n_threads}")

        try:
            from llama_cpp import Llama

            # Use admin-configured context size
            logger.info(f"  Using context size: {self.num_ctx}")

            # Determine GPU layers - force 0 if CPU mode enabled
            gpu_layers = 0 if self.cpu_mode else self.n_gpu_layers
            logger.info(f"  GPU layers: {gpu_layers} (CPU mode: {self.cpu_mode})")
            logger.info(f"  Batch size: {self.n_batch}, mmap: {self.use_mmap}, mlock: {self.use_mlock}")

            self._model = Llama(
                model_path=self.model_path,
                n_ctx=self.num_ctx,
                n_gpu_layers=gpu_layers,
                n_threads=self.n_threads,
                n_threads_batch=self.n_threads,  # Use same threads for batch processing
                n_batch=self.n_batch,
                use_mmap=self.use_mmap,
                use_mlock=self.use_mlock,
                flash_attn=False,
                verbose=False,
            )
            self._model_path = self.model_path
            logger.info("Model loaded successfully")

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

        # Add stop sequences
        stop = list(overrides.get("stop", self.stop_sequences) or [])
        if isinstance(stop, str):
            stop = [stop]

        # Add thinking stop sequences if disabled (for Qwen3 and similar)
        if self.disable_thinking:
            thinking_stops = ["<think>", "<thinking>"]
            for ts in thinking_stops:
                if ts not in stop:
                    stop.append(ts)

        if stop:
            params["stop"] = stop

        return params

    def strip_thinking_tags(self, response: str) -> str:
        """Strip thinking tags from AI response"""
        from app.services.text_utils import strip_thinking_tags
        return strip_thinking_tags(response)

    def _sync_chat_completion(self, messages: List[Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        """Synchronous chat completion (runs in thread pool)"""
        self._ensure_model_loaded()

        params = self._get_sampling_params(**kwargs)

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
        loop = asyncio.get_event_loop()

        # Run synchronous inference in thread pool
        result = await loop.run_in_executor(
            _executor,
            lambda: self._sync_chat_completion(messages, **kwargs)
        )

        return result

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
        self._ensure_model_loaded()

        params = self._get_sampling_params(**kwargs)
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        model_name = model or self.default_model

        queue = asyncio.Queue()
        loop = asyncio.get_event_loop()

        def run_streaming():
            """Run synchronous generation in thread, put SSE chunks in queue"""
            with _get_inference_semaphore(self.max_concurrent):
                try:
                    for chunk in self._model.create_chat_completion(
                        messages=messages,
                        stream=True,
                        **params
                    ):
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
                    loop.call_soon_threadsafe(queue.put_nowait, None)

        # Start streaming in background thread
        _executor.submit(run_streaming)

        # Yield from queue as chunks arrive
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
        """
        Direct content streaming generator (no SSE formatting).
        For internal use by web UI - more efficient than parsing SSE.
        """
        self._ensure_model_loaded()
        params = self._get_sampling_params(**kwargs)

        with _get_inference_semaphore(self.max_concurrent):
            try:
                for chunk in self._model.create_chat_completion(
                    messages=messages,
                    stream=True,
                    **params
                ):
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
        import os

        models = []

        # Check model directory for .gguf files
        model_dir = os.path.dirname(self.model_path)
        if os.path.isdir(model_dir):
            for filename in os.listdir(model_dir):
                if filename.endswith(".gguf"):
                    models.append({
                        "name": filename,
                        "model": filename,
                        "size": os.path.getsize(os.path.join(model_dir, filename)),
                    })

        return models

    def reload_model(self):
        """Force reload the model (useful after settings change)"""
        if self._model is not None:
            logger.info("Force reloading model...")
            try:
                del self._model
            except AttributeError as e:
                # Handle incomplete model objects (e.g., sampler not initialized)
                logger.warning(f"Error during model cleanup (ignored): {e}")
            self._model = None
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
            self._model = None
            self._model_path = None

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
