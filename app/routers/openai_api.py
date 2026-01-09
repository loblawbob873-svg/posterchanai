"""
OpenAI-compatible API router for LLM backend.
Supports both native llama-cpp-python and Ollama backends.
Provides both /v1/* and /api/* endpoints for maximum compatibility.
"""
import json
import logging
import re
from datetime import datetime
from typing import Optional, AsyncGenerator
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.database import get_db
from app.models import Setting, APIKey, User
from app.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionChoice,
    ChatCompletionUsage,
    ChatMessage,
    ModelInfo,
    ModelsResponse,
)
from app.services.inference_factory import get_inference_service
from app.services.text_utils import strip_thinking_tags


router = APIRouter(tags=["OpenAI API"])


async def filter_thinking_stream(stream: AsyncGenerator[str, None]) -> AsyncGenerator[str, None]:
    """Filter thinking tags from SSE stream, buffering until thinking section ends.

    Handles multiple tag variants:
    - <think>/<thinking>, <thought>, <reasoning>, <internal_thought>
    """
    buffer = ""
    thinking_done = False

    # Pattern to detect end of any thinking tag variant
    end_pattern = re.compile(r'</(?:think(?:ing)?|thought|reasoning|internal[_-]?thought)>', re.IGNORECASE)
    # Pattern to detect if any thinking tag is present
    start_pattern = re.compile(r'<(?:think(?:ing)?|thought|reasoning|internal[_-]?thought)', re.IGNORECASE)

    async for chunk in stream:
        if not chunk.startswith("data: "):
            yield chunk
            continue

        data_str = chunk[6:].strip()
        if data_str == "[DONE]":
            # Flush any remaining buffer
            if buffer:
                clean = strip_thinking_tags(buffer)
                if clean and clean != "I apologize, I wasn't able to generate a proper response. Please try again.":
                    # Re-emit as SSE chunk
                    yield f"data: {json.dumps({'choices': [{'delta': {'content': clean}}]})}\n\n"
            yield "data: [DONE]\n\n"
            continue

        try:
            data = json.loads(data_str)
            content = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
            if content:
                if not thinking_done:
                    buffer += content
                    # Look for end of any thinking tag variant
                    match = end_pattern.search(buffer)
                    if match:
                        thinking_done = True
                        after_think = buffer[match.end():]
                        buffer = ""  # Clear buffer - we're done with thinking
                        if after_think.strip():
                            # Re-emit content after thinking
                            data["choices"][0]["delta"]["content"] = after_think
                            yield f"data: {json.dumps(data)}\n\n"
                    # Check if no thinking tag in first 50 chars - assume no thinking
                    elif len(buffer) > 50 and not start_pattern.search(buffer):
                        thinking_done = True
                        data["choices"][0]["delta"]["content"] = buffer
                        yield f"data: {json.dumps(data)}\n\n"
                        buffer = ""  # Clear buffer - we're done with thinking
                else:
                    # thinking_done=True, just pass through chunks (don't buffer)
                    yield chunk
        except json.JSONDecodeError:
            yield chunk


def verify_api_key(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> Optional[User]:
    """Verify API key from Authorization header. Returns the user if authenticated."""
    # Check authorization header
    if not authorization:
        # Check if there's a global API key configured
        setting = db.query(Setting).filter(Setting.key == "openai_api_key").first()
        if not setting or not setting.value:
            # No auth required if no global key set
            return None
        raise HTTPException(status_code=401, detail="Missing API key")

    # Extract token from "Bearer <token>" format
    if authorization.startswith("Bearer "):
        token = authorization[7:]
    else:
        token = authorization

    # First check global API key
    setting = db.query(Setting).filter(Setting.key == "openai_api_key").first()
    if setting and setting.value and token == setting.value:
        return None  # Global key, no specific user

    # Check user API keys
    api_key = db.query(APIKey).filter(
        APIKey.key == token,
        APIKey.is_active == True
    ).first()

    if api_key:
        # Update last used timestamp
        api_key.last_used_at = datetime.utcnow()
        db.commit()
        return api_key.user

    # If global key is not set and user key not found, reject
    if not setting or not setting.value:
        raise HTTPException(status_code=401, detail="Invalid API key")

    raise HTTPException(status_code=401, detail="Invalid API key")


# ============== /v1 Endpoints ==============

@router.post("/v1/chat/completions")
async def v1_chat_completions(
    request: ChatCompletionRequest,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(verify_api_key)
):
    """OpenAI-compatible chat completions endpoint"""
    return await _handle_chat_completions(request, db)


@router.get("/v1/models")
async def v1_list_models(
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(verify_api_key)
):
    """OpenAI-compatible models list endpoint"""
    return await _handle_list_models(db)


# ============== /api Endpoints (OpenWebUI compatibility) ==============

@router.post("/api/chat/completions")
async def api_chat_completions(
    request: ChatCompletionRequest,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(verify_api_key)
):
    """OpenWebUI-compatible chat completions endpoint"""
    return await _handle_chat_completions(request, db)


@router.get("/api/models")
async def api_list_models(
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(verify_api_key)
):
    """OpenWebUI-compatible models list endpoint"""
    return await _handle_list_models(db)


# ============== Root-level Endpoints (maximum compatibility) ==============

@router.post("/chat/completions")
async def root_chat_completions(
    request: ChatCompletionRequest,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(verify_api_key)
):
    """Root-level chat completions endpoint"""
    return await _handle_chat_completions(request, db)


@router.get("/models")
async def root_list_models(
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(verify_api_key)
):
    """Root-level models list endpoint"""
    return await _handle_list_models(db)


# ============== Shared Handlers ==============

async def _handle_chat_completions(request: ChatCompletionRequest, db: Session):
    """Handle chat completions request"""
    from app.models import Setting
    from app.services.load_balancer import LoadBalancer, parse_server_urls

    # Check for load balancer first
    settings = {s.key: s.value for s in db.query(Setting).all()}
    chat_server_urls = settings.get("chat_server_urls", "")
    servers = parse_server_urls(chat_server_urls)

    # Convert messages to dict format
    messages = [{"role": m.role, "content": m.content} for m in request.messages]

    # Build kwargs
    temperature = request.temperature if request.temperature is not None else float(settings.get("ollama_temperature", "0.7"))
    top_p = request.top_p if request.top_p is not None else float(settings.get("ollama_top_p", "0.9"))
    max_tokens = request.max_tokens if request.max_tokens is not None else int(settings.get("ollama_num_predict", "2048"))

    # Use load balancer if configured (alternates between local and remote)
    if servers:
        from app.services.load_balancer import should_use_remote, NoHealthyServersError

        if await should_use_remote(len(servers)):
            # This request goes to a remote server
            timeout = int(settings.get("ollama_timeout", "120000")) / 1000
            model = settings.get("ollama_model", "default")
            load_balancer = LoadBalancer(servers, timeout=timeout, model=model)

            try:
                if request.stream:
                    lb_stream = load_balancer.chat_stream(
                        messages=messages,
                        temperature=temperature,
                        top_p=top_p,
                        max_tokens=max_tokens
                    )
                    return StreamingResponse(
                        filter_thinking_stream(lb_stream),
                        media_type="text/event-stream",
                        headers={
                            "Cache-Control": "no-cache",
                            "Connection": "keep-alive",
                            "X-Accel-Buffering": "no",
                        }
                    )
                else:
                    result = await load_balancer.chat(
                        messages=messages,
                        temperature=temperature,
                        top_p=top_p,
                        max_tokens=max_tokens
                    )
                    if "error" in result:
                        raise HTTPException(
                            status_code=500,
                            detail=result["error"].get("message", "Unknown error")
                        )
                    # Strip thinking tags from load balancer response
                    if result.get("choices"):
                        for choice in result["choices"]:
                            if choice.get("message", {}).get("content"):
                                choice["message"]["content"] = strip_thinking_tags(choice["message"]["content"])
                    return result
            except NoHealthyServersError:
                # No healthy remote servers, fall through to local processing
                logger.info("No healthy remote servers, processing locally")
        else:
            # should_use_remote returned False, use local
            logger.info("Load balancer: using LOCAL inference")

    # Fall back to local inference service
    logger.info("Processing with local inference service")
    service = get_inference_service(db)

    # Build kwargs from request
    kwargs = {}
    if request.temperature is not None:
        kwargs["temperature"] = request.temperature
    if request.top_p is not None:
        kwargs["top_p"] = request.top_p
    if request.max_tokens is not None:
        kwargs["max_tokens"] = request.max_tokens
    if request.stop is not None:
        kwargs["stop"] = request.stop

    # Handle streaming vs non-streaming
    if request.stream:
        stream = service.chat_completion_stream(
            messages=messages,
            model=request.model,
            **kwargs
        )
        return StreamingResponse(
            filter_thinking_stream(stream),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
    else:
        result = await service.chat_completion(
            messages=messages,
            model=request.model,
            **kwargs
        )

        # Check for error
        if "error" in result:
            raise HTTPException(
                status_code=result["error"].get("code", 500),
                detail=result["error"].get("message", "Unknown error")
            )

        # Strip thinking tags from response content
        if result.get("choices"):
            for choice in result["choices"]:
                if choice.get("message", {}).get("content"):
                    choice["message"]["content"] = strip_thinking_tags(choice["message"]["content"])

        return result


async def _handle_list_models(db: Session):
    """Handle models list request"""
    from app.services.inference_factory import get_backend_type

    service = get_inference_service(db)
    models = await service.list_models()
    backend = get_backend_type(db)

    # Convert to OpenAI format
    model_list = []
    for model in models:
        model_list.append(ModelInfo(
            id=model.get("name", "unknown"),
            object="model",
            created=0,
            owned_by="native" if backend == "native" else "ollama"
        ))

    return ModelsResponse(object="list", data=model_list)
