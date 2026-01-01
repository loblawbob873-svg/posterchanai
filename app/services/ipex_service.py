"""
IPEX-LLM Service for Intel Arc GPU acceleration.
Uses Intel's optimized LLM inference for maximum performance on Arc GPUs.
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
logger = logging.getLogger("ipex_service")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [IPEX] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(handler)


# Global model instance (singleton)
_ipex_instance: Optional["IPEXService"] = None
_executor = ThreadPoolExecutor(max_workers=1)
_inference_lock = threading.Lock()


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

    def _load_settings(self):
        """Load settings from database"""
        settings = {s.key: s.value for s in self.db.query(Setting).all()}

        # Model settings
        self.model_path = settings.get("llm_model_path", "/home/verita84/models/model.gguf")
        self.default_model = settings.get("ollama_model", "ipex")

        # Context and generation settings
        self.num_ctx = int(settings.get("ollama_num_ctx", "4096"))
        self.num_predict = int(settings.get("ollama_num_predict", "2048"))

        # Sampling settings
        self.temperature = float(settings.get("ollama_temperature", "0.7"))
        self.top_p = float(settings.get("ollama_top_p", "0.9"))
        self.top_k = int(settings.get("ollama_top_k", "40"))
        self.repeat_penalty = float(settings.get("ollama_repeat_penalty", "1.1"))

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
            del self._tokenizer
            self._model = None
            self._tokenizer = None

        logger.info(f"Loading model with IPEX-LLM: {self.model_path}")
        logger.info(f"  Context size: {self.num_ctx}")

        try:
            # Check if GGUF model - use llama.cpp backend
            if self.model_path.endswith('.gguf'):
                from ipex_llm.llama_cpp import Llama

                self._model = Llama(
                    model_path=self.model_path,
                    n_ctx=self.num_ctx,
                    n_gpu_layers=-1,  # All layers on GPU
                    verbose=False,
                )
                self._tokenizer = None  # llama.cpp handles tokenization
                self._is_gguf = True
                logger.info("GGUF model loaded with IPEX-LLM llama.cpp backend")
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
            logger.error(f"Failed to load model: {e}")
            raise

    def strip_thinking_tags(self, response: str) -> str:
        """Strip thinking tags from AI response"""
        matches = list(re.finditer(r'</think(?:ing)?>', response, re.IGNORECASE))
        if matches:
            last_match = matches[-1]
            return response[last_match.end():].strip()
        return response

    def _generate_response(self, messages: List[Dict[str, Any]], **kwargs) -> str:
        """Generate a response synchronously"""
        self._ensure_model_loaded()

        if self._is_gguf:
            # Use llama.cpp API for GGUF models
            response = self._model.create_chat_completion(
                messages=messages,
                max_tokens=kwargs.get("max_tokens", self.num_predict),
                temperature=kwargs.get("temperature", self.temperature),
                top_p=kwargs.get("top_p", self.top_p),
                top_k=kwargs.get("top_k", self.top_k),
                repeat_penalty=kwargs.get("repeat_penalty", self.repeat_penalty),
            )
            content = response["choices"][0]["message"]["content"]
            return self.strip_thinking_tags(content)
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
        """Non-streaming chat completion."""
        loop = asyncio.get_event_loop()

        with _inference_lock:
            response = await loop.run_in_executor(
                _executor,
                lambda: self._generate_response(messages, **kwargs)
            )

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
        loop = asyncio.get_event_loop()

        def run_streaming():
            """Run generation in thread, put tokens in queue"""
            with _inference_lock:
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

                        import threading
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
        """Direct content streaming generator for web UI."""
        self._ensure_model_loaded()

        with _inference_lock:
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
                        stream=True,
                    ):
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
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

                    import threading
                    gen_thread = threading.Thread(
                        target=lambda: self._model.generate(**generation_kwargs)
                    )
                    gen_thread.start()

                    for token in streamer:
                        if token:
                            yield token

                    gen_thread.join()

            except Exception as e:
                logger.error(f"Stream content error: {e}")
                yield f"Error: {e}"

    async def list_models(self) -> List[Dict[str, Any]]:
        """List available models"""
        import os

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
            del self._model
            del self._tokenizer
            self._model = None
            self._tokenizer = None
            self._model_path = None
        self._load_settings()
        self._ensure_model_loaded()

    def unload_model(self):
        """Unload the model from memory"""
        if self._model is not None:
            logger.info("Unloading model from memory")
            del self._model
            del self._tokenizer
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
