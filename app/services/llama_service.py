"""
Native LLM Service using llama-cpp-python with GPU acceleration.
Supports Intel Arc (SYCL), NVIDIA (CUDA), and CPU fallback.
"""
import asyncio
import json
import logging
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
_executor = ThreadPoolExecutor(max_workers=1)  # Single worker to prevent concurrent inference
_inference_lock = threading.Lock()  # Ensure only one inference at a time


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
        self.n_threads = int(settings.get("llm_n_threads", "4"))

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

        # System prompt
        self.system_prompt = settings.get("ollama_system_prompt", "You are a helpful, friendly AI assistant.")

    def _ensure_model_loaded(self):
        """Load model if not already loaded or if path changed"""
        if self._model is not None and self._model_path == self.model_path:
            return

        # Unload previous model
        if self._model is not None:
            logger.info(f"Unloading previous model: {self._model_path}")
            del self._model
            self._model = None

        logger.info(f"Loading model: {self.model_path}")
        logger.info(f"  Context size: {self.num_ctx}")
        logger.info(f"  GPU layers: {self.n_gpu_layers}")
        logger.info(f"  CPU threads: {self.n_threads}")

        try:
            from llama_cpp import Llama

            self._model = Llama(
                model_path=self.model_path,
                n_ctx=self.num_ctx,
                n_gpu_layers=self.n_gpu_layers,
                n_threads=self.n_threads,
                n_batch=512,  # Larger batch for faster prompt processing
                flash_attn=True,  # Enable flash attention if supported
                verbose=False,
                chat_format="chatml",  # Works with most models
            )
            self._model_path = self.model_path
            logger.info("Model loaded successfully")

        except Exception as e:
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
        stop = overrides.get("stop", self.stop_sequences)
        if stop:
            params["stop"] = stop if isinstance(stop, list) else [stop]

        return params

    def strip_thinking_tags(self, response: str) -> str:
        """Strip thinking tags from AI response"""
        matches = list(re.finditer(r'</think(?:ing)?>', response, re.IGNORECASE))
        if matches:
            last_match = matches[-1]
            return response[last_match.end():].strip()
        return response

    def _sync_chat_completion(self, messages: List[Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        """Synchronous chat completion (runs in thread pool)"""
        self._ensure_model_loaded()

        params = self._get_sampling_params(**kwargs)

        with _inference_lock:
            try:
                result = self._model.create_chat_completion(
                    messages=messages,
                    **params
                )

                # Strip thinking tags from response
                content = result["choices"][0]["message"]["content"]
                content = self.strip_thinking_tags(content)
                result["choices"][0]["message"]["content"] = content

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
            with _inference_lock:
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

        with _inference_lock:
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
            del self._model
            self._model = None
            self._model_path = None
        self._load_settings()
        self._ensure_model_loaded()

    def unload_model(self):
        """Unload the model from memory"""
        if self._model is not None:
            logger.info("Unloading model from memory")
            del self._model
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
