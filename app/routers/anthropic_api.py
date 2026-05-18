"""
Anthropic-compatible /v1/messages endpoint for Claude Code CLI.
Set ANTHROPIC_BASE_URL=http://<host>:<port> in your environment.
"""
import json
import logging
import os
import re
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Setting
from app.routers.openai_api import verify_api_key
from app.services.inference_factory import get_inference_service, prepare_vram_for_llm
from app.services.text_utils import inject_no_think, strip_thinking_tags

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Anthropic API"])

_TOOL_CALL_RE = re.compile(r'<tool_call>\s*(.*?)\s*</tool_call>', re.DOTALL | re.IGNORECASE)
_THINK_RE = re.compile(r'<think(?:ing)?>(.*?)</think(?:ing)?>', re.DOTALL | re.IGNORECASE)


# ── Pydantic models ───────────────────────────────────────────────────────────

class AnthropicMessage(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]]]


class MessagesRequest(BaseModel):
    model: str
    messages: List[AnthropicMessage]
    max_tokens: int = 4096
    system: Optional[Union[str, List[Dict[str, Any]]]] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    stream: bool = False
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    thinking: Optional[Any] = None
    metadata: Optional[Dict[str, Any]] = None


# ── Format helpers ────────────────────────────────────────────────────────────

def _system_text(system) -> str:
    if not system:
        return ""
    if isinstance(system, str):
        return system
    parts = []
    for block in system:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def _tools_prompt(tools: list) -> str:
    """Format tools as a compact XML block the model understands."""
    if not tools:
        return ""
    entries = []
    for t in tools:
        name = t.get("name", "")
        desc = t.get("description", "")
        schema = t.get("input_schema", t.get("parameters", {}))
        props = schema.get("properties", {})
        required = schema.get("required", [])
        param_lines = []
        for pname, pdef in props.items():
            req = " (required)" if pname in required else ""
            pdesc = pdef.get("description", "")
            param_lines.append(f"  {pname}{req}: {pdesc}")
        params_str = "\n".join(param_lines) if param_lines else "  (no parameters)"
        entries.append(f"### {name}\n{desc}\nParameters:\n{params_str}")
    return "<tools>\n" + "\n\n".join(entries) + "\n</tools>"


def _content_to_text(content) -> str:
    """Flatten Anthropic content (str or list of blocks) to plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        t = block.get("type", "")
        if t == "text":
            parts.append(block.get("text", ""))
        elif t == "tool_use":
            name = block.get("name", "")
            inp = block.get("input", {})
            parts.append(f'<tool_call>\n{json.dumps({"name": name, "arguments": inp})}\n</tool_call>')
        elif t == "tool_result":
            result_content = block.get("content", "")
            if isinstance(result_content, list):
                result_content = "\n".join(
                    b.get("text", "") for b in result_content if isinstance(b, dict) and b.get("type") == "text"
                )
            parts.append(str(result_content))
    return "\n".join(parts)


def _build_model_messages(request: MessagesRequest) -> list:
    """Convert Anthropic messages to plain role/content dicts for the local model."""
    sys_text = _system_text(request.system)
    tools_text = _tools_prompt(request.tools or [])
    if tools_text:
        sys_text = (sys_text + "\n\n" + tools_text).strip()

    result = []
    if sys_text:
        result.append({"role": "system", "content": sys_text})

    for msg in request.messages:
        role = msg.role
        content = msg.content

        if role == "user":
            # tool_result blocks from Claude Code come as user messages with list content
            text = _content_to_text(content)
            if text.strip():
                result.append({"role": "user", "content": text})
        elif role == "assistant":
            text = _content_to_text(content)
            if text.strip():
                result.append({"role": "assistant", "content": text})

    return result


def _parse_tool_calls(text: str):
    """Extract tool calls from model text. Returns (clean_text, tool_calls_list)."""
    tool_calls = []
    for m in _TOOL_CALL_RE.finditer(text):
        raw = m.group(1).strip()
        try:
            parsed = json.loads(raw)
        except Exception:
            try:
                from app.services.text_utils import strip_thinking_tags as _s
                parsed = json.loads(re.sub(r'[\x00-\x1f]', ' ', raw))
            except Exception:
                continue
        name = parsed.get("name", "")
        arguments = parsed.get("arguments", {})
        if not name:
            continue
        tool_calls.append({
            "id": f"toolu_{uuid.uuid4().hex[:24]}",
            "type": "tool_use",
            "name": name,
            "input": arguments if isinstance(arguments, dict) else {},
        })
    clean = _TOOL_CALL_RE.sub("", text).strip()
    return clean, tool_calls


def _strip_thinking(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


# ── Streaming helpers ─────────────────────────────────────────────────────────

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _stream_response(text: str, tool_calls: list, input_tokens: int = 0, output_tokens: int = 0) -> AsyncGenerator[str, None]:
    """Yield Anthropic SSE events for a completed model response."""
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    stop_reason = "tool_use" if tool_calls else "end_turn"
    model_name = "local"

    yield _sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "model": model_name, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 0},
        },
    })

    block_idx = 0

    # Text block (if any)
    if text:
        yield _sse("content_block_start", {
            "type": "content_block_start", "index": block_idx,
            "content_block": {"type": "text", "text": ""},
        })
        # Stream text in chunks
        chunk_size = 64
        for i in range(0, len(text), chunk_size):
            yield _sse("content_block_delta", {
                "type": "content_block_delta", "index": block_idx,
                "delta": {"type": "text_delta", "text": text[i:i+chunk_size]},
            })
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": block_idx})
        block_idx += 1

    # Tool use blocks
    for tc in tool_calls:
        yield _sse("content_block_start", {
            "type": "content_block_start", "index": block_idx,
            "content_block": {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": {}},
        })
        args_str = json.dumps(tc["input"])
        chunk_size = 64
        for i in range(0, len(args_str), chunk_size):
            yield _sse("content_block_delta", {
                "type": "content_block_delta", "index": block_idx,
                "delta": {"type": "input_json_delta", "partial_json": args_str[i:i+chunk_size]},
            })
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": block_idx})
        block_idx += 1

    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    })
    yield _sse("message_stop", {"type": "message_stop"})


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/v1/messages")
async def messages(
    request: Request,
    body: MessagesRequest,
    db: Session = Depends(get_db),
    _auth=Depends(verify_api_key),
):
    settings = {s.key: s.value for s in db.query(Setting).all()}

    messages_for_model = _build_model_messages(body)

    # Inject /no_think for Qwen3 models
    llm_path = settings.get("llm_model_path", "").lower()
    if "qwen3" in llm_path:
        messages_for_model = inject_no_think(messages_for_model)

    max_tokens = body.max_tokens or 4096
    temperature = body.temperature if body.temperature is not None else 0.0
    top_p = body.top_p

    kwargs = {"temperature": temperature, "max_tokens": max_tokens}
    if top_p is not None:
        kwargs["top_p"] = top_p

    # Try load balancer first
    chat_server_urls = settings.get("chat_server_urls", "")
    from app.services.load_balancer import LoadBalancer, parse_server_urls, NoHealthyServersError
    servers = parse_server_urls(chat_server_urls, exclude_self=False) if chat_server_urls else []

    full_text = None
    if servers:
        try:
            lb_model = os.path.basename(settings.get("llm_model_path", "")) or "default"
            timeout = int(settings.get("ollama_timeout", "300000")) / 1000
            lb = LoadBalancer(servers, timeout=timeout, model=lb_model)
            result = await lb.chat(messages=messages_for_model, **kwargs)
            if "error" not in result and result.get("choices"):
                raw = result["choices"][0].get("message", {}).get("content", "") or ""
                full_text = _strip_thinking(strip_thinking_tags(raw))
        except Exception as e:
            logger.warning(f"[ANTHR] Load balancer failed: {e}, falling back to local")

    if full_text is None:
        prepare_vram_for_llm(db)
        service = get_inference_service(db)
        result = await service.chat_completion(
            messages=messages_for_model, model=body.model, **kwargs
        )
        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"].get("message", "Inference error"))
        raw = result.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        full_text = _strip_thinking(strip_thinking_tags(raw))

    clean_text, tool_calls = _parse_tool_calls(full_text)

    # Rewrite Write to sudo for system paths
    tool_calls = _rewrite_tool_calls(tool_calls)

    usage = result.get("usage", {}) if isinstance(result, dict) else {}
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)

    if body.stream:
        return StreamingResponse(
            _stream_response(clean_text, tool_calls, input_tokens, output_tokens),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    # Non-streaming response
    content_blocks = []
    if clean_text:
        content_blocks.append({"type": "text", "text": clean_text})
    for tc in tool_calls:
        content_blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]})

    stop_reason = "tool_use" if tool_calls else "end_turn"
    return JSONResponse({
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": body.model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    })


def _rewrite_tool_calls(tool_calls: list) -> list:
    """Rewrite Write calls to system paths → Bash(sudo tee)."""
    import base64, shlex
    result = []
    system_prefixes = ("/etc/", "/usr/", "/var/", "/run/", "/srv/", "/boot/")
    for tc in tool_calls:
        if tc.get("name") == "Write":
            inp = tc.get("input", {})
            fp = inp.get("file_path") or inp.get("filePath") or inp.get("path") or ""
            if fp and any(fp.startswith(p) for p in system_prefixes):
                content = inp.get("content", "")
                b64 = base64.b64encode(content.encode()).decode()
                dir_path = "/".join(fp.split("/")[:-1]) or "."
                cmd = (f"sudo mkdir -p {shlex.quote(dir_path)} && "
                       f"printf '%s' {shlex.quote(b64)} | base64 -d | sudo tee {shlex.quote(fp)} > /dev/null")
                result.append({
                    "id": tc["id"], "type": "tool_use", "name": "Bash",
                    "input": {"command": cmd},
                })
                logger.info(f"[ANTHR] Write({fp}) → Bash(sudo tee)")
                continue
        result.append(tc)
    return result
