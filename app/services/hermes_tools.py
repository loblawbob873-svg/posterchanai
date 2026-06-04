"""Qwen/Hermes-style tool-calling.

llama-cpp-python's generic ``chatml-function-calling`` handler uses a ``functions.NAME:``
convention that Qwen3.x was NOT trained on, so tool calls intermittently leak as plain
text (``functions.write:``) and never parse. Qwen is trained on the Hermes format:
tools are described in a ``<tools>`` block in the system prompt and the model emits
``<tool_call>{"name":...,"arguments":...}</tool_call>``.

This module injects tools in that format and parses the model's output back into OpenAI
``tool_calls`` — independent of llama-cpp's handler. The model is run with plain ``chatml``.
"""
import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

# Tolerant matcher: optional whitespace/newlines, capture the JSON object inside.
_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def _tools_system_text(tools: List[Dict[str, Any]]) -> str:
    sig = "\n".join(json.dumps(t, ensure_ascii=False) for t in tools)
    return (
        "# Tools\n\n"
        "You may call one or more functions to assist with the user query.\n\n"
        "When the user asks you to create, edit, run, or inspect files or commands, you "
        "MUST accomplish it by calling the tools — actually perform each action. Do NOT "
        "just describe the steps or print code/commands in your reply; emit a <tool_call> "
        "for each action and wait for its result before continuing.\n\n"
        "You are provided with function signatures within <tools></tools> XML tags:\n"
        f"<tools>\n{sig}\n</tools>\n\n"
        "For each function call, return a json object with function name and arguments "
        "within <tool_call></tool_call> XML tags:\n"
        "<tool_call>\n"
        '{"name": <function-name>, "arguments": <args-json-object>}\n'
        "</tool_call>\n\n"
        "Rules for the arguments field (follow exactly):\n"
        '- "arguments" MUST be a JSON object whose keys are exactly the parameter names '
        "from the function's signature above.\n"
        "- Include every required parameter. Never put a bare value, string, or command "
        'in place of the object — always wrap it, e.g. {"command": "python3 fib.py"}, '
        'not "python3 fib.py".\n'
        "- Emit strict JSON: double-quoted keys and strings, proper escaping, no trailing "
        "commas, no comments, no extra text inside the tags.\n\n"
        "Example of a correct call:\n"
        "<tool_call>\n"
        '{"name": "bash", "arguments": {"command": "python3 fib.py"}}\n'
        "</tool_call>"
    )


def inject_tools(messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return a copy of messages adapted to the Hermes format:

    - the tools block is merged into (or prepended as) the system message;
    - prior ``role=tool`` results become ``<tool_response>`` user turns;
    - prior assistant ``tool_calls`` become textual ``<tool_call>`` turns,
    so multi-step conversations round-trip in the format the model understands.
    """
    converted: List[Dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            content = m.get("content")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            converted.append({"role": "user", "content": f"<tool_response>\n{content}\n</tool_response>"})
        elif role == "assistant" and m.get("tool_calls"):
            parts = []
            if m.get("content"):
                parts.append(m["content"])
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function", {}) or {}
                args = fn.get("arguments", "{}")
                try:
                    args = json.loads(args) if isinstance(args, str) else args
                except Exception:
                    pass
                parts.append("<tool_call>\n" + json.dumps({"name": fn.get("name"), "arguments": args}, ensure_ascii=False) + "\n</tool_call>")
            converted.append({"role": "assistant", "content": "\n".join(parts)})
        else:
            converted.append(dict(m))

    tools_text = _tools_system_text(tools)
    if converted and converted[0].get("role") == "system":
        base = converted[0].get("content", "")
        converted[0] = {"role": "system", "content": (base.rstrip() + "\n\n" + tools_text) if base else tools_text}
    else:
        converted.insert(0, {"role": "system", "content": tools_text})
    return converted


def parse_tool_calls(text: str) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Extract Hermes ``<tool_call>`` blocks -> (content_or_None, openai_tool_calls)."""
    if not text:
        return text, []
    matches = list(_TOOL_CALL_RE.finditer(text))
    if not matches:
        return text, []
    tool_calls = []
    for i, mt in enumerate(matches):
        try:
            obj = json.loads(mt.group(1).strip())
        except Exception:
            continue
        args = obj.get("arguments", {})
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        tool_calls.append({
            "id": f"call_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "index": i,
            "function": {"name": obj.get("name"), "arguments": args},
        })
    if not tool_calls:
        return text, []
    content = _TOOL_CALL_RE.sub("", text).strip()
    return (content or None), tool_calls


def generate_message(model, messages, tools, params, strip_thinking=None) -> Tuple[Dict[str, Any], str]:
    """Run a plain-chatml generation with tools injected, parse the result.

    Returns (openai_message_dict, finish_reason). ``params`` must NOT contain tools/
    tool_choice (we do tool handling ourselves, not via llama-cpp's handler).
    """
    injected = inject_tools(messages, tools)
    result = model.create_chat_completion(messages=injected, **params)
    text = (result.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    if strip_thinking:
        text = strip_thinking(text)
    content, tool_calls = parse_tool_calls(text)
    msg: Dict[str, Any] = {"role": "assistant"}
    if tool_calls:
        msg["content"] = content
        msg["tool_calls"] = tool_calls
        return msg, "tool_calls"
    msg["content"] = content if content is not None else text
    return msg, "stop"
