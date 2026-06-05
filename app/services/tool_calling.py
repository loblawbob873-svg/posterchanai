"""Native tool-calling for Qwen GGUFs.

Tool calls are rendered with the model's OWN embedded chat template (Jinja2ChatFormatter),
which formats tool_call/tool_response history in the exact form the model was trained on, and
the output is parsed back into OpenAI ``tool_calls``. This replaced an earlier manual
JSON-Hermes injection that the model didn't recognize (it looped on multi-step tasks).

Notes for future readers:
- Qwen's native tool format is ``<tool_call><function=NAME><parameter=KEY>VALUE</parameter>
  </function></tool_call>`` (NOT the JSON ``<tool_call>{...}</tool_call>`` Hermes form);
  ``parse_tool_calls`` handles both.
- SYCL (Arc) string-stop is unreliable, so generation gets a Python-level EOS stop or the
  model rambles past the stop and blows its token budget -> empty response.
"""
import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("tool_calling")

# JSON-Hermes form: <tool_call>{"name":...,"arguments":...}</tool_call>
_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
# Qwen native form: <function=NAME><parameter=KEY>VALUE</parameter>...</function>
_FUNC_RE = re.compile(r"<function=([^>\s]+)\s*>(.*?)</function>", re.DOTALL)
_PARAM_RE = re.compile(r"<parameter=([^>\s]+)\s*>\s*(.*?)\s*</parameter>", re.DOTALL)
# Alternate JSON wrapper that Qwen2.5-Coder emits: <function-calls>{...JSON...}</function-calls>
# (also tolerates singular / underscore variants). Inner is one or more {"name","arguments"} objects.
_FUNC_CALLS_RE = re.compile(r"<function[-_]calls?>\s*(.*?)\s*</function[-_]calls?>", re.DOTALL)
# Any <tool_call>...</tool_call> block (either form) - for stripping from content.
_ANY_TOOL_CALL_RE = re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL)


def _iter_json_objects(s: str) -> List[Any]:
    """Yield JSON objects from a string that is either a single object, a JSON array, or several
    concatenated objects (some models emit multiple calls back-to-back inside one wrapper)."""
    s = (s or "").strip()
    if not s:
        return []
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else [v]
    except Exception:
        pass
    objs, depth, start, instr, esc = [], 0, None, False, False
    for i, ch in enumerate(s):
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        objs.append(json.loads(s[start:i + 1]))
                    except Exception:
                        pass
                    start = None
    return objs


def _mk_call(name, args, idx):
    if not isinstance(args, str):
        args = json.dumps(args, ensure_ascii=False)
    return {"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
            "index": idx, "function": {"name": name, "arguments": args}}


def _coerce_param_value(v: str) -> Any:
    """Decode a Qwen-native ``<parameter=KEY>VALUE</parameter>`` value.

    The regex captures every VALUE as a raw string, but array/object/number/bool params are
    emitted as their JSON literal (e.g. ``questions=[{...}]``, ``timeout=5000``). Left as strings
    they reach the client as the WRONG type — an array param arrives as a JSON-encoded string and
    fails the client's schema (``Expected array, got "[{\\""``), a number arrives as ``"5000"``
    (``Expected number``). Decode the literal so the forwarded ``arguments`` carries real types.
    Only upgrade when ``json.loads`` yields a non-string: plain text (commands, paths, prose) isn't
    valid JSON and stays a string, so string params are untouched."""
    s = (v or "").strip()
    if not s:
        return v
    try:
        parsed = json.loads(s)
    except Exception:
        return v
    return v if isinstance(parsed, str) else parsed


# Models trained on other agent toolsets emit these names; map them to the equivalent the
# client actually provided (only applied when the target IS in the provided tools, so it can
# only rescue a call the client would otherwise reject - never breaks a valid one).
_TOOL_ALIASES = {
    "run_shell_command": "bash", "run_command": "bash", "shell": "bash", "exec": "bash",
    "execute": "bash", "execute_command": "bash", "run": "bash", "terminal": "bash",
    "create_file": "write", "write_file": "write", "writefile": "write", "save_file": "write",
    "edit_file": "edit", "read_file": "read", "view_file": "read", "list_files": "glob",
}


def _normalize_tool_names(tool_calls, tools):
    """Rename a tool call whose name the client doesn't provide to a known-equivalent that it
    does (e.g. run_shell_command -> bash). No-op when the name is already valid."""
    avail = {(t.get("function") or {}).get("name") for t in (tools or []) if t.get("function")}
    for tc in tool_calls:
        n = tc["function"]["name"]
        if n not in avail:
            alias = _TOOL_ALIASES.get(n)
            if alias and alias in avail:
                tc["function"]["name"] = alias
    return tool_calls


def parse_tool_calls(text: str) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Extract tool calls -> (content_or_None, openai_tool_calls). Handles BOTH the JSON-Hermes
    form and Qwen's native <function=..><parameter=..> form so whichever the model emits is caught."""
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
            args = {k.strip(): _coerce_param_value(v) for k, v in _PARAM_RE.findall(fm.group(2))}
            tool_calls.append(_mk_call(fm.group(1).strip(), args, len(tool_calls)))
    # 3) <function-calls>{"name":..,"arguments":..}</function-calls> wrapper (Qwen2.5-Coder).
    if not tool_calls:
        for fm in _FUNC_CALLS_RE.finditer(text):
            for obj in _iter_json_objects(fm.group(1)):
                if isinstance(obj, dict) and obj.get("name"):
                    tool_calls.append(_mk_call(obj.get("name"),
                                               obj.get("arguments", obj.get("parameters", {})),
                                               len(tool_calls)))
    if not tool_calls:
        return text, []
    # Small models sometimes degenerate into repeating the same call many times (e.g. 296
    # identical 'question' calls in one response). Dedup identical (name+args) calls and cap
    # the total so a runaway can't reach the client. Legitimate multi-tool turns are 1-3 calls.
    seen = set()
    deduped = []
    for tc in tool_calls:
        key = (tc["function"]["name"], tc["function"]["arguments"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(tc)
    tool_calls = deduped[:8]
    for i, tc in enumerate(tool_calls):
        tc["index"] = i
    # Strip any tool-call block (all forms) from the visible content.
    content = _ANY_TOOL_CALL_RE.sub("", text)
    content = _FUNC_RE.sub("", content)
    content = _FUNC_CALLS_RE.sub("", content).strip()
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


def _eos_stopping_criteria(model):
    """Python-level stop on the EOS / <|im_end|> token ids. The C++ string-stop is unreliable on
    Intel SYCL (Arc): the model rambles past the stop and blows its token budget -> empty response.
    Stopping on the token id fixes that. Harmless on CUDA (belt-and-suspenders). Returns None if
    unavailable."""
    try:
        from llama_cpp import StoppingCriteriaList
    except Exception:
        return None
    ids = set()
    try:
        e = model.token_eos()
        if e is not None and e >= 0:
            ids.add(e)
    except Exception:
        pass
    try:
        t = model.tokenize(b"<|im_end|>", add_bos=False, special=True)
        if len(t) == 1:
            ids.add(t[0])
    except Exception:
        pass
    if not ids:
        return None

    def _stop(tokens, logits, _ids=ids):
        return len(tokens) > 0 and tokens[-1] in _ids
    return StoppingCriteriaList([_stop])


def _prep_for_template(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Adapt messages for the native template:
    - tool results: render empty/silent output explicitly (mkdir etc.) so the model doesn't read
      a blank <tool_response> as failure and re-call the same tool (the mkdir loop);
    - assistant tool_call arguments: parse str -> dict (the template iterates them with ``| items``);
    - strip the /no_think control token from content (plain generation doesn't strip it)."""
    out = []
    for m in messages:
        mm = dict(m)
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
    """Generate + parse a tool-aware response using the model's embedded chat template.

    Returns (openai_message_dict, finish_reason). ``params`` must NOT contain tools/tool_choice.
    Falls back to a plain chat completion only if the model has no embedded template.
    """
    # TEMP loop-debug: dump the conversation shape + tool-result content the model actually receives,
    # so we can tell whether tool results are conveyed (vs the model re-doing steps blindly).
    try:
        _dbg = []
        for _m in messages:
            _r = _m.get("role")
            if _r == "assistant" and _m.get("tool_calls"):
                _dbg.append("asst[" + ",".join((tc.get("function") or {}).get("name", "?") for tc in _m["tool_calls"]) + "]")
            elif _r == "tool":
                _c = _m.get("content")
                _c = _c if isinstance(_c, str) else str(_c)
                _dbg.append("toolresult=" + repr((_c or "")[:90]))
            else:
                _dbg.append(_r)
        logger.warning("TOOLDBG n=%d seq=%s", len(messages), " | ".join(_dbg[-8:]))
    except Exception:
        pass
    template = (getattr(model, "metadata", None) or {}).get("tokenizer.chat_template")
    if template:
        try:
            r = _get_formatter(template)(messages=_prep_for_template(messages), tools=tools)
            stops = list(getattr(r, "stop", None) or [])
            if "<|im_end|>" not in stops:
                stops.append("<|im_end|>")
            toks = model.tokenize(r.prompt.encode("utf-8"), add_bos=False, special=True)
            _p = dict(params)
            _p["stop"] = stops
            _sc = _eos_stopping_criteria(model)
            if _sc is not None:
                _p["stopping_criteria"] = _sc
            raw = (model.create_completion(prompt=toks, **_p).get("choices") or [{}])[0].get("text") or ""
            # Generation starts inside the template's pre-filled "<think>" block.
            if "</think>" in raw:
                # Think closed: the real answer/tool_call is AFTER it. Parse only there, so a
                # <tool_call> the model writes inside its REASONING (a plan/example) is never
                # mistaken for a real call, and unclosed reasoning is never surfaced as content.
                content, tool_calls = parse_tool_calls(raw.split("</think>", 1)[1].strip())
            else:
                # Think never closed: the model emitted the <tool_call> before closing </think> (or
                # ran out). Parse the full raw so a valid call isn't discarded -> empty message ->
                # the LB reads the node as dead and aborts the streamed SSE -> opencode just stops.
                _, tool_calls = parse_tool_calls(raw)
                content = None
            tool_calls = _normalize_tool_names(tool_calls, tools)
            # The model sometimes closes </think> then ends the turn WITHOUT emitting the answer /
            # tool_call (lazy end-of-turn) -> empty message -> the LB reads the node as dead and
            # aborts the already-streamed SSE -> opencode stops. Retry by RE-RENDERING with /no_think
            # injected into the system turn: that makes the template signal no-think mode, so the
            # model answers directly instead of thinking-then-quitting. (Prompt surgery to close the
            # think block alone does NOT work - without the /no_think signal the model still quits.)
            if not tool_calls and not content and "</think>" in raw:
                nt = [dict(m) for m in _prep_for_template(messages)]
                for _m in nt:
                    if _m.get("role") == "system":
                        _m["content"] = ((_m.get("content") or "") + " /no_think").strip()
                        break
                else:
                    nt.insert(0, {"role": "system", "content": "/no_think"})
                r2 = _get_formatter(template)(messages=nt, tools=tools)
                toks2 = model.tokenize(r2.prompt.encode("utf-8"), add_bos=False, special=True)
                raw2 = (model.create_completion(prompt=toks2, **_p).get("choices") or [{}])[0].get("text") or ""
                body2 = raw2.split("</think>", 1)[1].strip() if "</think>" in raw2 else raw2.strip()
                content, tool_calls = parse_tool_calls(body2)
                tool_calls = _normalize_tool_names(tool_calls, tools)
                if not tool_calls and not content:
                    logger.warning("empty after /no_think retry; raw2[:160]=%r", raw2[:160])
            elif not tool_calls and not content:
                logger.warning("empty tool generation (think never closed, len=%d); raw[:300]=%r",
                               len(raw), raw[:300])
            # Last-resort band-aid: NEVER return an empty message. An empty tool response makes the
            # LB read the node as dead and abort the already-streamed SSE -> opencode HARD-stops.
            # If retries still produced nothing usable, surface the model's own reasoning as content
            # so the stream stays non-empty (degraded: text, not an action - but the agent doesn't
            # dead-stop and can continue on the next turn).
            if not tool_calls and not content:
                reasoning = (raw.split("</think>", 1)[0] if "</think>" in raw else raw).replace("<think>", "").strip()
                content = reasoning or None
                if content:
                    logger.warning("returning reasoning-as-content to avoid hard-stop (len=%d)", len(content))
            msg: Dict[str, Any] = {"role": "assistant", "content": content}
            return (({**msg, "tool_calls": tool_calls}, "tool_calls") if tool_calls else (msg, "stop"))
        except Exception as e:
            import logging
            logging.getLogger("tool_calling").warning("native-template tool path failed (%s); using fallback", e)

    # Fallback (no embedded template): plain chat completion; still parse any native tool calls.
    result = model.create_chat_completion(messages=messages, **params)
    text = (result.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    if strip_thinking:
        text = strip_thinking(text)
    content, tool_calls = parse_tool_calls(text)
    tool_calls = _normalize_tool_names(tool_calls, tools)
    msg = {"role": "assistant", "content": content}
    return (({**msg, "tool_calls": tool_calls}, "tool_calls") if tool_calls else (msg, "stop"))
