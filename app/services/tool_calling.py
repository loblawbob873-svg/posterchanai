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
# Nested-tag form some models emit instead of JSON/<function=> inside the <tool_call> wrapper:
# <tool_call><tool>NAME</tool><input>ARGS</input></tool_call>. Name tag: tool|tool_name|name|
# function; args tag: input|arguments|args|parameters. ARGS body is JSON or key=value pairs.
# Without this the block isn't recognized as a call and falls through as prose -> the agent
# (opencode) renders it as text and silently does nothing.
_TOOL_TAG_RE = re.compile(
    r"<tool_call>\s*<(?:tool|tool_name|name|function)>\s*(.*?)\s*</(?:tool|tool_name|name|function)>\s*"
    r"<(?:input|arguments|args|parameters)>(.*?)</(?:input|arguments|args|parameters)>\s*</tool_call>",
    re.DOTALL | re.IGNORECASE)
# Any <tool_call>...</tool_call> block (any form) - for stripping from content.
_ANY_TOOL_CALL_RE = re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL)
# Markdown-fenced JSON tool call: ```json {"name":..,"arguments":..} ``` — OpenAI-style instruct
# models (Qwen2.5-Coder-Instruct etc.) wrap the call in a code fence instead of <tool_call>/<function=>.
_MD_FENCE_RE = re.compile(r"```(?:json|tool_call|tool_code)?\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE)
# Fenced JSON block that actually carries a tool call (has a "name") - for stripping from content.
_MD_TOOL_FENCE_RE = re.compile(r"```(?:json|tool_call|tool_code)?\s*\n?\{.*?\"name\".*?\}\s*```",
                               re.DOTALL | re.IGNORECASE)


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


# Filesystem roots an absolute path can start with; used to repair a path arg the model emitted
# with the leading slash dropped ("home/u/x" -> "/home/u/x"), the common small-model slip that
# makes a glob/read/edit silently resolve against the wrong cwd and find nothing.
_ABS_ROOT_RE = re.compile(r"^(?:home|usr|etc|var|opt|tmp|root|mnt|srv|Users)/")
# Argument keys that name a filesystem path (so a content/command/pattern value that merely starts
# with "home/" is never rewritten — only genuine path args are repaired).
_PATH_KEY_RE = re.compile(r"path|dir|directory|file|cwd|filename", re.IGNORECASE)


def _repair_path_args(args: Any) -> Any:
    """Prepend the dropped leading '/' on a path-typed argument whose value clearly names an
    absolute path (starts with a real fs root). Only touches keys that name a path/dir/file, so a
    content/command/pattern value is never rewritten. No-op for anything that isn't an args dict."""
    if not isinstance(args, dict):
        return args
    for k, v in list(args.items()):
        if isinstance(v, str) and _PATH_KEY_RE.search(k) and _ABS_ROOT_RE.match(v):
            args[k] = "/" + v
    return args


# Narrate-instead-of-act detection: content that reads like the model was ABOUT to act (a botched/
# truncated tool-call tag, a trailing "next step:" colon, or an intent phrase) rather than giving a
# final answer. Used only to decide whether to regenerate forcing the call (push-to-act) — and only
# when NO call was parsed, so it can never affect a valid call.
_ORPHAN_TAG_RE = re.compile(r"</?(?:tool_call|function|parameter)\b|<function=|<parameter=", re.IGNORECASE)
_INTENT_PHRASE_RE = re.compile(
    r"(?:^|\n|[.\s])(?:let me|i'?ll|i will|i'?m going to|i am going to|i need to|i should|"
    r"now i'?m|now i'?ll|let's|first,? i|next,? i)\b", re.IGNORECASE)
# Model CLAIMS it performed a file op ("I've created the config file at /tmp/x.conf …") but emitted
# no tool call — a hallucinated completion. In an agentic turn (tools offered) that means it should
# have called write/edit and didn't, so we force the call. Matches even with a trailing question
# ("…Would you like me to install it?"), which otherwise reads as a real answer.
_CLAIMED_ACTION_RE = re.compile(
    r"\bi(?:'?ve|\s+have)?\s+(?:created|wrote|written|added|updated|saved|modified|generated|made|"
    r"configured|edited|set up)\b[^.\n]{0,80}(?:file|config|configuration|\.[A-Za-z0-9]{1,5}\b|/[\w./-]+)",
    re.IGNORECASE)


def _looks_like_intent_to_act(content: str) -> bool:
    """True if content reads like the model was about to act (or claims it did) rather than giving a
    final answer. False for a plain answer or a bare question (so push-to-act never regenerates over
    legitimate final text)."""
    if not content:
        return False
    s = content.strip()
    if _CLAIMED_ACTION_RE.search(s):  # claims a file op but emitted no call -> force the call
        return True
    if s.endswith("?"):            # a question to the user is a real answer
        return False
    if _ORPHAN_TAG_RE.search(s):   # a botched/truncated tool-call tag = it tried to call
        return True
    if s.endswith(":"):           # "Let me check X:" hand-off to an action
        return True
    return bool(_INTENT_PHRASE_RE.search(s[:180]) or _INTENT_PHRASE_RE.search(s[-180:]))


# Strong task-completion language. Used (only when the model ALREADY acted this conversation) to let
# it conclude with a final answer instead of being force-pushed into yet another redundant call —
# which otherwise loops opencode on "<none> retrying" after the work is already done.
_COMPLETION_RE = re.compile(
    r"\b(?:successfully (?:created|updated|configured|reloaded|tested|applied|completed|written|saved)|"
    r"(?:tested and reloaded|reloaded|completed|created|updated|configured) successfully|"
    r"has been (?:created|updated|tested|reloaded|configured|saved|applied|written)|"
    r"have been (?:made|applied|created|updated|saved)|"
    r"all (?:the )?changes (?:have been|are)\b|task (?:is )?complete|completed the task)\b",
    re.IGNORECASE)


def _has_prior_tool_calls(messages) -> bool:
    """True if the model already emitted a tool call earlier in THIS conversation — i.e. it has
    actually acted, so a 'I did X successfully' summary is a real conclusion, not a hallucination."""
    for m in (messages or []):
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls"):
            return True
    return False


def _mk_call(name, args, idx):
    args = _repair_path_args(args)
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


def _parse_tag_args(body: str) -> Any:
    """Decode the args body of a nested-tag tool call (the <input>/<arguments>/... payload).

    The body is either a JSON object or newline/space-separated ``key=value`` pairs (the form
    the model emits with this schema, e.g. ``pattern=Sy|=== path=foo``). A value runs until the
    next whitespace-delimited ``key=`` marker, so values may contain spaces and ``=`` (a regex,
    a path). Empty values are dropped (the model trailing off), and literals (numbers/arrays) are
    decoded via the same coercion the native ``<parameter=>`` form uses."""
    body = (body or "").strip()
    if not body:
        return {}
    for obj in _iter_json_objects(body):
        if isinstance(obj, dict):
            return obj
    args: Dict[str, Any] = {}
    markers = list(re.finditer(r"(?:^|\s)([A-Za-z_]\w*)\s*=", body))
    for i, m in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(body)
        val = body[m.end():end].strip()
        if val:
            args[m.group(1)] = _coerce_param_value(val)
    return args


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
    # 1b) Nested-tag: <tool_call><tool>NAME</tool><input>ARGS</input></tool_call>. Some models emit
    #     this instead of JSON/<function=> inside the wrapper; without it the call falls through as
    #     prose and the agent silently does nothing.
    if not tool_calls:
        for tm in _TOOL_TAG_RE.finditer(text):
            tool_calls.append(_mk_call(tm.group(1).strip(), _parse_tag_args(tm.group(2)), len(tool_calls)))
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
    # 4) Markdown-fenced / bare JSON: ```json {"name":..,"arguments":..} ``` — OpenAI-style instruct
    #    models (Qwen2.5-Coder-Instruct) fence the call instead of using <tool_call>/<function=>.
    #    Prefer fenced blocks (don't grab a JSON example buried in prose); fall back to whole text.
    if not tool_calls:
        fenced = _MD_FENCE_RE.findall(text)
        for src in (fenced or [text]):
            for obj in _iter_json_objects(src):
                if isinstance(obj, dict) and obj.get("name") and ("arguments" in obj or "parameters" in obj):
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
    content = _FUNC_CALLS_RE.sub("", content)
    content = _MD_TOOL_FENCE_RE.sub("", content).strip()
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


def _inject_no_think_system(msgs: list) -> list:
    """Append ' /no_think' to the system turn (or prepend one), so a non-thinking model SKIPS the
    template's <think> prefill. Mutates and returns msgs."""
    for _m in msgs:
        if _m.get("role") == "system":
            if "/no_think" not in (_m.get("content") or ""):
                _m["content"] = ((_m.get("content") or "") + " /no_think").strip()
            return msgs
    msgs.insert(0, {"role": "system", "content": "/no_think"})
    return msgs


def generate_message(model, messages, tools, params, strip_thinking=None, disable_thinking=False) -> Tuple[Dict[str, Any], str]:
    """Generate + parse a tool-aware response using the model's embedded chat template.

    Returns (openai_message_dict, finish_reason). ``params`` must NOT contain tools/tool_choice.
    Falls back to a plain chat completion only if the model has no embedded template.

    ``disable_thinking``: inject /no_think on the FIRST render so a non-thinking model (e.g.
    Qwen3-Coder) skips the template's <think> prefill — otherwise it opens <think>, never closes it
    ("empty tool generation, think never closed"), and we burn a whole pass before the /no_think
    retry. Skipping it up front turns the common case from 3 LLM passes per tool call into 1.
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
            _msgs0 = _prep_for_template(messages)
            if disable_thinking:
                _inject_no_think_system(_msgs0)
            r = _get_formatter(template)(messages=_msgs0, tools=tools)
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
            # An empty result (no tool_call, no content) -> empty message -> the LB reads the node as
            # dead and aborts the already-streamed SSE -> opencode HARD-stops. It has TWO causes, both
            # fixed the same way: (a) the model closed </think> then ended the turn without an answer/
            # tool_call (lazy end-of-turn); (b) the model ran away thinking and NEVER closed </think>,
            # blowing its whole token budget on reasoning before emitting the tool_call (raw is all
            # reasoning — the len=6088 "tried to make a fix but then just stopped" case). Re-render
            # with /no_think injected into the system turn so the model SKIPS the think block and spends
            # its budget on the actual tool_call instead of thinking-then-quitting (or thinking forever).
            # (Prompt surgery to close the think block alone does NOT work — without the /no_think signal
            # the model still quits.) Previously only (a) retried; (b) fell straight to the reasoning-as-
            # content band-aid, so the agent got prose and no action — what the user saw as "it stopped".
            if not tool_calls and not content:
                if "</think>" not in raw:
                    logger.warning("empty tool generation (think never closed, len=%d); retrying /no_think",
                                   len(raw))
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
            # Minimal push-to-act (narrate-instead-of-act): the model produced PROSE that reads like
            # it was about to act (intent phrase / "next step:" / a botched-truncated tool-call tag)
            # but emitted NO parseable call -> opencode reads content+stop as a final answer and HALTS.
            # Regenerate ONCE forcing the call (/no_think + an explicit "emit ONLY the tool call now").
            # STRICTLY ADDITIVE & cannot repeat the 8f2c6c8d regression: it runs ONLY when no call was
            # parsed (never touches a valid call) and KEEPS the original content if the regen still
            # yields nothing — so it can only ADD a recovered action, never make things worse.
            # Conclusion guard: if the model ALREADY acted this turn-chain and is now summarizing
            # completion ("…reloaded successfully" ± a dangling empty <tool_call>), let it FINISH —
            # return the summary as the final answer instead of force-pushing another redundant call,
            # which loops opencode on "<none> retrying" after the job is done. Only applies when it
            # actually acted; a bare claim with no prior tool call still gets forced below.
            if (tools and content and not tool_calls
                    and _has_prior_tool_calls(messages) and _COMPLETION_RE.search(content)):
                _stripped = _ORPHAN_TAG_RE.sub("", content).rstrip(" >\n\t").strip()  # drop dangling <tool_call>
                content = _stripped or content
                logger.info("task-completion summary after prior action — returning final answer "
                            "(no forced call), len=%d", len(content))
            elif tools and content and not tool_calls and _looks_like_intent_to_act(content):
                logger.warning("narration-only intent-to-act — pushing for the tool call (len=%d); content[:500]=%r",
                               len(content), content[:500])
                try:
                    push = ("You started to act but did NOT emit a complete tool call. Emit the tool "
                            "call NOW and output ONLY the tool call — no explanation, no prose. /no_think")
                    nt = [dict(m) for m in _prep_for_template(messages)]
                    for _m in nt:
                        if _m.get("role") == "system":
                            _m["content"] = ((_m.get("content") or "") + "\n" + push).strip()
                            break
                    else:
                        nt.insert(0, {"role": "system", "content": push})
                    r3 = _get_formatter(template)(messages=nt, tools=tools)
                    # PREFILL the response with a closed think + an open <tool_call>, forcing the model
                    # to CONTINUE a tool call (filling in the args it already worked out) instead of
                    # restarting with prose. The template opens the assistant turn inside <think>, so
                    # close it first. The model continues with the call body; we prepend the tag back.
                    prompt3 = r3.prompt
                    if prompt3.rstrip().endswith("<think>"):
                        prompt3 = prompt3.rstrip()[: -len("<think>")] + "<think></think>\n\n<tool_call>\n"
                    else:
                        prompt3 = prompt3 + "\n<tool_call>\n"
                    toks3 = model.tokenize(prompt3.encode("utf-8"), add_bos=False, special=True)
                    raw3 = (model.create_completion(prompt=toks3, **_p).get("choices") or [{}])[0].get("text") or ""
                    # Reconstruct the full call: the prefilled "<tool_call>" + the model's continuation.
                    c3, tc3 = parse_tool_calls("<tool_call>\n" + raw3)
                    tc3 = _normalize_tool_names(tc3, tools)
                    if tc3:
                        logger.warning("push-to-act succeeded (%s)",
                                       ",".join(t["function"]["name"] for t in tc3))
                        content, tool_calls = c3, tc3   # now acting; the obsolete narration is dropped
                    else:
                        # DIAG: capture what the model actually emitted so we can tell genuine prose
                        # from a malformed/unparsed tool call (fixable in parse_tool_calls).
                        logger.warning("push-to-act produced no call; keeping original content. "
                                       "body3[:500]=%r", (body3 or "")[:500])
                except Exception as e:
                    logger.warning("push-to-act failed (%s); keeping original content", e)
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
            # DIAG: when we return WITHOUT a tool call but tools were offered, log what we're handing
            # opencode (prose vs a malformed Write call) so the right fix is clear.
            if tools and not tool_calls:
                logger.warning("RETURNING NO TOOL CALL with tools present; content[:600]=%r", (content or "")[:600])
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


def reset_context_if_needed(model) -> None:
    """No-op shim: the per-gen SYCL context reset is disabled (LLM_RESET_EACH_GEN=0) on the
    2025.2-rebuilt Arc, which no longer has the broadcast-crash. Kept for import compatibility."""
    return None
