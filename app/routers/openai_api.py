"""
OpenAI-compatible API router for LLM backend.
Supports both native llama-cpp-python and Ollama backends.
Provides both /v1/* and /api/* endpoints for maximum compatibility.
"""
import ast as _ast
import base64 as _b64
import json
import logging
import os
import re
import shlex as _shlex
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, AsyncGenerator
from fastapi import APIRouter, Depends, Header, HTTPException, Request
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
from app.services.inference_factory import get_inference_service, prepare_vram_for_llm
from app.services.text_utils import strip_thinking_tags
from app.utils.tool_utils import (
    TOOL_CALL_OPEN as _TOOL_CALL_OPEN,
    TOOL_CALL_CLOSE as _TOOL_CALL_CLOSE,
    TOOL_ONLY_RE as _TOOL_ONLY_RE,
    HOME_DIR as _HOME_DIR,
    NEVER_WRITE_EXACT as _NEVER_WRITE_EXACT,
    NEVER_WRITE_PREFIXES_BLOCK as _NEVER_WRITE_PREFIXES_BLOCK,
    MAX_TOOL_ITERATIONS as _MAX_TOOL_ITERATIONS,
    needs_sudo as _needs_sudo,
    fix_redirects as _fix_redirects,
    compact_schema as _compact_schema,
    resolve_model_path as _resolve_model_path,
    build_tools_system_prompt as _build_tools_system_prompt,
    repair_tool_json as _repair_tool_json,
    truncate_tool_output as _truncate_tool_output,
    extract_cwd_from_system as _extract_cwd_from_system,
    sanitize_json_control_chars as _sanitize_json_control_chars,
)


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


# ── OpenAI-compatible tool calling (text-based) ───────────────────────────────
# Constants/helpers imported from app.utils.tool_utils:
#   _TOOL_CALL_OPEN, _TOOL_CALL_CLOSE, _TOOL_ONLY_RE, _HOME_DIR,
#   _NEVER_WRITE_EXACT, _NEVER_WRITE_PREFIXES_BLOCK, _MAX_TOOL_ITERATIONS,
#   _needs_sudo, _fix_redirects, _compact_schema, _resolve_model_path,
#   _build_tools_system_prompt

# Strip complete <tool_call>...</tool_call> blocks (used in non-tool streaming path)
_TOOL_CALL_STRIP_RE = re.compile(r'<tool_call>.*?</tool_call>', re.DOTALL | re.IGNORECASE)
_TOOL_RESULT_OPEN = "<tool_response>\n"
_TOOL_RESULT_CLOSE = "\n</tool_response>"

_KA_EVENT = (
    'data: {"id":"ka","object":"chat.completion.chunk","created":0,'
    '"model":"","choices":[{"index":0,"delta":{},"finish_reason":null}]}\n\n'
)

_FILTER_FALLBACK = "I apologize, I wasn't able to generate a proper response. Please try again."
_THINK_RESIDUAL_RE = re.compile(r'</?(?:think(?:ing)?|thought|reasoning|internal[_-]?thought)[^>]*>', re.IGNORECASE)
_THINK_END_RE = re.compile(r'</(?:think(?:ing)?|thought|reasoning|internal[_-]?thought)>', re.IGNORECASE)
_THINK_START_RE = re.compile(r'<(?:think(?:ing)?|thought|reasoning|internal[_-]?thought)', re.IGNORECASE)
_TOOL_RESULT_STRIP_RE = re.compile(r'<tool_result>.*?</tool_result>', re.DOTALL | re.IGNORECASE)

_TOOL_NAME_ALIASES_OAI: Dict[str, str] = {
    "write_file": "Write", "writefile": "Write", "write": "Write",
    "bash": "Bash", "shell": "Bash", "run_command": "Bash",
    "run_bash": "Bash", "run_shell": "Bash", "execute": "Bash",
    "exec_command": "Bash", "run": "Bash",
    "read_file": "Read", "readfile": "Read", "read": "Read",
}

def _normalize_tool_name_oai(name: str) -> str:
    return _TOOL_NAME_ALIASES_OAI.get(name.lower(), name)

def _sudo_rewrite_bash(cmd: str) -> str:
    """Fix shell redirections that bypass sudo for paths outside the user's home."""
    new_cmd = _fix_redirects(cmd)
    if new_cmd != cmd:
        logger.info("[TOOL-REWRITE] Bash: fixed redirect (→ sudo tee)")
    return new_cmd


def _rewrite_privileged_tool_calls(tool_calls: list, cwd: str = "") -> list:
    """Rewrite Write(<system-path>) → Bash(sudo tee); fix shell redirects to system paths."""
    _SYSTEM_PREFIXES = ("/etc/", "/usr/", "/var/", "/opt/", "/run/", "/srv/", "/tmp/")
    result = []
    for tc in tool_calls:
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, TypeError):
            result.append(tc)
            continue

        if name == "Write":
            fp_key = "filePath" if "filePath" in args else "file_path"
            fp = os.path.expanduser(args.get(fp_key, ""))
            # Resolve relative paths against cwd so Write lands in the project dir.
            if fp and not os.path.isabs(fp) and cwd:
                fp = os.path.join(cwd, fp)
                logger.info(f"[TOOL-REWRITE] Resolved relative Write path → {fp}")
            # Redirect out-of-cwd paths back into cwd (model hallucinated wrong dir).
            if fp and cwd and os.path.isabs(fp) and not fp.startswith(cwd + "/") and not any(fp.startswith(p) for p in ("/etc/", "/usr/", "/var/", "/opt/", "/run/", "/srv/")):
                basename = os.path.basename(fp)
                new_fp = os.path.join(cwd, basename)
                logger.warning(f"[TOOL-REDIRECT] Write({fp}) outside cwd {cwd!r} → {new_fp}")
                fp = new_fp
            if fp in _NEVER_WRITE_EXACT or any(fp.startswith(p) for p in _NEVER_WRITE_PREFIXES_BLOCK):
                logger.warning(f"[TOOL-BLOCK] Blocked Write to critical file: {fp}")
                cmd = f'echo "ERROR: Writing to {fp} is blocked — this is a critical system file." >&2; false'
                result.append({
                    "id": tc["id"], "type": "function",
                    "function": {"name": "Bash", "arguments": json.dumps({"command": cmd})},
                })
                continue
            if not _needs_sudo(fp):
                content_len = len(args.get("content", ""))
                content_preview = args.get("content", "")[:60].replace("\n", "\\n")
                logger.info(f"[WRITE] {fp!r} | content_len={content_len} | preview={content_preview!r}")
                result.append({
                    "id": tc["id"], "type": "function",
                    "function": {"name": "Write", "arguments": json.dumps({**args, fp_key: fp})},
                })
                continue
            # System path: rewrite Write → Bash(sudo tee) so we can write as root.
            content = args.get("content", "")
            dir_path = "/".join(fp.split("/")[:-1]) or "."
            b64 = _b64.b64encode(content.encode("utf-8")).decode("ascii")
            cmd = (
                f"sudo mkdir -p {_shlex.quote(dir_path)} && "
                f"printf '%s' {_shlex.quote(b64)} | base64 -d | sudo tee {_shlex.quote(fp)} > /dev/null"
            )
            logger.info(f"[TOOL-REWRITE] Write({fp}) → Bash(sudo tee)")
            result.append({
                "id": tc["id"], "type": "function",
                "function": {"name": "Bash", "arguments": json.dumps({"command": cmd})},
            })
            continue

        elif name == "Bash":
            cmd = args.get("command", "")
            new_cmd = _sudo_rewrite_bash(cmd)
            # Keep description (OpenCode requires it) but strip timeout — model emits
            # timeout in seconds but OpenCode interprets it as milliseconds (60 → 60ms).
            clean_args: dict = {"command": new_cmd}
            if args.get("description"):
                clean_args["description"] = args["description"]
            result.append({
                "id": tc["id"], "type": "function",
                "function": {"name": name, "arguments": json.dumps(clean_args)},
            })
            continue

        result.append(tc)
    return result


def _openai_tools_to_system_prompt(tools: List[Any], system_text: str = "") -> str:
    """Format tools in Qwen3's native <tools> format with stripped schemas to save tokens."""
    entries = []
    for t in tools:
        fn = t.get("function", t) if t.get("type") == "function" else t
        desc = fn.get("description", "").split("\n")[0][:150]
        entries.append(json.dumps({
            "type": "function",
            "function": {
                "name": fn.get("name", ""),
                "description": desc,
                "parameters": _compact_schema(fn.get("parameters", {})),
            }
        }))
    cwd = _extract_cwd_from_system(system_text)
    return _build_tools_system_prompt(entries, working_dir=cwd)


def _openai_parse_tool_calls(text: str):
    """
    Extract <tool_call>…</tool_call> blocks from text.
    Returns (remaining_text, openai_tool_calls_list).
    """
    tool_calls: List[Any] = []
    pattern = re.compile(
        re.escape(_TOOL_CALL_OPEN) + r"\s*(.*?)\s*" + re.escape(_TOOL_CALL_CLOSE),
        re.DOTALL,
    )
    clean_text = text
    for match in pattern.finditer(text):
        raw = match.group(1)
        parsed_tc = None
        try:
            data = json.loads(raw)
            args = data.get("arguments", data.get("input", {}))
            parsed_tc = {
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {
                    "name": _normalize_tool_name_oai(data.get("name", "")),
                    "arguments": json.dumps(args),
                },
            }
        except json.JSONDecodeError:
            fixed = raw.replace("\\'", "'")
            try:
                data = json.loads(fixed)
                args = data.get("arguments", data.get("input", {}))
                parsed_tc = {
                    "id": f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": _normalize_tool_name_oai(data.get("name", "")),
                        "arguments": json.dumps(args),
                    },
                }
            except json.JSONDecodeError as e:
                try:
                    data = json.loads(_sanitize_json_control_chars(raw))
                    args = data.get("arguments", data.get("input", {}))
                    if isinstance(args, str):
                        args = json.loads(args)
                    parsed_tc = {
                        "id": f"call_{uuid.uuid4().hex[:12]}",
                        "type": "function",
                        "function": {
                            "name": _normalize_tool_name_oai(data.get("name", "")),
                            "arguments": json.dumps(args),
                        },
                    }
                    logger.info(f"[TOOL-PARSE] Repaired via control-char sanitizer: name={data.get('name')!r}")
                except json.JSONDecodeError:
                    pass

                if parsed_tc is None:
                    # Repair pass 2b: raw_decode (extra data after }}) + brace completion (truncated JSON)
                    _sanitized = _sanitize_json_control_chars(raw)
                    for _candidate in (_sanitized, _sanitized + "}", _sanitized + "}}"):
                        try:
                            data, _ = json.JSONDecoder().raw_decode(_candidate.strip())
                            args = data.get("arguments", data.get("input", {}))
                            if isinstance(args, str):
                                args = json.loads(args)
                            parsed_tc = {
                                "id": f"call_{uuid.uuid4().hex[:12]}",
                                "type": "function",
                                "function": {
                                    "name": _normalize_tool_name_oai(data.get("name", "")),
                                    "arguments": json.dumps(args),
                                },
                            }
                            logger.info(f"[TOOL-PARSE] Repaired via brace-completion: name={data.get('name')!r}")
                            break
                        except (json.JSONDecodeError, ValueError):
                            pass

                if parsed_tc is None:
                    # Repair pass 2c: ast.literal_eval for single-quoted JSON strings.
                    # Also tries brace-completion (+ "}" or + "}}") to fix truncated dicts.
                    for _py_suffix in ("", "}", "}}"):
                        try:
                            _py_src = re.sub(r'\bnull\b', 'None', raw + _py_suffix)
                            _py_src = re.sub(r'\btrue\b', 'True', _py_src)
                            _py_src = re.sub(r'\bfalse\b', 'False', _py_src)
                            data = _ast.literal_eval(_py_src)
                            if isinstance(data, dict):
                                args = data.get("arguments", data.get("input", {}))
                                if isinstance(args, str):
                                    args = json.loads(args)
                                parsed_tc = {
                                    "id": f"call_{uuid.uuid4().hex[:12]}",
                                    "type": "function",
                                    "function": {
                                        "name": _normalize_tool_name_oai(data.get("name", "")),
                                        "arguments": json.dumps(args),
                                    },
                                }
                                logger.info(f"[TOOL-PARSE] Repaired via ast.literal_eval{_py_suffix!r}: name={data.get('name')!r}")
                                break
                        except (ValueError, SyntaxError):
                            pass

                if parsed_tc is None:
                    # XML-format tool call (<tool>name</tool><input>{}</input>)
                    xml_name_m = re.search(r'<tool>\s*(\w+)\s*</tool>', raw, re.IGNORECASE)
                    xml_args_m = re.search(r'<(?:input|arguments)>\s*([\s\S]*?)\s*</(?:input|arguments)>', raw, re.IGNORECASE)
                    if xml_name_m and xml_args_m:
                        try:
                            xml_args = json.loads(_sanitize_json_control_chars(xml_args_m.group(1)))
                            xml_name = _normalize_tool_name_oai(xml_name_m.group(1))
                            parsed_tc = {
                                "id": f"call_{uuid.uuid4().hex[:12]}",
                                "type": "function",
                                "function": {"name": xml_name, "arguments": json.dumps(xml_args)},
                            }
                            logger.info(f"[TOOL-PARSE] Parsed XML-format tool call: name={xml_name!r}")
                        except json.JSONDecodeError:
                            pass

                if parsed_tc is None:
                    logger.warning(
                        f"[TOOL-PARSE] JSON parse failed: {e} | len={len(raw)} "
                        f"ends={raw[-40:]!r} | raw={raw[:120]!r}"
                    )
                    repaired = _repair_tool_json(raw)
                    if repaired:
                        logger.info(f"[TOOL-PARSE] Repaired tool call via greedy extract: name={repaired['name']!r}")
                        parsed_tc = {
                            "id": f"call_{uuid.uuid4().hex[:12]}",
                            "type": "function",
                            "function": {
                                "name": _normalize_tool_name_oai(repaired["name"]),
                                "arguments": json.dumps(repaired["arguments"]),
                            },
                        }
                    else:
                        logger.warning(
                            f"[TOOL-PARSE] Repair failed: len={len(raw)} "
                            f"ends={raw[-40:]!r}"
                        )

        if parsed_tc is not None:
            tool_calls.append(parsed_tc)
        clean_text = clean_text.replace(match.group(0), "")
    return clean_text.strip(), tool_calls


def _build_messages_with_tools(request_messages) -> List[Any]:
    """Convert ChatMessage list → plain dict list, encoding tool_calls/tool results as text."""
    # Build call_id → tool_name index so each result can identify which call it answers
    id_to_name: Dict[str, str] = {}
    for m in request_messages:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                tc_id = tc.get("id", "")
                tc_name = tc.get("function", {}).get("name", "")
                if tc_id and tc_name:
                    id_to_name[tc_id] = tc_name

    msgs = []
    for m in request_messages:
        role = m.role
        content = m.content or ""
        tool_calls = m.tool_calls
        tool_call_id = m.tool_call_id

        if role == "tool":
            display = content if content.strip() else "(success — no output)"
            tool_name = m.name or id_to_name.get(tool_call_id or "", "")
            prefix = f"[Result of {tool_name}]: " if tool_name else ""
            msgs.append({
                "role": "user",
                "content": f"{_TOOL_RESULT_OPEN}{prefix}{_truncate_tool_output(display)}{_TOOL_RESULT_CLOSE}",
            })
        elif role == "assistant" and tool_calls:
            tc_text = ""
            for tc in tool_calls:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    args = {}
                tc_text += (
                    _TOOL_CALL_OPEN
                    + json.dumps({"name": fn.get("name", ""), "arguments": args})
                    + _TOOL_CALL_CLOSE
                )
            msgs.append({"role": "assistant", "content": content + tc_text})
        else:
            msgs.append({"role": role, "content": content})
    return msgs


def _strip_completed_tool_cycles(messages: List[Dict]) -> List[Dict]:
    """Strip stale tool_use/tool_result pairs when the user starts a new request.

    Same purpose as the Anthropic-path version. In the OpenAI tool format, tool
    results are role="user" messages wrapped in <tool_response> tags, so detection
    differs from the Anthropic path (where they are role="tool").
    """
    def _is_tool_result(msg: Dict) -> bool:
        return (
            msg.get("role") == "user"
            and isinstance(msg.get("content"), str)
            and msg["content"].lstrip().startswith(_TOOL_RESULT_OPEN.strip())
        )

    last_msg = next((m for m in reversed(messages) if m.get("role") != "system"), None)
    if not last_msg or last_msg.get("role") != "user" or _is_tool_result(last_msg):
        return messages  # Mid-cycle or empty — leave untouched

    if not any(_is_tool_result(m) for m in messages):
        return messages  # No tool history — nothing to strip

    cleaned: List[Dict] = []
    for msg in messages:
        if _is_tool_result(msg):
            continue  # Drop tool result messages
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if isinstance(content, str):
                if _TOOL_ONLY_RE.match(content):
                    continue  # Drop assistant turns that contain only tool_call blocks
                # Mixed text+tool message: strip tool_call blocks to avoid orphaned references
                # that cause the model to re-run the previous task.
                stripped = _TOOL_CALL_STRIP_RE.sub('', content).strip()
                if stripped != content:
                    if not stripped:
                        continue
                    msg = dict(msg, content=stripped)
        cleaned.append(msg)

    # Collapse consecutive user messages (happens when no final text response was produced).
    result: List[Dict] = []
    for msg in cleaned:
        if msg.get("role") == "user" and result and result[-1].get("role") == "user":
            result[-1] = msg
        else:
            result.append(msg)
    return result


async def _stream_openai_with_tools(
    backend_stream: AsyncGenerator[str, None],
    cwd: str = "",
    has_prior_tools: bool = False,
) -> AsyncGenerator[str, None]:
    """Accumulate backend SSE stream, parse tool calls, emit OpenAI-format SSE."""
    accumulated = ""
    async for raw in backend_stream:
        if not raw.startswith("data: "):
            continue
        payload = raw[6:].strip()
        if payload == "[DONE]":
            break
        try:
            data = json.loads(payload)
            text = (data.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
            accumulated += text
        except json.JSONDecodeError:
            continue

    # Strip hallucinated <tool_result> blocks before parsing.
    accumulated = _TOOL_RESULT_STRIP_RE.sub('', accumulated)
    # Parse tool calls from full text (including any inside <think>) BEFORE stripping.
    # With open-think active, the model may place <tool_call> inside the thinking block.
    clean_text, tool_calls = _openai_parse_tool_calls(accumulated)
    clean_text = strip_thinking_tags(clean_text)
    if tool_calls and clean_text == _FILTER_FALLBACK:
        clean_text = ""
    if tool_calls:
        tool_calls = _rewrite_privileged_tool_calls(tool_calls, cwd=cwd)

    if not tool_calls:
        _acc_stripped = accumulated.strip()
        if _acc_stripped and len(_acc_stripped) < 400 and "\n\n" not in _acc_stripped and "<tool_call>" not in _acc_stripped and not has_prior_tools:
            logger.warning(f"[NO-TOOL-CALL-PREAMBLE] OAI stream: {len(_acc_stripped)}c: {_acc_stripped[:80]!r} — injecting retry signal")
            tool_calls = [{
                "id": f"call_retry_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {
                    "name": "Bash",
                    "arguments": json.dumps({"command": "echo 'ERROR: Call the tool now. Do not describe the task — use Write, Bash, or Read.'", "description": "Retry signal"}),
                },
            }]

    msg_id = f"chatcmpl_{uuid.uuid4().hex[:12]}"

    if tool_calls:
        yield f"data: {json.dumps({'id': msg_id, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': None}, 'finish_reason': None}]})}\n\n"
        for i, tc in enumerate(tool_calls):
            yield f"data: {json.dumps({'id': msg_id, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': i, 'id': tc['id'], 'type': 'function', 'function': {'name': tc['function']['name'], 'arguments': ''}}]}, 'finish_reason': None}]})}\n\n"
            yield f"data: {json.dumps({'id': msg_id, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': i, 'function': {'arguments': tc['function']['arguments']}}]}, 'finish_reason': None}]})}\n\n"
        yield f"data: {json.dumps({'id': msg_id, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]})}\n\n"
    else:
        text_out = clean_text if clean_text else strip_thinking_tags(accumulated)
        if text_out == _FILTER_FALLBACK:
            text_out = ""
        yield f"data: {json.dumps({'id': msg_id, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]})}\n\n"
        yield f"data: {json.dumps({'id': msg_id, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'content': text_out}, 'finish_reason': None}]})}\n\n"
        yield f"data: {json.dumps({'id': msg_id, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"

    yield "data: [DONE]\n\n"


async def filter_thinking_stream(stream: AsyncGenerator[str, None]) -> AsyncGenerator[str, None]:
    """Filter thinking tags and stray <tool_call> blocks from SSE stream.

    Thinking tags (<think>, <thinking>, etc.) are buffered and stripped.
    <tool_call>...</tool_call> blocks are suppressed in the passthrough phase
    (non-tool clients like opencode should never see raw tool XML).
    """
    buffer = ""       # accumulates content while inside a thinking section
    tool_buf = ""     # accumulates content while inside a stray <tool_call> block
    thinking_done = False
    chunk_count = 0

    async for chunk in stream:
        chunk_count += 1
        if not chunk.startswith("data: "):
            yield chunk
            continue

        data_str = chunk[6:].strip()
        if data_str == "[DONE]":
            if buffer:
                clean = strip_thinking_tags(buffer)
                clean = _TOOL_CALL_STRIP_RE.sub('', clean).strip()
                buffer = ""
                if clean and clean != _FILTER_FALLBACK:
                    yield f"data: {json.dumps({'choices': [{'delta': {'content': clean}}]})}\n\n"
            if tool_buf:
                leftover = _TOOL_CALL_STRIP_RE.sub('', tool_buf).strip()
                tool_buf = ""
                if leftover and leftover != _FILTER_FALLBACK:
                    yield f"data: {json.dumps({'choices': [{'delta': {'content': leftover}}]})}\n\n"
            logger.debug(f"filter_thinking_stream: received [DONE] after {chunk_count} chunks")
            yield "data: [DONE]\n\n"
            return

        try:
            data = json.loads(data_str)
            content = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
            if content:
                if not thinking_done:
                    buffer += content
                    match = _THINK_END_RE.search(buffer)
                    if match:
                        thinking_done = True
                        after_think = buffer[match.end():]
                        buffer = ""
                        if after_think.strip():
                            data["choices"][0]["delta"]["content"] = after_think
                            yield f"data: {json.dumps(data)}\n\n"
                    elif len(buffer) > 50 and not _THINK_START_RE.search(buffer):
                        # No <think> opener seen yet. If buffer already has </think>, the model
                        # omitted the opener — strip up to and including </think>, emit the rest.
                        end_m = _THINK_END_RE.search(buffer)
                        if end_m:
                            thinking_done = True
                            after_think = buffer[end_m.end():]
                            buffer = ""
                            if after_think.strip():
                                data["choices"][0]["delta"]["content"] = after_think
                                yield f"data: {json.dumps(data)}\n\n"
                        elif len(buffer) > 8000:
                            # Very large buffer with no </think> — safe to assume normal response.
                            # Emit in one chunk via strip_thinking_tags as a safety net.
                            thinking_done = True
                            clean = strip_thinking_tags(buffer)
                            buffer = ""
                            if clean and clean != _FILTER_FALLBACK:
                                data["choices"][0]["delta"]["content"] = clean
                                yield f"data: {json.dumps(data)}\n\n"
                        # else: keep buffering — </think> may arrive in a later chunk
                else:
                    # Passthrough: strip residual think tags, then handle any <tool_call> blocks
                    cleaned = _THINK_RESIDUAL_RE.sub('', content)

                    tool_buf += cleaned
                    # Remove complete <tool_call>...</tool_call> blocks
                    tool_buf = _TOOL_CALL_STRIP_RE.sub('', tool_buf)
                    open_pos = tool_buf.lower().find('<tool_call>')
                    if open_pos == -1:
                        # No pending tool_call — emit all buffered content
                        out = tool_buf
                        tool_buf = ""
                        if out:
                            data["choices"][0]["delta"]["content"] = out
                            yield f"data: {json.dumps(data)}\n\n"
                    elif open_pos > 0:
                        # Emit content before the opening tag, hold the rest
                        safe = tool_buf[:open_pos]
                        tool_buf = tool_buf[open_pos:]
                        if safe:
                            data["choices"][0]["delta"]["content"] = safe
                            yield f"data: {json.dumps(data)}\n\n"
                    # else: entire buffer is inside a tool_call block — suppress
            else:
                yield chunk
        except json.JSONDecodeError as e:
            logger.debug(f"filter_thinking_stream: JSON decode error on chunk {chunk_count}: {e}, passing through")
            yield chunk

    if chunk_count == 0:
        logger.warning("filter_thinking_stream: received 0 chunks from stream")
    elif buffer and not thinking_done:
        logger.debug(f"filter_thinking_stream: flushing remaining buffer (len={len(buffer)})")
        clean = strip_thinking_tags(buffer)
        clean = _TOOL_CALL_STRIP_RE.sub('', clean).strip()
        if clean and clean != _FILTER_FALLBACK:
            yield f"data: {json.dumps({'choices': [{'delta': {'content': clean}}]})}\n\n"
    elif tool_buf:
        leftover = _TOOL_CALL_STRIP_RE.sub('', tool_buf).strip()
        if leftover and leftover != _FILTER_FALLBACK:
            yield f"data: {json.dumps({'choices': [{'delta': {'content': leftover}}]})}\n\n"


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
            # Key is valid in DB but user lookup failed (session state issue) — allow request
            logger.debug(f"[OPENAI-API] X-API-Key valid in DB but user lookup failed, allowing request")
            return None
        # x_api_key was provided but not found — don't fall through to Authorization check
        # so a client sending both headers can't accidentally get blocked by Authorization path
        if not authorization:
            return None

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

    # Token did not match any user API key — allow as unauthenticated
    # (covers OAuth-format tokens from Claude Code and other external clients
    # that send a bearer token the local DB doesn't know about)
    logger.debug(f"[OPENAI-API] Bearer token not in DB, allowing as unauthenticated")
    return None


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
    from app.models import Setting
    from app.services.load_balancer import LoadBalancer, parse_server_urls, is_self_url

    # Check for load balancer first (unless explicitly skipped to prevent loops)
    # Load balancer ONLY uses what's configured in admin UI - round-robin between all configured servers
    settings = {s.key: s.value for s in db.query(Setting).all()}
    chat_server_urls = settings.get("chat_server_urls", "")
    # Parse server URLs; prefer remote (NAS, faster) over self (Arc, slower SYCL) so NAS
    # handles requests first and Arc is used only as overflow.
    servers = parse_server_urls(chat_server_urls, exclude_self=False) if not skip_load_balancer and chat_server_urls else []
    if servers:
        _remote = [s for s in servers if not is_self_url(s)]
        _local = [s for s in servers if is_self_url(s)]
        servers = _remote + _local

    has_tools = bool(request.tools)
    _oai_cwd = ""

    if has_tools:
        # Tool path: encode messages as text, prepend tool system prompt,
        # skip RAG / alternating-roles / no_think / load balancer.
        _sys_text = next((m.content for m in request.messages if m.role == "system" and isinstance(m.content, str)), "")
        _oai_cwd = _extract_cwd_from_system(_sys_text)
        logger.info(f"[CWD] Extracted cwd={_oai_cwd!r} from system prompt (first 200c: {_sys_text[:200]!r})")
        tool_system = _openai_tools_to_system_prompt(request.tools, system_text=_sys_text)
        messages = _build_messages_with_tools(request.messages)
        # Remove stale tool_use/tool_result pairs when user starts a new request.
        messages = _strip_completed_tool_cycles(messages)

        # Hard cap: too many tool iterations → synthetic stop instead of looping forever.
        # Count tool results on the original request (after _build_messages_with_tools, role="tool"
        # becomes role="user" with <tool_response> wrapper, so count on request.messages instead).
        tool_iters = sum(1 for m in request.messages if m.role == "tool")
        _has_prior_tools = tool_iters > 0
        if tool_iters >= _MAX_TOOL_ITERATIONS:
            logger.warning(f"[TOOL-LIMIT] Reached {tool_iters} tool iterations, forcing stop")
            from fastapi.responses import JSONResponse
            return JSONResponse(content={
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion",
                "created": int(__import__("time").time()),
                "model": request.model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": f"Tool iteration limit ({_MAX_TOOL_ITERATIONS}) reached. Review the results above and let me know how to proceed."}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 1, "total_tokens": 1},
            })

        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = tool_system + "\n\n" + messages[0]["content"]
        else:
            messages.insert(0, {"role": "system", "content": tool_system})
        # Inject /no_think for Qwen3 tool requests — propagates through LB to remote nodes
        # so they also disable open-think and emit <tool_call> outside <think> blocks
        _tl_user_idx = None
        for _i in range(len(messages) - 1, -1, -1):
            if messages[_i].get("role") == "user":
                _tl_user_idx = _i
                break
        if _tl_user_idx is not None:
            _tu = messages[_tl_user_idx]
            _tuc = _tu.get("content", "")
            _llm_path_lower = settings.get("llm_model_path", "").lower()
            if "qwen3" in _llm_path_lower and "/no_think" not in _tuc:
                messages[_tl_user_idx] = dict(_tu, content=_tuc.rstrip() + " /no_think")
        # Tool requests can be load-balanced — messages are already encoded as text,
        # and _stream_openai_with_tools on the originating server parses tool calls
        # from the text response. Remote nodes process it as a plain completion.
    else:
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

        # Append absolute-path note to existing system messages (e.g. aider) — no tools only.
        # aider uses Python open() which does not expand ~, so ~ paths create a literal ~ directory.
        if api_inject_system and not skip_load_balancer and has_system and not request.tools:
            _abs_note = (
                f"\n\nNOTE: Always use full absolute paths — never ~ or placeholder words like "
                f"'username' or 'user' in paths. The home directory is {_HOME_DIR}. "
                f"For example, user systemd units go in {_HOME_DIR}/.config/systemd/user/ — "
                f"NEVER /home/user/ or ~/."
            )
            if _HOME_DIR not in (messages[0].get("content") or ""):
                messages[0] = dict(messages[0], content=(messages[0].get("content") or "") + _abs_note)

        # Inject RAG context if enabled
        rag_enabled = settings.get("api_rag_enabled", "true").lower() == "true"
        rag_api_url = settings.get("rag_api_url", "").strip() or None
        if rag_enabled:
            messages = await inject_rag_context(messages, db, user_id=1, top_k=3, rag_api_url=rag_api_url)

        # Ensure messages alternate user/assistant properly (prevents "roles must alternate" errors)
        from app.services.chat_service import ChatService
        messages = ChatService._ensure_alternating_roles(messages)

        if has_system and not skip_load_balancer:
            _sys_preview = (messages[0].get("content", "") or "")[:120].replace("\n", " ")
            logger.info(f"[OPENAI API] has_system=True, sys_preview={repr(_sys_preview)}")

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
                        _failed_urls = []
                        MAX_CHARS = 2000
                        for _r in _fetched:
                            if _r.get("content") and not _r.get("error"):
                                _c = _r["content"][:MAX_CHARS]
                                _url_context += f"\n\n---\nContent from {_r['url']}:\nTitle: {_r['title']}\n\n{_c}\n---"
                            elif _r.get("error"):
                                logger.warning(f"[OPENAI-API] Failed to fetch {_r['url']}: {_r['error']}")
                                _failed_urls.append(_r['url'])

                        if not _url_context and _failed_urls:
                            # URL(s) present but none could be fetched — inject error so model
                            # doesn't hallucinate content it has never seen.
                            _text_without_urls = _user_content
                            for _u in _urls:
                                _text_without_urls = _text_without_urls.replace(_u, '').strip()
                            _failed_list = ", ".join(_failed_urls[:3])
                            if not _text_without_urls:
                                messages[_last_user_idx]["content"] = (
                                    f"The URL(s) could not be fetched ({_failed_list}). "
                                    "Inform the user that the content is unavailable and you cannot summarize it."
                                )
                            else:
                                messages[_last_user_idx]["content"] = (
                                    _user_content +
                                    f"\n\n(Note: Could not fetch URL content from {_failed_list} — do not invent content you have not seen.)"
                                )
                            logger.warning(f"[OPENAI-API] URL fetch failed for all URLs — injecting error notice to prevent hallucination")

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

    # For non-tool Qwen3 requests: inject /no_think so the model goes straight to the
    # response without generating a thinking block that could leak to the caller.
    # chat_service does the same thing. Tool requests already inject it separately.
    if not has_tools and not skip_load_balancer:
        _llm_path_lower = settings.get("llm_model_path", "").lower()
        if "qwen3" in _llm_path_lower:
            _last_u_idx = next((i for i in range(len(messages)-1, -1, -1) if messages[i].get("role") == "user"), None)
            if _last_u_idx is not None:
                _uc = messages[_last_u_idx].get("content", "")
                if isinstance(_uc, str) and "/no_think" not in _uc:
                    messages[_last_u_idx] = dict(messages[_last_u_idx], content=_uc.rstrip() + " /no_think")

    # Build kwargs
    temperature = request.temperature if request.temperature is not None else float(settings.get("ollama_temperature", "0.7"))
    top_p = request.top_p if request.top_p is not None else float(settings.get("ollama_top_p", "0.9"))
    server_num_predict = int(settings.get("ollama_num_predict", "2048"))
    max_tokens = max(request.max_tokens, server_num_predict) if request.max_tokens is not None else server_num_predict

    resolved_path = _resolve_model_path(request.model, settings)
    kwargs = {}
    if resolved_path:
        kwargs["_override_model_path"] = resolved_path
    if has_tools:
        kwargs["temperature"] = request.temperature if request.temperature is not None else 0.0
        kwargs["_tool_request"] = True
    elif request.temperature is not None:
        kwargs["temperature"] = request.temperature
    if request.top_p is not None:
        kwargs["top_p"] = request.top_p
    if request.max_tokens is not None:
        kwargs["max_tokens"] = max(request.max_tokens, server_num_predict)
    if request.stop is not None:
        kwargs["stop"] = request.stop

    # Use load balancer if configured - picks server round-robin, uses local inference for "self" URLs
    # Skip if explicitly requested (to prevent loops when called from another posterchanai instance)
    if servers and not skip_load_balancer:
        from app.services.load_balancer import get_healthy_server, is_self_url, NoHealthyServersError

        # Server-to-server requests don't need authentication
        try:
            # Pass full server list to LoadBalancer - it will handle round-robin internally
            # This ensures proper load balancing across all servers
            timeout = int(settings.get("ollama_timeout", "300000")) / 1000
            load_balancer = LoadBalancer(servers, timeout=timeout, model=request.model)

            try:
                _lb_temp = 0.0 if has_tools else temperature
                if request.stream:
                    # Create a wrapper that catches NoHealthyServersError and re-raises it
                    # so the outer handler can catch it and fall back to local
                    lb_stream = load_balancer.chat_stream(
                        messages=messages,
                        temperature=_lb_temp,
                        top_p=top_p,
                        max_tokens=max_tokens
                    )
                    
                    # Wrap generator - if NoHealthyServersError is raised, stop silently
                    # The outer exception handler will catch it when StreamingResponse fails
                    # and fall back to local inference
                    import asyncio as _asyncio

                    async def _with_keepalive(src):
                        q: _asyncio.Queue = _asyncio.Queue(maxsize=128)
                        _exc: list = [None]
                        async def _fill():
                            try:
                                async for chunk in src:
                                    await q.put(chunk)
                            except NoHealthyServersError as _e:
                                _exc[0] = _e
                            except Exception:
                                pass
                            finally:
                                await q.put(None)
                        task = _asyncio.create_task(_fill())
                        try:
                            while True:
                                try:
                                    item = await _asyncio.wait_for(q.get(), timeout=3.0)
                                    if item is None:
                                        if _exc[0] is not None:
                                            raise _exc[0]
                                        break
                                    yield item
                                except _asyncio.TimeoutError:
                                    yield _KA_EVENT
                        finally:
                            task.cancel()
                            try:
                                await _asyncio.shield(task)
                            except Exception:
                                pass

                    async def safe_stream():
                        try:
                            async for chunk in _with_keepalive(filter_thinking_stream(lb_stream)):
                                yield chunk
                        except NoHealthyServersError:
                            logger.info("[LB-FALLBACK] All remote servers failed, falling back to local inference")
                            prepare_vram_for_llm(db)
                            _fb_svc = get_inference_service(db)
                            _fb_s = _fb_svc.chat_completion_stream(messages=messages, model=request.model, **kwargs)
                            async for chunk in _with_keepalive(filter_thinking_stream(_fb_s)):
                                yield chunk

                    async def safe_stream_tools():
                        try:
                            async for chunk in _with_keepalive(_stream_openai_with_tools(lb_stream, cwd=_oai_cwd, has_prior_tools=_has_prior_tools)):
                                yield chunk
                        except NoHealthyServersError:
                            logger.info("[LB-FALLBACK] All remote servers failed, falling back to local inference (tools)")
                            prepare_vram_for_llm(db)
                            _fb_svc = get_inference_service(db)
                            _fb_s = _fb_svc.chat_completion_stream(messages=messages, model=request.model, **kwargs)
                            async for chunk in _with_keepalive(_stream_openai_with_tools(_fb_s, cwd=_oai_cwd, has_prior_tools=_has_prior_tools)):
                                yield chunk

                    return StreamingResponse(
                        safe_stream_tools() if has_tools else safe_stream(),
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
                        temperature=_lb_temp,
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
    prepare_vram_for_llm(db)
    service = get_inference_service(db)

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
            if skip_load_balancer:
                # Load-balanced hop from another node: stream raw output (including think tags).
                # The originating node applies filter_thinking_stream / _stream_openai_with_tools
                # and can parse tool calls from inside <think> blocks. If we strip here,
                # the tool call is gone before Arc can parse it.
                _inner = stream
            elif has_tools:
                _inner = _stream_openai_with_tools(stream, cwd=_oai_cwd if has_tools else "", has_prior_tools=_has_prior_tools)
            else:
                _inner = filter_thinking_stream(stream)
            async def _local_oai_stream():
                import asyncio as _aio
                q: _aio.Queue = _aio.Queue(maxsize=128)
                async def _fill():
                    try:
                        async for chunk in _inner:
                            await q.put(chunk)
                    except Exception:
                        pass
                    finally:
                        await q.put(None)
                task = _aio.create_task(_fill())
                try:
                    while True:
                        try:
                            item = await _aio.wait_for(q.get(), timeout=3.0)
                            if item is None:
                                break
                            yield item
                        except _aio.TimeoutError:
                            yield _KA_EVENT
                finally:
                    task.cancel()
                    try:
                        await _aio.shield(task)
                    except Exception:
                        pass
            return StreamingResponse(
                _local_oai_stream(),
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

        if has_tools:
            raw_text = ""
            if result.get("choices"):
                raw_text = result["choices"][0].get("message", {}).get("content") or ""
            # Strip hallucinated <tool_result> blocks before parsing.
            raw_text = _TOOL_RESULT_STRIP_RE.sub('', raw_text)
            # Parse tool calls from full text before stripping thinking (open-think places
            # <tool_call> inside <think> blocks which would otherwise be stripped first).
            clean_text, tool_calls = _openai_parse_tool_calls(raw_text)
            clean_text = strip_thinking_tags(clean_text)
            if tool_calls and clean_text == _FILTER_FALLBACK:
                clean_text = ""
            if tool_calls:
                tool_calls = _rewrite_privileged_tool_calls(tool_calls, cwd=_oai_cwd)
                result["choices"][0]["message"]["content"] = clean_text or None
                result["choices"][0]["message"]["tool_calls"] = tool_calls
                result["choices"][0]["finish_reason"] = "tool_calls"
            else:
                _raw_stripped = raw_text.strip()
                if _raw_stripped and len(_raw_stripped) < 400 and "\n\n" not in _raw_stripped and "<tool_call>" not in _raw_stripped and not _has_prior_tools:
                    # Short preamble on first turn — model described the task instead of calling a tool.
                    logger.warning(f"[NO-TOOL-CALL-PREAMBLE] OAI non-stream: {len(_raw_stripped)}c: {_raw_stripped[:80]!r} — injecting retry signal")
                    result["choices"][0]["message"]["content"] = None
                    result["choices"][0]["message"]["tool_calls"] = [{
                        "id": f"call_retry_{uuid.uuid4().hex[:12]}",
                        "type": "function",
                        "function": {
                            "name": "Bash",
                            "arguments": json.dumps({"command": "echo 'ERROR: Call the tool now. Do not describe the task — use Write, Bash, or Read.'", "description": "Retry signal"}),
                        },
                    }]
                    result["choices"][0]["finish_reason"] = "tool_calls"
                else:
                    # raw_text may be empty if native tool_calls came from the service
                    result["choices"][0]["message"]["content"] = strip_thinking_tags(raw_text) if raw_text else None
        else:
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

    # Claude Code aliases — let clients request standard Anthropic model names
    for alias in ("claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5"):
        model_list.append(ModelInfo(
            id=alias,
            object="model",
            created=0,
            owned_by="posterchanai",
            root_context_length=ctx,
            context_length=ctx,
        ))

    return ModelsResponse(object="list", data=model_list)
