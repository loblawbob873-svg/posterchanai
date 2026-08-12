"""
OpenAI-compatible API router for LLM backend.
Supports both native llama-cpp-python and Ollama backends.
Provides both /v1/* and /api/* endpoints for maximum compatibility.
"""
import json
import logging
import os
import re
from datetime import datetime
from typing import Optional, AsyncGenerator
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Default small-context coding guidance, appended server-side to the system prompt of any request
# that carries tools (opencode et al.). Lets large-file editing behave on small-ctx local models
# without each client maintaining its own AGENTS.md. Admin-overridable via the `tool_guidance_text`
# setting; disable with `tool_guidance_enabled=false`. Kept generic (applies to ANY tool-bearing
# client) per the keep-proxy-generic rule.
_DEFAULT_TOOL_GUIDANCE = (
    "You are running on a model with a SMALL context window. Read efficiently, write whole files, "
    "and finish what you start:\n"
    "- Never read a whole file just to find something: use grep/glob to locate the exact lines, "
    "then read with a tight offset/limit window. With several files, grep across them and read only "
    "the few relevant lines from each — opening whole files overflows the context.\n"
    "- For a task that spans MANY files, work ONE FILE AT A TIME, end to end: read that file, make "
    "its edit (and create any file it needs), then MOVE ON to the next — do NOT read all the files "
    "first. Reading everything up front before any edit fills the context and you run out before "
    "writing anything. After each file is done, go straight to the next until every file is handled.\n"
    "- Write COMPLETE, coherent files and edits. Do NOT split one file into many tiny append "
    "steps — that produces broken, half-finished output (e.g. a stylesheet written in fragments "
    "looks wrong). When you change something like a theme or a component, rewrite the whole "
    "relevant block at once so the result is consistent.\n"
    "- ACT, don't narrate, and KEEP GOING until the task is actually DONE: the moment you know what "
    "to change, make the change with a tool call. Do not stop to explain or summarize mid-task, and "
    "do NOT declare the task finished until every required change has actually been applied and "
    "verified. Output plain text ONLY to ask the user a question or to report you have FULLY "
    "finished.\n"
    "- APPLY your fix — never end mid-analysis or merely DESCRIBE the change. The moment you've found "
    "the bug, make the edit with the edit/write tool. A clear diagnosis without an applied edit is a "
    "FAILURE.\n"
    "- Use ABSOLUTE paths in tool arguments: a file or directory path must start with '/' "
    "(e.g. /home/you/project/x), NEVER a bare relative path like 'home/you/...' — a missing leading "
    "slash makes the tool look in the wrong place and find nothing.\n"
    "- WRITE every file with the write/edit tool; do NOT paste a file's contents as a code block in "
    "your reply and consider it created — pasted code is NOT a saved file. Do EVERY step the task asks "
    "for (e.g. create the venv, install the dependencies, write EACH file it needs) — don't skip setup. "
    "After writing a Python file, run `python -m py_compile <file>` and fix any SyntaxError before you "
    "move on. A program meant to RUN (a web server, a CLI) must include the entry point that actually "
    "STARTS it (e.g. `if __name__ == \"__main__\": app.run(...)`); a file that only defines things but "
    "never starts compiles fine yet does nothing — re-read the end of the file to confirm it's there.\n"
    "- VERIFY proportionally, and do NOT get stuck testing. For something you can run directly in one "
    "step (a script, a function, a CLI), run it. But for a LONG-RUNNING SERVER you cannot reliably "
    "start-and-curl in a single shell step — backgrounding it and its startup timing make that flaky — "
    "do NOT loop trying to launch it. Instead verify by (a) confirming the file compiles "
    "(python -m py_compile) and (b) re-reading the EXACT lines you changed to confirm the logic is "
    "right: correct API calls, every DB connection opened-used-closed in order (and no cursor used "
    "after close), imports present, tables created. A clean compile plus a careful read of the changed "
    "code IS acceptable verification for a web server. A half-applied, never-applied, or never-tested "
    "edit is NOT — but spending the whole turn failing to start a server is also a failure: make the "
    "edit, sanity-check it, and finish."
)

from app.database import get_db
from app.utils import lb_auth
from app.models import User
from app.services import settings_store
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
    # A peer node, proven by the shared secret once `lb_shared_secret` is set. The bare header
    # alone is settable by any caller — see app/utils/lb_auth.py.
    if request:
        if lb_auth.is_internal(request):
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


# ============== Shared Handlers ==============

async def _handle_chat_completions(request: ChatCompletionRequest, db: Session, skip_load_balancer: bool = False):
    """Handle chat completions request
    
    Args:
        request: Chat completion request
        db: Database session
        skip_load_balancer: If True, skip load balancing (used to prevent loops when called from another instance)
    """
    from app.services.load_balancer import LoadBalancer, parse_server_urls

    # Check for load balancer first (unless explicitly skipped to prevent loops)
    # Load balancer ONLY uses what's configured in admin UI - round-robin between all configured servers
    settings = settings_store.all_settings()

    # Tool/agentic requests use the configured agentic model (`llm_tools_model`) when the client
    # didn't ask for a specific real model — so opencode/aider get the coding model while plain
    # chat stays on the default. Generic + admin-configurable; blank setting → no change. Both the
    # LB hop and local inference resolve request.model via resolve_model_path (missing → default).
    if request.tools and not skip_load_balancer \
            and (request.model or "").strip().lower() in ("", "native", "default"):
        _tools_model = (settings.get("llm_tools_model", "") or "").strip()
        if _tools_model:
            request.model = _tools_model

    chat_server_urls = settings.get("chat_server_urls", "")
    # Parse server URLs from admin UI - use all servers for round-robin (including self if configured)
    servers = parse_server_urls(chat_server_urls, exclude_self=False) if not skip_load_balancer and chat_server_urls else []

    # Convert messages to dict format. Preserve the tool-calling fields: tool_calls on assistant
    # turns and tool_call_id/name on tool turns. Dropping them blinds the model to its OWN prior
    # tool calls, so an agent (opencode) never sees that it already wrote/read a file and re-issues
    # the same call every turn -> infinite write->read->write loop.
    messages = []
    for m in request.messages:
        md = {"role": m.role, "content": m.content}
        if getattr(m, "tool_calls", None):
            md["tool_calls"] = m.tool_calls
        if getattr(m, "tool_call_id", None):
            md["tool_call_id"] = m.tool_call_id
        if getattr(m, "name", None):
            md["name"] = m.name
        messages.append(md)

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

    # Server-side coding guidance: when a request carries tools (agentic coding clients like
    # opencode), append a short small-context discipline note to the system prompt so large-file
    # editing behaves WITHOUT each client maintaining its own AGENTS.md. Generic (any tool-bearing
    # client), admin-configurable (tool_guidance_text), toggleable (tool_guidance_enabled). Skip on
    # LB hops so it isn't injected twice.
    if request.tools and not skip_load_balancer \
            and settings.get("tool_guidance_enabled", "true").lower() == "true":
        guidance = (settings.get("tool_guidance_text", "") or "").strip() or _DEFAULT_TOOL_GUIDANCE
        sys0 = messages[0] if messages and messages[0].get("role") == "system" else None
        sys_content = sys0.get("content") if sys0 else None
        if isinstance(sys_content, str):
            sys0["content"] = sys_content.rstrip() + "\n\n" + guidance
        elif isinstance(sys_content, list):
            # OpenAI content-parts form: append guidance as another text part rather than crash.
            sys0["content"] = sys_content + [{"type": "text", "text": guidance}]
        else:
            # No leading system message (or null content) -> add one.
            messages.insert(0, {"role": "system", "content": guidance})

    # Ensure messages alternate user/assistant properly (prevents "roles must alternate" errors).
    # Must run BEFORE inject_no_think so /no_think is appended to the final merged user turn,
    # not buried mid-string inside a merged consecutive-user-message block.
    from app.services.chat_service import ChatService
    messages = ChatService._ensure_alternating_roles(messages)

    # Inject /no_think for Qwen3 thinking models — but ONLY for direct external API calls
    # (skip_load_balancer=False). Telegram messages arrive here via chat_service → load_balancer
    # with skip_load_balancer=True so they are excluded. External bots (e.g. Posterchan bot)
    # call this endpoint directly with skip_load_balancer=False and need thinking suppressed:
    # without /no_think the model reasons about extreme personality prompts and refuses to engage.
    # NOTE: never inject /no_think for tool/agentic requests - coding agents (opencode) need
    # the model to reason about which tools to call, and our plain-chatml/Hermes path doesn't
    # strip the token, so it would leak as literal text (the model created /tmp/no_think).
    _llm_path = settings.get("llm_model_path", "").lower()
    if "qwen3" in _llm_path and not skip_load_balancer and not request.tools:
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
                                messages[_last_user_idx]["content"] = _SS.build_grounded_message(
                                    "", _url_context,
                                    instruction="Write a single concise paragraph summarizing the above article.")
                            else:
                                # A specific question — but the CONTENT still comes first, or the
                                # model answers it before reading (see SearchService.GROUNDING_NOTE).
                                messages[_last_user_idx]["content"] = _SS.build_grounded_message(
                                    _user_content, _url_context)

                            logger.info(f"[OPENAI-API] Injected {len(_url_context)} chars of URL context (summarize={_is_summarize_req})")
        except Exception as _url_err:
            logger.warning(f"[OPENAI-API] URL fetch failed, continuing without context: {_url_err}")

    # Build kwargs
    temperature = request.temperature if request.temperature is not None else float(settings.get("ollama_temperature", "0.2"))
    top_p = request.top_p if request.top_p is not None else float(settings.get("ollama_top_p", "0.9"))
    server_num_predict = int(settings.get("ollama_num_predict", "2048"))
    # CAP the client's max_tokens at the server limit (don't floor it - the old max() forced
    # every generation to ~32k). opencode sends max_tokens=32000, which makes a slow model
    # ramble for 100s+ and blow past opencode's ~125s request timeout -> retries. Capping at
    # server_num_predict (e.g. 4096) keeps each step bounded and under the client timeout.
    #
    # Tool requests are the exception: an agentic coding client (opencode) emits a whole-file
    # `write` as ONE tool call, so the low chat cap truncates large files mid-function. Use a
    # separate, higher cap when the request carries tools. Generic - keys on "has tools", not on
    # any specific client.
    if request.tools:
        try:
            predict_limit = int((settings.get("ollama_tool_num_predict", "16384") or "16384").strip())
        except (TypeError, ValueError):
            predict_limit = 16384
    else:
        predict_limit = server_num_predict
    max_tokens = min(request.max_tokens, predict_limit) if request.max_tokens is not None else predict_limit

    # Use load balancer if configured - picks server round-robin, uses local inference for "self" URLs
    # Skip if explicitly requested (to prevent loops when called from another posterchanai instance)
    if servers and not skip_load_balancer:
        from app.services.load_balancer import get_healthy_server, is_self_url, NoHealthyServersError

        # Server-to-server requests don't need authentication
        try:
            # Pass full server list to LoadBalancer - it will handle round-robin internally
            # This ensures proper load balancing across all servers
            timeout = int(settings.get("ollama_timeout", "300000")) / 1000
            # Forward the client-requested model when it names a real local .gguf, so the
            # remote node loads it; otherwise use the admin-configured default. This lets an
            # API client (opencode) pick a model per call while the web UI stays on default.
            llm_path = settings.get("llm_model_path", "")
            default_model = os.path.basename(llm_path) if llm_path else "default"
            req_model = (request.model or "").strip()
            models_dir = os.path.dirname(llm_path) if llm_path else ""
            if req_model and req_model.lower() not in ("native", "default") and models_dir \
                    and os.path.isfile(os.path.join(models_dir, os.path.basename(req_model))):
                model = os.path.basename(req_model)
            else:
                model = default_model
            logger.info(f"[OPENAI API] LoadBalancer model={model!r} (requested={request.model!r})")
            load_balancer = LoadBalancer(servers, timeout=timeout, model=model)

            try:
                if request.stream:
                    # Create a wrapper that catches NoHealthyServersError and re-raises it
                    # so the outer handler can catch it and fall back to local
                    lb_stream = load_balancer.chat_stream(
                        messages=messages,
                        temperature=temperature,
                        top_p=top_p,
                        max_tokens=max_tokens,
                        tools=request.tools,
                        tool_choice=request.tool_choice
                    )

                    # Wrap generator - if NoHealthyServersError is raised, stop silently
                    # The outer exception handler will catch it when StreamingResponse fails
                    # and fall back to local inference. Bypass the think-tag filter for tool
                    # requests (it buffers/reorders content and corrupts tool_call streams).
                    async def safe_stream():
                        try:
                            _src = lb_stream if request.tools else filter_thinking_stream(lb_stream)
                            async for chunk in _src:
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
                        max_tokens=max_tokens,
                        tools=request.tools,
                        tool_choice=request.tool_choice
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
        kwargs["max_tokens"] = min(request.max_tokens, server_num_predict)  # cap, don't floor
    if request.stop is not None:
        kwargs["stop"] = request.stop
    # Forward OpenAI tool definitions to the backend (generic function-calling support;
    # the backend decides how to surface them to the model). Task-agnostic pass-through.
    if request.tools:
        kwargs["tools"] = request.tools
    if request.tool_choice is not None:
        kwargs["tool_choice"] = request.tool_choice

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
            # The think-tag filter buffers content to detect <think> blocks, which reorders
            # and mangles tool-call streams. Bypass it for tool requests so tool_calls and
            # finish_reason reach the client in order.
            out_stream = stream if request.tools else filter_thinking_stream(stream)
            return StreamingResponse(
                out_stream,
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
    setting = settings_store.get("ollama_num_ctx")
    ctx = int(setting) if setting and str(setting).isdigit() else 4096
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
