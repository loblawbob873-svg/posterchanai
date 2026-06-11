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
            _txt = (c or "").strip()
            # Treat a TRULY-empty result AND common "no output" placeholders (opencode renders a
            # silent command as the literal "(no output)") as an explicit success. Otherwise a small
            # model reads the blank/placeholder as failure and re-runs the same command forever
            # (the pip-install / mkdir loop).
            if not _txt or _txt.lower().strip("().") in ("no output", "empty", "no stdout", "command produced no output"):
                _txt = "(command completed successfully, no output)"
            mm["content"] = _txt
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


def _tool_sig(tc: Dict[str, Any]):
    """Canonical (name, args) signature for a tool call, so a dict-args and a JSON-string-args
    form of the SAME call compare equal."""
    fn = tc.get("function", {}) or {}
    args = fn.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            pass
    try:
        args = json.dumps(args, sort_keys=True)
    except Exception:
        args = str(args)
    return (fn.get("name"), args)


def _is_repeat_tool_call(messages: List[Dict[str, Any]], new_tcs: list) -> bool:
    """True if the just-generated call duplicates the MOST RECENT prior assistant tool call —
    i.e. the model is re-emitting the same command it already ran last turn (the pip-install
    re-run loop). Only the immediately-preceding call is compared, so a legitimate later re-run
    (test -> edit -> test) is NOT flagged."""
    if not new_tcs:
        return False
    new_sig = _tool_sig(new_tcs[0])
    for m in reversed(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            return any(_tool_sig(tc) == new_sig for tc in m["tool_calls"])
    return False


def _looks_like_intent_to_act(text: str) -> bool:
    """True if content ANNOUNCES a next action but emitted no tool call (the narrate-instead-of-act
    stop: e.g. "Let me check the stacking context:" / "I'll fix the import …" then ends the turn).
    Conservative: a genuine final answer or a question to the user must NOT match (those should stop)."""
    if not text:
        return False
    t = text.strip()
    if t.endswith("?"):
        return False  # a question to the user — stopping is correct
    low = t.lower()
    if any(k in low[-200:] for k in ("let me know", "feel free", "anything else", "all set",
                                     "is complete", "is now complete", "have completed", "tests pass")):
        return False  # a closing / completion — leave it
    if t.endswith(":"):
        return True  # announced an action then trailed off without the call (strongest tell)
    tail = low[-160:]
    return any(p in tail for p in ("let me ", "let's ", "i'll ", "i will ", "now i ", "next, i",
                                   "i'm going to", "i am going to", "i need to ", "let me check",
                                   "let me look", "let me fix", "let me run", "let me try"))


def _fit_to_context(model, template, messages, tools, reserve):
    """Trim the conversation so the rendered prompt fits the model's context window.

    A long agentic session (opencode keeps the full history) can render to MORE tokens than the
    context window (e.g. 32399 > 32256), which makes the template/tokenize path raise BEFORE any
    generation -> the whole native tool path falls to the hard "couldn't generate" fallback. We
    instead drop the OLDEST turns (keeping a leading system turn + as many recent turns as fit),
    so the agent degrades gracefully and keeps going. Returns the (possibly trimmed) message list;
    on any error returns the input unchanged (the caller's try/except still guards it)."""
    try:
        n_ctx = int(model.n_ctx())
    except Exception:
        return messages
    budget = n_ctx - max(int(reserve or 0), 512)
    if budget <= 0:
        return messages

    def _ntok(msgs):
        r = _get_formatter(template)(messages=_prep_for_template(msgs), tools=tools)
        return len(model.tokenize(r.prompt.encode("utf-8"), add_bos=False, special=True))

    try:
        if _ntok(messages) <= budget:
            return messages
    except Exception:
        return messages

    head = []
    rest = list(messages)
    if rest and rest[0].get("role") == "system":
        head.append(rest.pop(0))
    # Trim oldest ASSISTANT/TOOL turns only — NEVER a user message. The user's messages carry the
    # task and every mid-session correction; dropping them is why a long session stops "listening".
    # An assistant tool_call is dropped together with its following tool result(s) so we never leave
    # an orphan (tool result without its call / call without result), which some templates reject.
    def _oldest_droppable_span(msgs):
        for k, m in enumerate(msgs):
            if m.get("role") == "user":
                continue
            span = 1
            if m.get("role") == "assistant" and m.get("tool_calls"):
                j = k + 1
                while j < len(msgs) and msgs[j].get("role") == "tool":
                    span += 1
                    j += 1
            return k, span
        return None, 0

    guard = 0
    while guard < len(messages) + 5:
        guard += 1
        try:
            if _ntok(head + rest) <= budget:
                break
        except Exception:
            break
        k, span = _oldest_droppable_span(rest)
        if k is None:  # only user messages left — stop (better an over-budget retry than losing them)
            break
        del rest[k:k + span]
    # Never start the kept tail on an orphan tool result.
    while rest and rest[0].get("role") == "tool":
        rest.pop(0)
    trimmed = head + rest
    # Pathological: a single recent turn (e.g. a huge file read) alone exceeds the budget. Truncate
    # the largest tool result's content so we still fit instead of hard-failing.
    try:
        if _ntok(trimmed) > budget:
            biggest = max((m for m in trimmed if m.get("role") == "tool" and isinstance(m.get("content"), str)),
                          key=lambda m: len(m["content"]), default=None)
            if biggest is not None and len(biggest["content"]) > 4000:
                keep = max(2000, len(biggest["content"]) - (len(biggest["content"]) // 2))
                biggest["content"] = biggest["content"][:keep] + "\n…[truncated to fit context]"
    except Exception:
        pass
    logger.warning("trimmed conversation to fit context (%d -> %d messages)", len(messages), len(trimmed))
    return trimmed


def generate_message(model, messages, tools, params, strip_thinking=None) -> Tuple[Dict[str, Any], str]:
    """Generate + parse a tool-aware response using the model's embedded chat template.

    Returns (openai_message_dict, finish_reason). ``params`` must NOT contain tools/tool_choice.
    Falls back to a plain chat completion only if the model has no embedded template.
    """
    # Conversation-shape trace for diagnosing tool-loop issues (whether tool results are conveyed
    # vs. the model re-doing steps blindly). Kept at DEBUG so it doesn't spam the journal.
    if logger.isEnabledFor(logging.DEBUG):
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
            logger.debug("TOOLDBG n=%d seq=%s", len(messages), " | ".join(_dbg[-8:]))
        except Exception:
            pass
    template = (getattr(model, "metadata", None) or {}).get("tokenizer.chat_template")
    if template:
        try:
            # Trim the oldest turns if the full history would overflow the context window, so a long
            # session degrades gracefully instead of raising "Requested tokens exceed context window"
            # and falling to the hard "couldn't generate" fallback. All downstream re-renders
            # (/no_think retry, re-steer) use this fitted list via `messages`.
            messages = _fit_to_context(model, template, messages, tools, (params or {}).get("max_tokens"))
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
            # Reset the reused model's context before generating. The SYCL (Arc) build of
            # llama-cpp-python 0.3.28 mishandles cross-request context reuse and throws
            # "could not broadcast input array from shape (N,) into shape (M,)" when the next
            # prompt doesn't fit the stale buffer; a clean reset sidesteps that path.
            try:
                model.reset()
            except Exception:
                pass
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
            if not tool_calls and not content:
                # Empty first generation — EITHER the think block closed with a lazy end-of-turn,
                # OR it NEVER closed (runaway reasoning that blew the token budget and dumped 20k+
                # chars). Both used to leak: the closed case dead-stopped, the runaway case surfaced
                # raw reasoning as "content" (opencode got rambling instead of an edit, over and
                # over). Retry RE-RENDERED with /no_think so the model answers / tool-calls directly
                # instead of thinking-then-quitting or rambling.
                if "</think>" not in raw:
                    logger.warning("runaway think (never closed, len=%d) — retrying with /no_think", len(raw))
                nt = [dict(m) for m in _prep_for_template(messages)]
                for _m in nt:
                    if _m.get("role") == "system":
                        _m["content"] = ((_m.get("content") or "") + " /no_think").strip()
                        break
                else:
                    nt.insert(0, {"role": "system", "content": "/no_think"})
                try:
                    try:
                        model.reset()  # clean context for the 2nd generation (SYCL reuse bug)
                    except Exception:
                        pass
                    r2 = _get_formatter(template)(messages=nt, tools=tools)
                    toks2 = model.tokenize(r2.prompt.encode("utf-8"), add_bos=False, special=True)
                    raw2 = (model.create_completion(prompt=toks2, **_p).get("choices") or [{}])[0].get("text") or ""
                    body2 = raw2.split("</think>", 1)[1].strip() if "</think>" in raw2 else raw2.strip()
                    content, tool_calls = parse_tool_calls(body2)
                    tool_calls = _normalize_tool_names(tool_calls, tools)
                    if not tool_calls and not content:
                        logger.warning("empty after /no_think retry; raw2[:160]=%r", raw2[:160])
                except Exception as _e2:
                    # The retry's extra create_completion can hit a llama.cpp shape/state error on a
                    # back-to-back generation. Don't let it nuke the whole native path to the hard
                    # "couldn't generate" fallback — degrade to reasoning-as-content (just below),
                    # which keeps the stream non-empty so opencode continues.
                    logger.warning("/no_think retry generation failed (%s); using reasoning fallback", _e2)
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
            # Push-to-act: the model often ANNOUNCES the next action as prose ("Let me check the
            # stacking context:") but emits no tool call -> opencode reads content+stop as a final
            # answer and ENDS the turn. When tools are available and the content is clearly an
            # intent-to-act (not a question / not a completion), regenerate ONCE forcing the tool call
            # so the agent keeps going. If it still won't emit a call, keep the prose (accept the stop).
            if content and not tool_calls and tools and _looks_like_intent_to_act(content):
                logger.warning("narration-only intent-to-act — pushing for the tool call")
                try:
                    _push = ("Do NOT explain or describe what you are about to do. Emit the tool call "
                             "NOW to perform that next action. Output ONLY the tool call.")
                    ntp = [dict(m) for m in _prep_for_template(messages)]
                    ntp.append({"role": "user", "content": _push + " /no_think"})
                    try:
                        model.reset()
                    except Exception:
                        pass
                    rp = _get_formatter(template)(messages=ntp, tools=tools)
                    toksp = model.tokenize(rp.prompt.encode("utf-8"), add_bos=False, special=True)
                    rawp = (model.create_completion(prompt=toksp, **_p).get("choices") or [{}])[0].get("text") or ""
                    bodyp = rawp.split("</think>", 1)[1].strip() if "</think>" in rawp else rawp.strip()
                    cp, tcp = parse_tool_calls(bodyp)
                    tcp = _normalize_tool_names(tcp, tools)
                    if tcp:
                        content, tool_calls = (cp or None), tcp  # got the action → opencode continues
                        logger.warning("push-to-act succeeded (%s)", (tcp[0].get("function") or {}).get("name"))
                except Exception as _ep:
                    logger.warning("push-to-act generation failed (%s)", _ep)

            # Loop-breaker: small models sometimes re-emit the SAME tool call they just ran (e.g.
            # `pip install X` over and over, even with its result right above). If this call
            # duplicates the immediately-preceding assistant tool call, don't run it again — return
            # a nudge so the agent moves on instead of looping forever.
            if tool_calls and _is_repeat_tool_call(messages, tool_calls):
                _nm = (tool_calls[0].get("function", {}) or {}).get("name")
                logger.warning("repeat tool call (%s) — re-steering to the next step", _nm)
                # Re-generate with a strong 'don't repeat, do the NEXT step' instruction so the model
                # emits a DIFFERENT action and opencode keeps going. (Returning content would make
                # opencode treat it as a final answer and STOP.) Only fall back to a stop-nudge if it
                # still repeats / produces nothing.
                _steered = False
                # The nudge goes in as a TRAILING turn (right after the duplicated result) so it's the
                # most-recent context, not buried in the system prompt; and we regenerate at ESCALATING
                # temperature, because at opencode's low temp the model just greedily reproduces the
                # identical call. Higher temp shakes it onto a different next action.
                _nudge = ("STOP. The previous command already ran and its result is shown above. Running "
                          "it again is forbidden and useless. Do something DIFFERENT now: take the NEXT "
                          "step toward the goal with a different command, or say you are finished.")
                for _temp in (0.85, 1.1):
                    try:
                        nt3 = [dict(m) for m in _prep_for_template(messages)]
                        nt3.append({"role": "user", "content": _nudge + " /no_think"})
                        try:
                            model.reset()
                        except Exception:
                            pass
                        _p3 = dict(_p)
                        _p3["temperature"] = _temp
                        r3 = _get_formatter(template)(messages=nt3, tools=tools)
                        toks3 = model.tokenize(r3.prompt.encode("utf-8"), add_bos=False, special=True)
                        raw3 = (model.create_completion(prompt=toks3, **_p3).get("choices") or [{}])[0].get("text") or ""
                        body3 = raw3.split("</think>", 1)[1].strip() if "</think>" in raw3 else raw3.strip()
                        c3, tc3 = parse_tool_calls(body3)
                        tc3 = _normalize_tool_names(tc3, tools)
                        if tc3 and not _is_repeat_tool_call(messages, tc3):
                            content, tool_calls, _steered = c3, tc3, True   # new action → opencode continues
                            break
                        if c3 and not tc3:
                            content, tool_calls, _steered = c3, None, True  # a different text answer
                            break
                    except Exception as _e3:
                        logger.warning("re-steer generation failed (temp=%s: %s)", _temp, _e3)
                logger.warning("re-steer %s for repeat %s", "succeeded" if _steered else "exhausted", _nm)
                if not _steered:
                    return ({"role": "assistant",
                             "content": "You already ran that exact command (result above). Do NOT run "
                                        "it again — continue with the next step or report done."},
                            "stop")
            msg: Dict[str, Any] = {"role": "assistant", "content": content}
            return (({**msg, "tool_calls": tool_calls}, "tool_calls") if tool_calls else (msg, "stop"))
        except Exception as e:
            logger.warning("native-template tool path failed (%s); using fallback", e)

    # Fallback (no embedded template): plain chat completion; still parse any native tool calls.
    result = model.create_chat_completion(messages=messages, **params)
    text = (result.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    if strip_thinking:
        text = strip_thinking(text)
    content, tool_calls = parse_tool_calls(text)
    tool_calls = _normalize_tool_names(tool_calls, tools)
    msg = {"role": "assistant", "content": content}
    return (({**msg, "tool_calls": tool_calls}, "tool_calls") if tool_calls else (msg, "stop"))
