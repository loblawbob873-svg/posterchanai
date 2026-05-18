"""
OpenAI-compatible API router for LLM backend.
Supports both native llama-cpp-python and Ollama backends.
Provides both /v1/* and /api/* endpoints for maximum compatibility.
"""
import json
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Optional, AsyncGenerator
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.database import get_db
from app.models import Setting, APIKey, User
from app.utils.auth_utils import query_api_key_with_retry, get_user_from_api_key
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


async def inject_rag_context(messages: list, db: Session, user_id: int = 1, top_k: int = 3, rag_api_url: str = None) -> list:
    """
    Query RAG for relevant context based on the last user message and inject it into the conversation.
    Returns modified messages list with RAG context prepended to system prompt.

    If rag_api_url is provided, queries the remote RAG API instead of local RAG.
    """
    try:
        # Find the last user message to use as query
        user_query = None
        for msg in reversed(messages):
            if msg.get("role") == "user":
                user_query = msg.get("content", "")
                break

        if not user_query:
            return messages

        # Query RAG (remote or local)
        results = None
        if rag_api_url:
            # Query remote RAG API
            results = await _query_remote_rag(rag_api_url, user_query, top_k)
        else:
            # Use local RAG
            from app.services.rag_service import get_rag_service
            rag_service = get_rag_service(db, user_id=user_id)
            results = rag_service.query(user_query, top_k=top_k)

        if not results:
            return messages

        # Format RAG context
        rag_context = "\n\n## Relevant Code Context (from RAG):\n"
        for r in results:
            file_path = r.get("file_path", "unknown")
            content = r.get("content", "")[:500]  # Limit content length
            similarity = r.get("similarity", 0)
            rag_context += f"\n### {file_path} ({similarity:.0%} match):\n```\n{content}\n```\n"

        # Inject into system message or create one
        new_messages = messages.copy()
        if new_messages and new_messages[0].get("role") == "system":
            new_messages[0]["content"] = new_messages[0]["content"] + rag_context
        else:
            new_messages.insert(0, {"role": "system", "content": f"Use this context to help answer questions:{rag_context}"})

        source = "remote" if rag_api_url else "local"
        logger.info(f"Injected RAG context from {len(results)} results ({source})")
        return new_messages

    except Exception as e:
        logger.warning(f"RAG injection failed: {e}")
        return messages


async def _query_remote_rag(rag_api_url: str, query: str, top_k: int) -> list:
    """Query a remote RAG API server."""
    import httpx

    try:
        # Ensure URL ends with /search
        url = rag_api_url.rstrip('/')
        if not url.endswith('/search'):
            url = f"{url}/search"

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json={"query": query, "top_k": top_k}
            )
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])
    except Exception as e:
        logger.warning(f"Remote RAG query failed: {e}")
        return []


router = APIRouter(tags=["OpenAI API"])


async def filter_thinking_stream(stream: AsyncGenerator[str, None]) -> AsyncGenerator[str, None]:
    """Filter thinking tags from SSE stream, buffering until thinking section ends.

    Handles multiple tag variants:
    - <think>/<thinking>, <thought>, <reasoning>, <internal_thought>
    """
    buffer = ""
    thinking_done = False
    chunk_count = 0

    # Pattern to detect end of any thinking tag variant
    end_pattern = re.compile(r'</(?:think(?:ing)?|thought|reasoning|internal[_-]?thought)>', re.IGNORECASE)
    # Pattern to detect if any thinking tag is present
    start_pattern = re.compile(r'<(?:think(?:ing)?|thought|reasoning|internal[_-]?thought)', re.IGNORECASE)

    async for chunk in stream:
        chunk_count += 1
        if not chunk.startswith("data: "):
            yield chunk
            continue

        data_str = chunk[6:].strip()
        if data_str == "[DONE]":
            # Flush any remaining buffer
            if buffer:
                clean = strip_thinking_tags(buffer)
                buffer = ""  # clear so the post-loop cleanup doesn't re-emit
                if clean and clean != "I apologize, I wasn't able to generate a proper response. Please try again.":
                    # Re-emit as SSE chunk
                    yield f"data: {json.dumps({'choices': [{'delta': {'content': clean}}]})}\n\n"
            logger.debug(f"filter_thinking_stream: received [DONE] after {chunk_count} chunks")
            yield "data: [DONE]\n\n"
            return  # stop — nothing valid can come after [DONE]

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
            else:
                # No content in this chunk, but might be a valid chunk (like finish_reason)
                # Pass it through to maintain proper SSE format
                yield chunk
        except json.JSONDecodeError as e:
            # Invalid JSON, but pass it through anyway (might be a comment or other SSE data)
            logger.debug(f"filter_thinking_stream: JSON decode error on chunk {chunk_count}: {e}, passing through")
            yield chunk
    
    # Log if we didn't receive any chunks with content
    if chunk_count == 0:
        logger.warning("filter_thinking_stream: received 0 chunks from stream")
    elif buffer and not thinking_done:
        # We have buffered content but never found thinking end - flush it
        logger.debug(f"filter_thinking_stream: flushing remaining buffer (len={len(buffer)})")
        clean = strip_thinking_tags(buffer)
        if clean and clean != "I apologize, I wasn't able to generate a proper response. Please try again.":
            yield f"data: {json.dumps({'choices': [{'delta': {'content': clean}}]})}\n\n"


def verify_api_key(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> Optional[User]:
    """Verify API key from Authorization header or X-API-Key header. Returns the user if authenticated."""
    # Allow load-balanced requests from other posterchanai nodes without authentication
    if request:
        load_balanced_header = request.headers.get("x-posterchanai-load-balanced", "").lower()
        if load_balanced_header == "true":
            logger.debug(f"[OPENAI-API] ✓ Load-balanced request from another posterchanai node - allowing without auth")
            return None  # Authenticated but no specific user
    
    # Check X-API-Key header first (for user API keys)
    if x_api_key:
        x_api_key = str(x_api_key).strip()
        # Check user API keys from api_keys table
        api_key, user_id = query_api_key_with_retry(db, x_api_key)
        if api_key and user_id:
            user = get_user_from_api_key(db, user_id)
            if user:
                logger.debug(f"[OPENAI-API] Authenticated via X-API-Key header (User API Key: {user.username})")
                return user
    
    # Check authorization header
    if not authorization:
        # Allow unauthenticated access (for load-balanced requests or open access)
        logger.debug(f"[OPENAI-API] No authorization header - allowing unauthenticated access")
        return None

    # Extract token from "Bearer <token>" format
    if authorization.startswith("Bearer "):
        token = authorization[7:]
    else:
        token = authorization

    # Check user API keys - get api_key AND user_id together to avoid lazy loading
    api_key, user_id = query_api_key_with_retry(db, token)

    if api_key and user_id:
        # Get user using the already-fetched user_id (no lazy loading needed)
        user = get_user_from_api_key(db, user_id)
        
        if user:
            # Now update last used timestamp (after we've already fetched user)
            try:
                from datetime import timezone
                # Try to refresh the api_key object to ensure it's in a valid state
                # Update last_used_at using direct SQL to avoid SQLite parameter binding issues
                # SQLite can have issues with ORM updates, so use raw SQL
                try:
                    from sqlalchemy import text
                    now_utc = datetime.now(timezone.utc)
                    # Use parameterized query but with explicit parameter names to avoid SQLite issues
                    db.execute(
                        text("UPDATE api_keys SET last_used_at = :last_used_at WHERE id = :id"),
                        {"last_used_at": now_utc, "id": api_key.id}
                    )
                    db.commit()
                except Exception as e:
                    # If direct SQL update fails, try ORM method as fallback
                    try:
                        db.rollback()
                        # Fallback: try refreshing and updating via ORM
                        try:
                            db.refresh(api_key)
                        except Exception:
                            pass
                        api_key.last_used_at = datetime.now(timezone.utc)
                        db.commit()
                    except Exception as fallback_error:
                        # If both methods fail, rollback but we already have the user
                        try:
                            db.rollback()
                        except Exception:
                            pass
                        logger.warning(f"Failed to update API key last_used_at (both methods): {fallback_error}")
            except Exception as e:
                # If commit fails, rollback but we already have the user
                try:
                    db.rollback()
                except Exception:
                    pass  # Ignore rollback errors
                logger.warning(f"Failed to update API key last_used_at: {e}")
            
            return user
        else:
            raise HTTPException(status_code=401, detail="Invalid API key")

    # Token did not match any user API key
    raise HTTPException(status_code=401, detail="Invalid API key")


# ============== /v1 Endpoints ==============

@router.get("/v1")
async def v1_root():
    """OpenAI-compatible API root. Use base URL https://your-host/v1 for OpenCode/OpenAI clients."""
    return {
        "message": "OpenAI-compatible API",
        "endpoints": {
            "models": "/v1/models",
            "chat": "/v1/chat/completions",
        },
    }


@router.post("/v1/chat/completions")
async def v1_chat_completions(
    request: ChatCompletionRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(verify_api_key)
):
    """OpenAI-compatible chat completions endpoint"""
    # Check if request is from another posterchanai instance (via load balancer)
    # If so, skip load balancing to prevent loops
    skip_lb = False
    user_agent = http_request.headers.get("user-agent", "").lower()
    load_balanced_header = http_request.headers.get("x-posterchanai-load-balanced", "").lower()
    
    # Only skip load balancing if the request has the load-balanced header (from another posterchanai instance)
    # Don't skip just because user-agent contains "httpx" - external clients might use httpx too
    if load_balanced_header == "true":
        skip_lb = True
        logger.info(f"Detected load-balanced request (header=true), skipping load balancing to prevent loops")
    else:
        logger.debug(f"Request user-agent: {user_agent[:100] if user_agent else 'None'}, load-balanced header: {load_balanced_header}")
    return await _handle_chat_completions(request, db, skip_load_balancer=skip_lb)


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
    http_request: Request,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(verify_api_key)
):
    """OpenWebUI-compatible chat completions endpoint"""
    load_balanced_header = http_request.headers.get("x-posterchanai-load-balanced", "").lower()
    skip_lb = load_balanced_header == "true"
    if skip_lb:
        logger.info(f"Detected load-balanced request (header=true), skipping load balancing")
    return await _handle_chat_completions(request, db, skip_load_balancer=skip_lb)


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
    http_request: Request,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(verify_api_key)
):
    """Root-level chat completions endpoint"""
    load_balanced_header = http_request.headers.get("x-posterchanai-load-balanced", "").lower()
    skip_lb = load_balanced_header == "true"
    if skip_lb:
        logger.info(f"Detected load-balanced request (header=true), skipping load balancing")
    return await _handle_chat_completions(request, db, skip_load_balancer=skip_lb)


@router.get("/models")
async def root_list_models(
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(verify_api_key)
):
    """Root-level models list endpoint"""
    return await _handle_list_models(db)


# ============== Tool call helpers (opencode / agentic) ==============

_TC_RE = re.compile(r'<tool_call>\s*(.*?)\s*</tool_call>', re.DOTALL | re.IGNORECASE)
_THINK_STRIP_RE = re.compile(r'<think(?:ing)?>(.*?)</think(?:ing)?>', re.DOTALL | re.IGNORECASE)


def _resolve_model(request_model: str, settings: dict) -> str:
    """Return the model name to send to the inference backend.
    If request.model is a .gguf filename that exists next to llm_model_path, use it.
    Otherwise fall back to the basename of llm_model_path."""
    if request_model and request_model.endswith(".gguf"):
        llm_path = settings.get("llm_model_path", "")
        if llm_path:
            candidate = os.path.join(os.path.dirname(llm_path), request_model)
            if os.path.isfile(candidate):
                return request_model
    return os.path.basename(settings.get("llm_model_path", "")) or "default"


def _tools_system_text(tools: list) -> str:
    if not tools:
        return ""
    entries = []
    for t in tools:
        name = t.get("name", "")
        desc = t.get("description", "")
        schema = t.get("input_schema") or t.get("parameters") or {}
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = schema.get("required", []) if isinstance(schema, dict) else []
        params = []
        for pname, pdef in props.items():
            req = " (required)" if pname in required else ""
            pdesc = pdef.get("description", "") if isinstance(pdef, dict) else ""
            params.append(f"  {pname}{req}: {pdesc}")
        entries.append(f"### {name}\n{desc}" + ("\nParameters:\n" + "\n".join(params) if params else ""))
    return "<tools>\n" + "\n\n".join(entries) + "\n</tools>"


def _oai_messages_for_tools(messages: list, tools: list) -> list:
    """Convert OpenAI messages (with tool_calls / tool role) to model text format."""
    result = []
    tools_text = _tools_system_text(tools)
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []

        if role == "system":
            combined = (content + "\n\n" + tools_text).strip() if tools_text else content
            result.append({"role": "system", "content": combined})
            tools_text = ""  # only inject once
        elif role == "assistant" and tool_calls:
            parts = [content] if content else []
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except Exception:
                    args = {}
                parts.append(f'<tool_call>\n{json.dumps({"name": name, "arguments": args})}\n</tool_call>')
            result.append({"role": "assistant", "content": "\n".join(parts)})
        elif role == "tool":
            result.append({"role": "user", "content": str(content)})
        else:
            result.append({"role": role, "content": content})

    # If no system message existed, prepend tools
    if tools_text and (not result or result[0].get("role") != "system"):
        result.insert(0, {"role": "system", "content": tools_text})

    return result


_TOOL_NAME_MAP = {
    "read_file": "read",
    "write_file": "write",
    "create_file": "write",
    "str_replace_editor": "edit",
    "str_replace": "edit",
}

def _normalize_tool(name: str, args: dict) -> tuple:
    """Map model-native tool names/args to opencode's actual schema."""
    name = _TOOL_NAME_MAP.get(name, name)
    # snake_case → camelCase for file path arg
    if "file_path" in args and "filePath" not in args:
        args["filePath"] = args.pop("file_path")
    if "old_string" in args and "oldString" not in args and name == "edit":
        args["oldString"] = args.pop("old_string")
    if "new_string" in args and "newString" not in args and name == "edit":
        args["newString"] = args.pop("new_string")
    # bash requires description
    if name in ("bash", "Bash") and "description" not in args:
        args["description"] = (args.get("command") or "")[:80]
    return name, args


def _repair_json(raw: str) -> str:
    """Escape literal newlines/tabs inside JSON string values."""
    result = []
    in_string = False
    escape_next = False
    for c in raw:
        if escape_next:
            result.append(c)
            escape_next = False
        elif c == '\\' and in_string:
            result.append(c)
            escape_next = True
        elif c == '"':
            in_string = not in_string
            result.append(c)
        elif in_string and c == '\n':
            result.append('\\n')
        elif in_string and c == '\r':
            result.append('\\r')
        elif in_string and c == '\t':
            result.append('\\t')
        else:
            result.append(c)
    return ''.join(result)


def _parse_oai_tool_calls(text: str):
    """Parse <tool_call> blocks (JSON or XML sub-format); return (clean_text, openai_tool_calls_list)."""
    tool_calls = []
    for m in _TC_RE.finditer(text):
        raw = m.group(1).strip()
        # XML sub-format: <tool>NAME</tool><input>JSON</input>
        tool_m = re.search(r'<tool>\s*(.*?)\s*</tool>', raw, re.DOTALL | re.IGNORECASE)
        input_m = re.search(r'<input>\s*(.*?)\s*</input>', raw, re.DOTALL | re.IGNORECASE)
        if tool_m and input_m:
            name = tool_m.group(1).strip()
            try:
                arguments = json.loads(input_m.group(1).strip())
            except Exception:
                arguments = {}
        else:
            # JSON format: {"name": ..., "arguments": ...}
            try:
                parsed = json.loads(raw)
            except Exception:
                try:
                    parsed = json.loads(_repair_json(raw))
                except Exception:
                    try:
                        parsed = json.loads(re.sub(r'[\x00-\x1f]', ' ', raw))
                    except Exception:
                        continue
            name = parsed.get("name", "")
            arguments = parsed.get("arguments", {})
        if not name:
            continue
        if isinstance(arguments, dict):
            name, arguments = _normalize_tool(name, arguments)
        tool_calls.append({
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments) if isinstance(arguments, dict) else str(arguments),
            },
        })
    clean = _TC_RE.sub("", text).strip()
    # Strip any hallucinated <tool_result>...</tool_result> blocks
    clean = re.sub(r'<tool_result>.*?</tool_result>', '', clean, flags=re.DOTALL | re.IGNORECASE).strip()
    clean = _THINK_STRIP_RE.sub("", clean).strip()
    return clean, tool_calls


async def _agentic_completion(request: ChatCompletionRequest, db: Session, skip_load_balancer: bool):
    """Handle chat completions with tools (opencode agentic mode)."""
    from app.models import Setting
    from app.services.load_balancer import LoadBalancer, parse_server_urls, NoHealthyServersError

    settings = {s.key: s.value for s in db.query(Setting).all()}
    tools = [t.dict() if hasattr(t, "dict") else t for t in (request.tools or [])]
    messages = _oai_messages_for_tools(
        [{"role": m.role, "content": m.content,
          "tool_calls": getattr(m, "tool_calls", None),
          "tool_call_id": getattr(m, "tool_call_id", None)}
         for m in request.messages],
        tools,
    )

    llm_path = settings.get("llm_model_path", "").lower()
    if "qwen3" in llm_path:
        from app.services.text_utils import inject_no_think
        messages = inject_no_think(messages)

    temperature = request.temperature if request.temperature is not None else 0.0
    max_tokens = max(request.max_tokens or 0, int(settings.get("ollama_num_predict", "2048")))
    kwargs = {"temperature": temperature, "max_tokens": max_tokens}
    if request.top_p is not None:
        kwargs["top_p"] = request.top_p

    full_text = None
    usage = {}
    chat_server_urls = settings.get("chat_server_urls", "")
    servers = parse_server_urls(chat_server_urls, exclude_self=False) if not skip_load_balancer and chat_server_urls else []

    if servers:
        try:
            lb_model = _resolve_model(request.model, settings)
            timeout = int(settings.get("ollama_timeout", "300000")) / 1000
            lb = LoadBalancer(servers, timeout=timeout, model=lb_model)
            result = await lb.chat(messages=messages, **kwargs)
            if "error" not in result and result.get("choices"):
                full_text = result["choices"][0].get("message", {}).get("content", "") or ""
                usage = result.get("usage", {})
        except Exception as e:
            logger.warning(f"[OAI-AGENTIC] LB failed: {e}, using local")

    if full_text is None:
        prepare_vram_for_llm(db)
        service = get_inference_service(db)
        result = await service.chat_completion(messages=messages, model=request.model, **kwargs)
        if "error" in result:
            raise HTTPException(status_code=500, detail=str(result["error"]))
        full_text = result.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        usage = result.get("usage", {})

    clean_text, tool_calls = _parse_oai_tool_calls(full_text)
    logger.info(f"[OAI-AGENTIC] raw={full_text[:300]!r} tool_calls={len(tool_calls)}")
    finish_reason = "tool_calls" if tool_calls else "stop"
    msg = {"role": "assistant", "content": clean_text or None}
    if tool_calls:
        msg["tool_calls"] = tool_calls

    resp_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    body = {
        "id": resp_id, "object": "chat.completion",
        "created": int(__import__("time").time()),
        "model": request.model,
        "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }

    if not request.stream:
        return body

    # Emit as an SSE stream — spec-compliant tool_call deltas (index-keyed)
    async def _emit():
        def _chunk(delta, finish=None):
            return f"data: {json.dumps({'id': resp_id, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]})}\n\n"

        yield _chunk({"role": "assistant", "content": ""})
        if clean_text:
            for i in range(0, len(clean_text), 64):
                yield _chunk({"content": clean_text[i:i+64]})
        for tc_idx, tc in enumerate(tool_calls):
            # First chunk for this tool call: id + name
            yield _chunk({"tool_calls": [{"index": tc_idx, "id": tc["id"], "type": "function",
                                          "function": {"name": tc["function"]["name"], "arguments": ""}}]})
            # Stream arguments in chunks
            args_str = tc["function"]["arguments"]
            for i in range(0, len(args_str), 64):
                yield _chunk({"tool_calls": [{"index": tc_idx, "function": {"arguments": args_str[i:i+64]}}]})
        yield _chunk({}, finish=finish_reason)
        yield "data: [DONE]\n\n"

    from fastapi.responses import StreamingResponse as SR
    return SR(_emit(), media_type="text/event-stream",
              headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})


# ============== Shared Handlers ==============

async def _handle_chat_completions(request: ChatCompletionRequest, db: Session, skip_load_balancer: bool = False):
    """Handle chat completions request
    
    Args:
        request: Chat completion request
        db: Database session
        skip_load_balancer: If True, skip load balancing (used to prevent loops when called from another instance)
    """
    # Agentic path: tools present → use dedicated handler
    if request.tools:
        return await _agentic_completion(request, db, skip_load_balancer)

    from app.models import Setting
    from app.services.load_balancer import LoadBalancer, parse_server_urls

    # Check for load balancer first (unless explicitly skipped to prevent loops)
    # Load balancer ONLY uses what's configured in admin UI - round-robin between all configured servers
    settings = {s.key: s.value for s in db.query(Setting).all()}
    chat_server_urls = settings.get("chat_server_urls", "")
    # Parse server URLs from admin UI - use all servers for round-robin (including self if configured)
    servers = parse_server_urls(chat_server_urls, exclude_self=False) if not skip_load_balancer and chat_server_urls else []

    # Convert messages to dict format
    messages = [{"role": m.role, "content": m.content} for m in request.messages]

    # Inject the admin system prompt only when the caller did NOT supply one.
    # If the caller already has a system message they have set the context intentionally
    # (e.g. Sharkey's summarize / viral-post endpoints, or any focused task call) and
    # appending the bot personality would contaminate the prompt and cause the model to
    # echo it back or ignore the caller's instructions.
    # Skip entirely when the request came from another posterchanai instance (load-balanced)
    # to prevent double-injection.
    api_inject_system = settings.get("api_inject_system_prompt", "true").lower() == "true"
    has_system = bool(messages and messages[0].get("role") == "system")
    if api_inject_system and not skip_load_balancer and not has_system:
        system_prompt = settings.get("ollama_system_prompt", "")
        if system_prompt:
            messages.insert(0, {"role": "system", "content": system_prompt})

    # Inject RAG context if enabled
    rag_enabled = settings.get("api_rag_enabled", "true").lower() == "true"
    rag_api_url = settings.get("rag_api_url", "").strip() or None
    if rag_enabled:
        messages = await inject_rag_context(messages, db, user_id=1, top_k=3, rag_api_url=rag_api_url)

    # Ensure messages alternate user/assistant properly (prevents "roles must alternate" errors)
    from app.services.chat_service import ChatService
    messages = ChatService._ensure_alternating_roles(messages)

    # Inject /no_think into last user message for Qwen3 thinking models
    _llm_path = settings.get("llm_model_path", "").lower()
    if "qwen3" in _llm_path:
        from app.services.text_utils import inject_no_think
        messages = inject_no_think(messages)

    # Fetch URL content from the last user message and inject it so the LLM can summarize.
    # Only do this on the originating server (not on load-balanced hops).
    if not skip_load_balancer:
        try:
            import asyncio as _asyncio
            import re as _re
            from app.services.search_service import SearchService as _SS

            # Find the last user message
            _last_user_idx = None
            for _i in range(len(messages) - 1, -1, -1):
                if messages[_i].get("role") == "user":
                    _last_user_idx = _i
                    break

            if _last_user_idx is not None:
                _user_content = messages[_last_user_idx].get("content", "")
                if isinstance(_user_content, str):
                    _urls = _SS.extract_urls(_user_content)
                    # Deduplicate (www.x.com == x.com)
                    if _urls:
                        def _ukey(u):
                            return _re.sub(r'^https?://(www\.)?', '', u.lower().rstrip('/'))
                        _seen, _deduped = set(), []
                        for _u in _urls:
                            _k = _ukey(_u)
                            if _k not in _seen:
                                _seen.add(_k)
                                _deduped.append(_u)
                        _urls = _deduped

                    if _urls:
                        logger.info(f"[OPENAI-API] Fetching URLs for LLM context: {_urls}")
                        try:
                            from app.database import SessionLocal as _SL
                            _tmp_db = _SL()
                            _ss = _SS(_tmp_db)
                            _fetched = await _asyncio.wait_for(
                                _ss.fetch_urls(_urls, max_urls=3), timeout=15
                            )
                            _tmp_db.close()
                        except Exception:
                            _fetched = []

                        _url_context = ""
                        MAX_CHARS = 2000
                        for _r in _fetched:
                            if _r.get("content") and not _r.get("error"):
                                _c = _r["content"][:MAX_CHARS]
                                _url_context += f"\n\n---\nContent from {_r['url']}:\nTitle: {_r['title']}\n\n{_c}\n---"
                            elif _r.get("error"):
                                logger.warning(f"[OPENAI-API] Failed to fetch {_r['url']}: {_r['error']}")

                        if _url_context:
                            # Detect what text the user wrote around the URL(s)
                            _text_without_urls = _user_content
                            for _u in _urls:
                                _text_without_urls = _text_without_urls.replace(_u, '').strip()

                            # "summarize URL" or bare URL → clean summarization request.
                            # Replace the user message entirely so the persona system prompt
                            # doesn't confuse the model ("Sure thing!" / "👋 Goodbye!" etc.)
                            _summarize_words = {"summarize", "summarise", "summary", "tldr", "tl;dr"}
                            _is_summarize_req = (
                                not _text_without_urls or
                                _text_without_urls.lower().rstrip(".,!?:") in _summarize_words
                            )

                            if _is_summarize_req:
                                # Clean message: article content first, then a single clear instruction
                                messages[_last_user_idx]["content"] = (
                                    _url_context.strip() +
                                    "\n\nWrite a single concise paragraph summarizing the above article."
                                )
                            else:
                                # User has a specific question/task — append the content as reference
                                messages[_last_user_idx]["content"] = (
                                    _user_content +
                                    f"\n\nHere is the content from the URLs mentioned above:{_url_context}"
                                )

                            logger.info(f"[OPENAI-API] Injected {len(_url_context)} chars of URL context (summarize={_is_summarize_req})")
        except Exception as _url_err:
            logger.warning(f"[OPENAI-API] URL fetch failed, continuing without context: {_url_err}")

    # Build kwargs
    temperature = request.temperature if request.temperature is not None else float(settings.get("ollama_temperature", "0.7"))
    top_p = request.top_p if request.top_p is not None else float(settings.get("ollama_top_p", "0.9"))
    server_num_predict = int(settings.get("ollama_num_predict", "2048"))
    max_tokens = max(request.max_tokens, server_num_predict) if request.max_tokens is not None else server_num_predict

    # Use load balancer if configured - picks server round-robin, uses local inference for "self" URLs
    # Skip if explicitly requested (to prevent loops when called from another posterchanai instance)
    if servers and not skip_load_balancer:
        from app.services.load_balancer import get_healthy_server, is_self_url, NoHealthyServersError

        # Server-to-server requests don't need authentication
        try:
            # Pass full server list to LoadBalancer - it will handle round-robin internally
            # This ensures proper load balancing across all servers
            timeout = int(settings.get("ollama_timeout", "300000")) / 1000
            model = _resolve_model(request.model, settings)
            logger.info(f"[OPENAI API] LoadBalancer model={model!r}")
            load_balancer = LoadBalancer(servers, timeout=timeout, model=model)

            try:
                if request.stream:
                    # Create a wrapper that catches NoHealthyServersError and re-raises it
                    # so the outer handler can catch it and fall back to local
                    lb_stream = load_balancer.chat_stream(
                        messages=messages,
                        temperature=temperature,
                        top_p=top_p,
                        max_tokens=max_tokens
                    )
                    
                    # Wrap generator - if NoHealthyServersError is raised, stop silently
                    # The outer exception handler will catch it when StreamingResponse fails
                    # and fall back to local inference
                    async def safe_stream():
                        try:
                            async for chunk in filter_thinking_stream(lb_stream):
                                yield chunk
                        except NoHealthyServersError:
                            # Remote server unavailable - stop generator silently
                            # FastAPI will see an empty stream, but the outer handler
                            # should catch the exception and fall back to local
                            # For now, just stop - don't yield anything
                            return
                    
                    return StreamingResponse(
                        safe_stream(),
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
                        logger.warning(f"Load balancer returned error, falling back to local: {result.get('error')}")
                        # Fall through to local inference instead of raising exception
                        raise NoHealthyServersError(f"Remote server returned error: {result.get('error')}")
                    # Strip thinking tags from load balancer response
                    if result.get("choices"):
                        for choice in result["choices"]:
                            if choice.get("message", {}).get("content"):
                                choice["message"]["content"] = strip_thinking_tags(choice["message"]["content"])
                    return result
            except (NoHealthyServersError, Exception) as e:
                # NoHealthyServersError is expected when remote returns empty stream - fallback is automatic
                # Don't log this as it's expected behavior - just silently fall back to local
                if not isinstance(e, NoHealthyServersError):
                    logger.warning(f"Load balancer request failed ({type(e).__name__}: {e}), falling back to local inference")
                # Fall through to local inference (silent fallback for NoHealthyServersError)
        except Exception as e:
            logger.error(f"Load balancer error: {e}, falling back to local inference", exc_info=True)
            # Fall through to local inference

    # Fall back to local inference service
    logger.info("Processing with local inference service")
    
    # Prepare VRAM for LLM (unload image model if needed in shared mode)
    from app.services.inference_factory import prepare_vram_for_llm
    prepare_vram_for_llm(db)
    
    service = get_inference_service(db)

    # Build kwargs from request
    kwargs = {}
    if request.temperature is not None:
        kwargs["temperature"] = request.temperature
    if request.top_p is not None:
        kwargs["top_p"] = request.top_p
    if request.max_tokens is not None:
        kwargs["max_tokens"] = max(request.max_tokens, server_num_predict)
    if request.stop is not None:
        kwargs["stop"] = request.stop

    # Handle streaming vs non-streaming
    if request.stream:
        logger.info("Starting local streaming inference")
        try:
            stream = service.chat_completion_stream(
                messages=messages,
                model=request.model,
                **kwargs
            )
            logger.info("Stream generator created, returning StreamingResponse")
            return StreamingResponse(
                filter_thinking_stream(stream),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                }
            )
        except Exception as e:
            logger.error(f"Error creating stream: {e}", exc_info=True)
            raise
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
    """Handle models list request.
    Model id is the backend's model name (e.g. .gguf filename for native).
    Context size is from server settings, minimum 16000 for OpenClaw compatibility.
    """
    from app.services.inference_factory import get_backend_type

    service = get_inference_service(db)
    models = await service.list_models()
    backend = get_backend_type(db)

    # Use actual server context size; minimum 16000 so OpenClaw and other clients accept it
    setting = db.query(Setting).filter(Setting.key == "ollama_num_ctx").first()
    ctx = int(setting.value) if setting and setting.value.isdigit() else 4096
    ctx = max(ctx, 16000)

    model_list = []
    for model in models:
        model_list.append(ModelInfo(
            id=model.get("name", "unknown"),
            object="model",
            created=0,
            owned_by="native" if backend == "native" else "ollama",
            root_context_length=ctx,
            context_length=ctx,
        ))

    return ModelsResponse(object="list", data=model_list)
