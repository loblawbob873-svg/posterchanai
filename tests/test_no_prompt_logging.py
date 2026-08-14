"""No log line may carry what a user typed.

The journal is read by the node agent, pasted into reports, shipped around in `/logs` output and
kept for as long as journald keeps anything — so a `logger.info(f"Generating: {prompt[:50]}")` is
private content in a place nobody thinks of as storage. It had spread to about thirty call sites:
every image/music/video prompt, the first chunk of every model reply, the OCR text, the full text of
every Telegram message, and — worst — the bot framework printed the final system prompt in full plus
the first 150 characters of every message on every single API call.

The diagnostics those lines existed for are all about SHAPE: did a personality load, did the history
come through, how big is this request. Sizes answer those. The text answered questions the log
should never have been able to answer at all.

This test greps rather than runs, deliberately: the leak is a *literal* in a formatting string, it
appears in code paths that need a GPU and a model to reach, and a runtime test would exercise almost
none of them. What it cannot catch is a NEW variable name spelling the same mistake — so the list
below is the set of names that have carried user content here, and adding to it is part of adding a
new one.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TREES = ("app", "botframework")

# Variables that hold something a person typed, said, or had OCR'd — or that a model said back.
CONTENT = (
    "prompt", "prompt_text", "clean_prompt", "system_prompt", "custom_system_prompt",
    "user_message", "user_content", "content_preview", "ocr_text", "reply_text",
    "source_text", "translate_messages", "query", "user_prompt", "message_text",
)

# A log/print call, and an interpolation of one of those names anywhere in it. `{prompt}`,
# `{prompt[:50]}`, `{prompt!r}` and `{x if prompt else y}` are all the same leak.
_CALL = re.compile(r"(?:logger\.\w+|logging\.\w+|print)\s*\(", re.I)
_LEAK = re.compile(r"\{[^{}]*\b(?:" + "|".join(CONTENT) + r")\b[^{}]*\}")

# `{len(prompt)}`, `{bool(result.get('prompt'))}`, `{type(user_content)}` — a measurement OF the
# content rather than the content. That is the fix, not a leak.
# The argument may itself be a call (`bool(result.get('prompt'))`), so one level of nesting is
# allowed inside the wrapper — but no more, so the pattern can't drift into matching anything.
_SIZE_ONLY = re.compile(
    r"^\{[^{}]*\b(?:len|bool|type)\s*\((?:[^()]|\([^()]*\))*\)[^{}]*\}$")

# `query` is the one name here that means two unrelated things. A SEARCH query is as private as a
# message — it is what somebody was looking for — but `Executing SQL query: {query}` is a statement
# this code wrote about its own tables, and scrubbing it would cost the only useful thing that line
# says. Nothing in the name distinguishes them, so the line's own wording does. Keep the wording
# literal: matching "sql" loosely would let a search log opt out by mentioning it.
_SQL = re.compile(r"\bSQL\b")


def _log_lines():
    """Every physical line of a log/print call in the trees, with its file and line number."""
    for tree in TREES:
        for base, dirs, files in os.walk(os.path.join(ROOT, tree)):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "node_modules", ".git")]
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(base, name)
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for n, line in enumerate(fh, 1):
                        if _CALL.search(line):
                            yield os.path.relpath(path, ROOT), n, line.rstrip("\n")


def _leaks_in(line):
    """The interpolations in `line` that carry content rather than a measurement of it."""
    if _SQL.search(line):
        return []
    return [m.group(0) for m in _LEAK.finditer(line) if not _SIZE_ONLY.match(m.group(0))]


def test_no_log_line_interpolates_user_content():
    found = [(f, n, line.strip(), bad)
             for f, n, line in _log_lines() for bad in [_leaks_in(line)] if bad]
    assert not found, "log lines carrying user content:\n" + "\n".join(
        f"  {f}:{n}  {bad}\n      {line[:150]}" for f, n, line, bad in found)


def test_the_check_can_fail():
    """The rule above is a grep, and a grep that matches nothing looks identical to a clean tree.

    So: run it against the exact shapes that were live in production, and against the fix that
    replaced them. If this stops failing on the leaks, the test above is no longer checking anything.
    """
    leaky = [
        'logger.info(f"[IMAGE-API] Generating image: {request.prompt[:50]}...")',
        'logger.info(f"Generating: {prompt[:50]}... (seed={current_seed})")',
        'print(f"  Message {i+1} ({role}): {content_preview}...")',
        'print(f"User content: {user_content[:200]}...")',
        "logger.warning(f\"TELEGRAM: text='{text}', reply_to='{reply_text[:50] if reply_text else ''}'\")",
        'print(f"[AI CLIENT] PROMPT loaded: {PROMPT[:100] if system_prompt else \'NONE\'}...")',
        'print(f"[SearXNG] Web search: {query}{scope}")',
    ]
    for line in leaky:
        assert _CALL.search(line), line
        assert _leaks_in(line), f"this leak is no longer detected: {line}"

    clean = [
        'logger.info(f"[IMAGE-API] Generating image ({len(request.prompt or \'\')} chars)")',
        'logger.info(f"Generating ({len(prompt or \'\')} chars, seed={current_seed})")',
        'print(f"User content: {len(user_content or \'\')} chars")',
        'logger.info(f"IMAGE REQUEST to {server} | prompt={len(prompt or \'\')} chars")',
        'print(f"[AI CLIENT] PROMPT loaded: {len(PROMPT) if PROMPT else 0} chars")',
        # A measurement of the content, and the SQL exception — both must stay allowed.
        'logger.info(f"[WEBSOCKET] generated_image: has_prompt={bool(result.get(\'prompt\'))}")',
        'logging.debug(f"Executing SQL query: {query}")',
    ]
    for line in clean:
        assert not _leaks_in(line), f"the fixed form is being reported as a leak: {line}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
