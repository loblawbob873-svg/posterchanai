"""
OpenAI-compatible API router for Ollama backend.
Provides both /v1/* and /api/* endpoints for maximum compatibility.
"""
import time
import uuid
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

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
from app.services.ollama_service import get_ollama_service


router = APIRouter(tags=["OpenAI API"])


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
    ollama = get_ollama_service(db)

    # Convert messages to dict format
    messages = [{"role": m.role, "content": m.content} for m in request.messages]

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
        return StreamingResponse(
            ollama.chat_completion_stream(
                messages=messages,
                model=request.model,
                **kwargs
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
    else:
        result = await ollama.chat_completion(
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

        return result


async def _handle_list_models(db: Session):
    """Handle models list request"""
    ollama = get_ollama_service(db)
    models = await ollama.list_models()

    # Convert to OpenAI format
    model_list = []
    for model in models:
        model_list.append(ModelInfo(
            id=model.get("name", "unknown"),
            object="model",
            created=0,
            owned_by="ollama"
        ))

    return ModelsResponse(object="list", data=model_list)
