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

# JSON-Hermes form: <tool_call>{"name":...,"arguments":...}</tool_call>
_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
# Qwen native form: <function=NAME><parameter=KEY>VALUE</parameter>...</function>
_FUNC_RE = re.compile(r"<function=([^>\s]+)\s*>(.*?)</function>", re.DOTALL)
_PARAM_RE = re.compile(r"<parameter=([^>\s]+)\s*>\s*(.*?)\s*</parameter>", re.DOTALL)
# Any <tool_call>...</tool_call> block (either form) - for stripping from content.
_ANY_TOOL_CALL_RE = re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL)


def _tools_system_text(tools: List[Dict[str, Any]]) -> str:
    sig = "\n".join(json.dumps(t, ensure_ascii=False) for t in tools)
    return (
        "# Tools\n\n"
        "You may call one or more functions to assist with the user query.\n\n"
        "When the user asks you to create, edit, run, or inspect files or commands, you "
        "MUST accomplish it by calling the tools — actually perform each action. Do NOT "
        "just describe the steps or print code/commands in your reply; emit a <tool_call> "
        "for each action and wait for its result before continuing.\n\n"
        "Once a tool returns a <tool_response>, that action is DONE. Do NOT call the same "
        "tool with the same arguments again — an empty or success response means it worked. "
        "Read the responses you already have, then move to the next step or give your final "
        "answer. Never repeat a completed step.\n\n"
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
            # A silent success (e.g. mkdir) returns empty output; make it explicit so the
            # model doesn't read a blank response as failure and retry the same call.
            content = (content or "").strip() or "(command completed successfully, no output)"
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
            mm = dict(m)
            # Safety net: strip the Qwen /no_think control token so it never reaches the
            # model as literal text (plain chatml doesn't strip it like Qwen's own template).
            c = mm.get("content")
            if isinstance(c, str) and "/no_think" in c:
                mm["content"] = c.replace(" /no_think", "").replace("\n/no_think", "").replace("/no_think", "").strip()
            converted.append(mm)

    tools_text = _tools_system_text(tools)
    if converted and converted[0].get("role") == "system":
        base = converted[0].get("content", "")
        converted[0] = {"role": "system", "content": (base.rstrip() + "\n\n" + tools_text) if base else tools_text}
    else:
        converted.insert(0, {"role": "system", "content": tools_text})
    return converted


def _mk_call(name, args, idx):
    if not isinstance(args, str):
        args = json.dumps(args, ensure_ascii=False)
    return {"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
            "index": idx, "function": {"name": name, "arguments": args}}


def parse_tool_calls(text: str) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Extract tool calls -> (content_or_None, openai_tool_calls). Handles BOTH the
    JSON-Hermes form and Qwen's native <function=..><parameter=..> form so whichever the
    model emits is caught."""
    if not text:
        return text, []
    tool_calls = []
    # 1) JSON-Hermes: <tool_call>{...}</tool_call>
    for mt in _TOOL_CALL_RE.finditer(text):
        try:
            obj = json.loads(mt.group(1).strip())
        except Exception:
            continue
        tool_calls.append(_mk_call(obj.get("name"), obj.get("arguments", {}), len(tool_calls)))
    # 2) Qwen native: <function=NAME><parameter=KEY>VALUE</parameter>...</function>
    if not tool_calls:
        for fm in _FUNC_RE.finditer(text):
            args = {k.strip(): v for k, v in _PARAM_RE.findall(fm.group(2))}
            tool_calls.append(_mk_call(fm.group(1).strip(), args, len(tool_calls)))
    if not tool_calls:
        return text, []
    # Strip any tool-call block (both forms) from the visible content.
    content = _ANY_TOOL_CALL_RE.sub("", text)
    content = _FUNC_RE.sub("", content).strip()
    return (content or None), tool_calls


def tool_sse_chunks(completion_id: str, created: int, model_name: str,
                    msg: Dict[str, Any], finish: str) -> list:
    """Synthesized SSE lines for a (non-streamable) tool/content response: one delta chunk
    (content and/or tool_calls), a terminal finish_reason chunk, then [DONE]. Shared by both
    backends so the stream-synthesis format stays identical."""
    delta: Dict[str, Any] = {"role": "assistant"}
    if msg.get("content"):
        delta["content"] = msg["content"]
    if msg.get("tool_calls"):
        delta["tool_calls"] = msg["tool_calls"]
    base = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model_name}
    return [
        "data: " + json.dumps({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}) + "\n\n",
        "data: " + json.dumps({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}) + "\n\n",
        "data: [DONE]\n\n",
    ]


_formatter_cache: Dict[str, Any] = {}


def _get_formatter(template: str):
    fmt = _formatter_cache.get(template)
    if fmt is None:
        from llama_cpp.llama_chat_format import Jinja2ChatFormatter
        fmt = Jinja2ChatFormatter(template=template, bos_token="", eos_token="<|im_end|>",
                                  add_generation_prompt=True)
        _formatter_cache[template] = fmt
    return fmt


def _prep_for_template(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Parse assistant tool_call arguments (str -> dict; the native template iterates them
    with ``| items``) and strip the /no_think control token from content."""
    out = []
    for m in messages:
        mm = dict(m)
        # Tool results: make empty/silent output explicit (mkdir etc.) so the model doesn't
        # read a blank <tool_response> as failure and re-call the same tool (the mkdir loop).
        if mm.get("role") == "tool":
            c = mm.get("content")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            mm["content"] = (c or "").strip() or "(command completed successfully, no output)"
            out.append(mm)
            continue
        c = mm.get("content")
        if isinstance(c, str) and "/no_think" in c:
            mm["content"] = c.replace(" /no_think", "").replace("/no_think", "").strip()
        if mm.get("role") == "assistant" and mm.get("tool_calls"):
            tcs = []
            for tc in mm["tool_calls"]:
                tc2 = dict(tc)
                fn = dict(tc2.get("function", {}) or {})
                a = fn.get("arguments")
                if isinstance(a, str):
                    try:
                        fn["arguments"] = json.loads(a)
                    except Exception:
                        fn["arguments"] = {}
                tc2["function"] = fn
                tcs.append(tc2)
            mm["tool_calls"] = tcs
        out.append(mm)
    return out


def generate_message(model, messages, tools, params, strip_thinking=None) -> Tuple[Dict[str, Any], str]:
    """Generate + parse a tool-aware response.

    Prefers the model's OWN embedded chat template — it renders tool_call/tool_response
    history in the exact format the model was trained on, so the model recognizes completed
    steps (manual JSON-Hermes did not, causing multi-step tool loops). Falls back to a manual
    chatml + JSON-Hermes prompt if the embedded template is unavailable or errors.
    ``params`` must NOT contain tools/tool_choice.
    """
    template = (getattr(model, "metadata", None) or {}).get("tokenizer.chat_template")
    if template:
        try:
            r = _get_formatter(template)(messages=_prep_for_template(messages), tools=tools)
            stops = list(getattr(r, "stop", None) or [])
            if "<|im_end|>" not in stops:
                stops.append("<|im_end|>")
            toks = model.tokenize(r.prompt.encode("utf-8"), add_bos=False, special=True)
            _p = dict(params); _p["stop"] = stops
            text = (model.create_completion(prompt=toks, **_p).get("choices") or [{}])[0].get("text") or ""
            # The embedded template pre-fills "<think>" in the assistant turn, so generation
            # starts INSIDE the think block - only the closing </think> appears. Drop everything
            # up to it (otherwise the reasoning leaks into the visible content).
            if "</think>" in text and "<think>" not in text:
                text = text.split("</think>", 1)[-1]
            if strip_thinking:
                text = strip_thinking(text)
            content, tool_calls = parse_tool_calls(text)
            msg: Dict[str, Any] = {"role": "assistant", "content": content}
            return (({**msg, "tool_calls": tool_calls}, "tool_calls") if tool_calls else (msg, "stop"))
        except Exception as e:
            import logging
            logging.getLogger("hermes_tools").warning("native-template tool path failed (%s); using fallback", e)

    # Fallback: manual chatml + JSON-Hermes injection.
    result = model.create_chat_completion(messages=inject_tools(messages, tools), **params)
    text = (result.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    if strip_thinking:
        text = strip_thinking(text)
    content, tool_calls = parse_tool_calls(text)
    msg = {"role": "assistant", "content": content}
    return (({**msg, "tool_calls": tool_calls}, "tool_calls") if tool_calls else (msg, "stop"))
