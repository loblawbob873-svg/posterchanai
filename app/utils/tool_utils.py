"""Shared tool-calling utilities for anthropic_api.py and openai_api.py.

Constants, path helpers, redirect rewriting, schema compaction, model resolution,
and the 14-rule tool system prompt all live here so they only need editing once.
"""
import json
import os
import re
from typing import Any, Dict, List, Optional

# ── Tag constants ─────────────────────────────────────────────────────────────

TOOL_CALL_OPEN = "<tool_call>"
TOOL_CALL_CLOSE = "</tool_call>"

TOOL_ONLY_RE = re.compile(
    r"^\s*(?:" + re.escape(TOOL_CALL_OPEN) + r".*?" + re.escape(TOOL_CALL_CLOSE) + r"\s*)+$",
    re.DOTALL,
)

# ── JSON repair helpers ───────────────────────────────────────────────────────

# Greedy patterns: capture a string value field up to the JSON object close.
# Used when json.loads fails due to unescaped " in file content or command strings.
# The trailing group allows zero or more simple string fields after the target field.
_SCALAR_VALUE = r'(?:"[^"]*"|-?\d+(?:\.\d+)?|true|false|null)'
_REPAIR_CONTENT_RE = re.compile(
    r'"content"\s*:\s*"(.*?)"(?:\s*(?:,\s*"[a-zA-Z_]+"\s*:\s*' + _SCALAR_VALUE + r'\s*)*)\s*\}\s*\}\s*$',
    re.DOTALL,
)
_REPAIR_COMMAND_RE = re.compile(
    r'"command"\s*:\s*"(.*?)"(?:\s*(?:,\s*"[a-zA-Z_]+"\s*:\s*' + _SCALAR_VALUE + r'\s*)*)\s*\}\s*\}\s*$',
    re.DOTALL,
)
# Fallback patterns for TRUNCATED JSON (model hit max_tokens before closing }}).
# These are unanchored — they match whatever content is present to end-of-string.
_REPAIR_CONTENT_TRUNCATED_RE = re.compile(r'"content"\s*:\s*"(.*)', re.DOTALL)
_REPAIR_COMMAND_TRUNCATED_RE = re.compile(r'"command"\s*:\s*"(.*)', re.DOTALL)

# Edit/Update greedy patterns: old_string anchored to new_string key; new_string anchored to }}.
_REPAIR_OLD_STR_RE = re.compile(
    r'"old_string"\s*:\s*"(.*?)"\s*,\s*["\']new_string',
    re.DOTALL,
)
_REPAIR_NEW_STR_RE = re.compile(
    r'"new_string"\s*:\s*"(.*?)"(?:\s*(?:,\s*"[a-zA-Z_]+"\s*:\s*' + _SCALAR_VALUE + r'\s*)*)\s*\}\s*\}\s*$',
    re.DOTALL,
)
# Truncated variants (unanchored, for incomplete JSON).
_REPAIR_OLD_STR_TRUNC_RE = re.compile(r'"old_string"\s*:\s*"(.*?)",\s*"new_string', re.DOTALL)
_REPAIR_NEW_STR_TRUNC_RE = re.compile(r'"new_string"\s*:\s*"(.*)', re.DOTALL)


def sanitize_json_control_chars(s: str) -> str:
    """Escape literal control characters that appear inside JSON string values.

    The model sometimes emits actual newlines/tabs inside JSON strings instead
    of the required \\n / \\t escape sequences, producing invalid JSON.
    This walks the string character-by-character so it only touches chars
    that are genuinely inside a string value, not JSON structural whitespace.
    """
    out: list[str] = []
    in_str = False
    escaped = False
    for c in s:
        if escaped:
            out.append(c)
            escaped = False
        elif c == '\\' and in_str:
            out.append(c)
            escaped = True
        elif c == '"':
            out.append(c)
            in_str = not in_str
        elif in_str and c == '\n':
            out.append('\\n')
        elif in_str and c == '\r':
            out.append('\\r')
        elif in_str and c == '\t':
            out.append('\\t')
        elif in_str and ord(c) < 0x20:
            out.append(f'\\u{ord(c):04x}')
        else:
            out.append(c)
    return ''.join(out)


def _decode_json_str_escapes(s: str) -> str:
    """Convert JSON string escape sequences to their actual characters."""
    s = s.replace('\\\\', '\x00')   # protect \\  first
    s = s.replace('\\"', '"')
    s = s.replace('\\n', '\n')
    s = s.replace('\\r', '\r')
    s = s.replace('\\t', '\t')
    s = s.replace('\\/', '/')
    s = s.replace('\x00', '\\')
    # Strip bare control characters (0x00–0x1F except tab/newline/CR) that the
    # model sometimes embeds literally, causing downstream Write tool rejections.
    return ''.join(c if c >= ' ' or c in '\n\r\t' else '' for c in s)


# Trailing JSON field(s) appended when the greedy unanchored regex overshoots the content
# closing quote. Patterns: `\n", "field": "value"}}` or `\n", "field": "val"}` (truncated).
# Uses \}+ (one or more) to handle both complete JSON (}}) and truncated JSON (single }).
# False-match risk is low: in properly-escaped JSON, " inside content is \", not bare ".
_JSON_TRAILING_FIELD_RE = re.compile(
    r'(?:\\n)?"(?:,\s*"[a-zA-Z_]+"\s*:\s*(?:"[^"\\]*"|-?\d+(?:\.\d+)?|true|false|null))*\s*\}+\s*$'
)


def _strip_json_tail(s: str) -> str:
    """Strip trailing JSON closing chars/fields captured by greedy unanchored regex.

    The unanchored regex grabs everything from "content"/"command": " to end of raw,
    which may include trailing `, "file_path": "dedup.py"}}` or just `"\\n}`.

    Order in the fallback path is critical: the model often places a real newline
    between the closing `}` and the content string's closing `"`, so we must strip
    whitespace AFTER removing the outer `}` and BEFORE stripping the `"`.
    """
    m = _JSON_TRAILING_FIELD_RE.search(s)
    if m and m.start() > 0:
        return s[:m.start()]
    # Fallback: strip JSON-close characters from the right.
    s = s.rstrip()     # ① trailing whitespace
    s = s.rstrip('}')  # ② outer JSON object close (or arguments close)
    s = s.rstrip()     # ③ real newline between } and closing " — must come before ④
    s = s.rstrip('"')  # ④ content/command string's closing quote
    s = s.rstrip('}')  # ⑤ inner dict close if present (single-nesting)
    return s.rstrip()  # ⑥ final cleanup


def repair_tool_json(raw: str) -> Optional[Dict[str, Any]]:
    """
    Repair malformed tool-call JSON where a string value contains unescaped double quotes
    OR where the JSON is truncated (model hit max_tokens before closing the object).

    Strategy:
    1. Try anchored greedy regex (requires closing }}) — handles unescaped " cases.
    2. If that fails, try unanchored regex (no }} required) — handles truncated JSON.
       The extracted content may be incomplete but is better than dropping the call.

    Returns a dict with 'name' and 'arguments' keys, or None if repair fails.
    """
    # Strip trailing punctuation the model sometimes appends after }}: e.g. `}},`
    # This turns `}},` into `}}` so the anchored regex can match cleanly.
    raw = raw.rstrip().rstrip(',;').rstrip()

    name_m = re.search(r'"name"\s*:\s*"(\w+)"', raw)
    if not name_m:
        return None
    name = name_m.group(1)

    def _extract_file_path(raw: str):
        """Return (key, value) preserving the original key name, or (None, None)."""
        m = re.search(r'"(file_path|filePath|path)"\s*:\s*"([^"]*)"', raw)
        return (m.group(1), m.group(2)) if m else (None, None)

    def _extract_cmd_extras(raw: str) -> Dict[str, Any]:
        extras: Dict[str, Any] = {}
        m = re.search(r'"description"\s*:\s*"([^"]*)"', raw)
        if m:
            extras["description"] = _decode_json_str_escapes(m.group(1))
        m = re.search(r'"timeout"\s*:\s*(-?\d+(?:\.\d+)?)', raw)
        if m:
            extras["timeout"] = int(float(m.group(1)))
        return extras

    # ── Anchored pass (complete JSON, unescaped " inside string) ──────────────
    content_m = _REPAIR_CONTENT_RE.search(raw)
    if content_m:
        content = _decode_json_str_escapes(content_m.group(1))
        args: Dict[str, Any] = {"content": content}
        fp_key, fp_val = _extract_file_path(raw)
        if fp_key:
            args[fp_key] = fp_val
        return {"name": name, "arguments": args}

    cmd_m = _REPAIR_COMMAND_RE.search(raw)
    if cmd_m:
        args = {"command": _decode_json_str_escapes(cmd_m.group(1)), **_extract_cmd_extras(raw)}
        return {"name": name, "arguments": args}

    # ── Truncated pass (JSON cut off mid-string by max_tokens) ────────────────
    content_t = _REPAIR_CONTENT_TRUNCATED_RE.search(raw)
    if content_t:
        content = _decode_json_str_escapes(_strip_json_tail(content_t.group(1)))
        args = {"content": content}
        fp_key, fp_val = _extract_file_path(raw)
        if fp_key:
            args[fp_key] = fp_val
        return {"name": name, "arguments": args}

    cmd_t = _REPAIR_COMMAND_TRUNCATED_RE.search(raw)
    if cmd_t:
        args = {"command": _decode_json_str_escapes(_strip_json_tail(cmd_t.group(1))), **_extract_cmd_extras(raw)}
        return {"name": name, "arguments": args}

    # ── Edit/Update: extract old_string + new_string ──────────────────────────
    old_m = _REPAIR_OLD_STR_RE.search(raw)
    new_m = _REPAIR_NEW_STR_RE.search(raw)
    if old_m and new_m:
        args = {
            "old_string": _decode_json_str_escapes(old_m.group(1)),
            "new_string": _decode_json_str_escapes(new_m.group(1)),
        }
        fp_key, fp_val = _extract_file_path(raw)
        if fp_key:
            args[fp_key] = fp_val
        return {"name": name, "arguments": args}

    old_t = _REPAIR_OLD_STR_TRUNC_RE.search(raw)
    new_t = _REPAIR_NEW_STR_TRUNC_RE.search(raw)
    if old_t and new_t:
        args = {
            "old_string": _decode_json_str_escapes(old_t.group(1)),
            "new_string": _decode_json_str_escapes(_strip_json_tail(new_t.group(1))),
        }
        fp_key, fp_val = _extract_file_path(raw)
        if fp_key:
            args[fp_key] = fp_val
        return {"name": name, "arguments": args}

    return None

# ── Path / sudo helpers ───────────────────────────────────────────────────────

HOME_DIR = os.path.expanduser("~")

NEVER_WRITE_EXACT = frozenset([
    "/etc/machine-id", "/etc/passwd", "/etc/shadow", "/etc/sudoers",
    "/etc/fstab", "/etc/crypttab", "/etc/group", "/etc/gshadow",
    "/etc/hostname", "/etc/hosts",
])
NEVER_WRITE_PREFIXES_BLOCK = ("/etc/sudoers.d/", "/etc/ssh/ssh_host", "/etc/ssl/private", "/boot/")



MAX_TOOL_ITERATIONS = 12

_CWD_RE = re.compile(
    r'(?:working directory|cwd|current directory|pwd)[:\s]+(/[^\s\n<>]+)',
    re.IGNORECASE,
)

def extract_cwd_from_system(system_text: str) -> str:
    """Extract the client's working directory from its system prompt, or return ''."""
    if not system_text:
        return ""
    m = _CWD_RE.search(system_text)
    return m.group(1).rstrip("/") if m else ""


def needs_sudo(fp: str) -> bool:
    """Return True when fp is an absolute path outside the user's home directory."""
    if not fp.startswith("/"):
        return False
    return not fp.startswith(HOME_DIR + "/") and fp != HOME_DIR


# ── Shell redirect rewriting ──────────────────────────────────────────────────
# Shell `>` / `>>` always run as the invoking user, even when the command is
# prefixed with sudo.  Rewrite `cmd > /system/path` → `cmd | sudo tee /system/path`
# so the write actually runs as root.

_CAT_REDIR_RE = re.compile(r'(?:sudo\s+)?cat\s+(>>?)\s+(/\S+)', re.MULTILINE)
_CMD_REDIR_RE = re.compile(
    r'((?:sudo\s+)?\S+(?:\s+[^>|&\n]+?)?)\s+(>>?)\s+(/\S+)',
    re.MULTILINE,
)


def fix_redirects(cmd: str) -> str:
    """Rewrite `cmd > /outside-home/path` to use `sudo tee` so the write runs as root."""
    def _cat_sub(m: re.Match) -> str:
        path = m.group(2)
        if not needs_sudo(path):
            return m.group(0)
        append = m.group(1) == ">>"
        return f"sudo tee {'-a ' if append else ''}{path}"

    def _cmd_sub(m: re.Match) -> str:
        path = m.group(3)
        if not needs_sudo(path):
            return m.group(0)
        src = m.group(1).strip()
        append = m.group(2) == ">>"
        words = src.split()
        first_word = words[1] if len(words) > 1 and words[0] == "sudo" else (words[0] if words else "")
        if first_word == "tee":
            return f"sudo tee {'-a ' if append else ''}{path}"
        return f"{src} | sudo tee {'-a ' if append else ''}{path}"

    result = _CAT_REDIR_RE.sub(_cat_sub, cmd)
    result = _CMD_REDIR_RE.sub(_cmd_sub, result)
    return result


# ── Schema / model helpers ────────────────────────────────────────────────────

def compact_schema(schema: Any) -> Dict[str, Any]:
    """Strip verbose parameter descriptions — keep only type+required to save tokens."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    props = schema.get("properties") or {}
    compact = {
        k: {"type": v.get("type", "string") if isinstance(v, dict) else "string"}
        for k, v in props.items()
    }
    out: Dict[str, Any] = {"type": "object", "properties": compact}
    if schema.get("required"):
        out["required"] = schema["required"]
    return out


def resolve_model_path(model_name: str, settings: dict) -> Optional[str]:
    """If model_name is a local GGUF filename, return its full path; else None."""
    if not model_name or not model_name.endswith(".gguf"):
        return None
    llm_path = settings.get("llm_model_path", "")
    if not llm_path:
        return None
    candidate = os.path.join(os.path.dirname(llm_path), model_name)
    return candidate if os.path.isfile(candidate) else None


# ── Tool output truncation ───────────────────────────────────────────────────

_TOOL_OUTPUT_MAX = 8_000    # chars; outputs larger than this get head+tail trimmed
_TOOL_OUTPUT_HEAD = 6_000   # chars kept from the start
_TOOL_OUTPUT_TAIL = 1_500   # chars kept from the end


def truncate_tool_output(text: str) -> str:
    """Trim large tool outputs so they don't blow up the context window.

    Keeps the first _TOOL_OUTPUT_HEAD chars and the last _TOOL_OUTPUT_TAIL chars,
    inserting a notice in between.  Output ≤ _TOOL_OUTPUT_MAX is returned as-is.
    """
    if len(text) <= _TOOL_OUTPUT_MAX:
        return text
    dropped = len(text) - _TOOL_OUTPUT_HEAD - _TOOL_OUTPUT_TAIL
    return (
        text[:_TOOL_OUTPUT_HEAD]
        + f"\n[... {dropped:,} characters of output omitted to save context — command completed, showing start and end only. Do NOT retry this command.]\n"
        + text[-_TOOL_OUTPUT_TAIL:]
    )


# ── Tools system prompt ───────────────────────────────────────────────────────

_TOOL_RULES = (
    "IMPORTANT RULES:\n"
    "1. NEVER guess, fabricate, or hallucinate tool outputs or file contents.\n"
    "2. Do NOT use <think> tags or output any reasoning. Call the tool immediately.\n"
    "3. Output ONLY the <tool_call> block — no preamble, no description, no explanation.\n"
    "4. After each tool result, decide: is the user's original request FULLY satisfied "
    "(every file created, every command succeeded with exit 0)? If YES, output a plain-text "
    "summary and STOP. If NO, call the next required tool immediately. Multi-step tasks "
    "(e.g. systemd timers: write .service, write .timer, daemon-reload, enable, start) are "
    "NOT done after just one step — keep going until ALL steps succeed.\n"
    "5. NEVER repeat a tool call that already succeeded. Each tool call must serve a new, distinct purpose.\n"
    "6. If a tool returns an error or non-zero exit code: identify WHY it failed, fix the root "
    "cause, then run again. NEVER re-run the exact same failing command unchanged. "
    "If the error is 'Permission denied' or 'Operation not permitted', prepend `sudo` and retry "
    "— no need to ask. If a command references a file that doesn't exist, create it first.\n"
    "7. NEVER fabricate tool output. Never print what a command 'would' output.\n"
    "8. NEVER declare success unless the most recent tool result explicitly confirms it. "
    "'(success — no output)' from Write means the file was written — continue with the next step. "
    "An error or non-zero exit code means keep fixing — do NOT summarize as done.\n"
    f"9. When creating project files with Write, use RELATIVE paths (e.g. 'hello.py', 'src/main.py'). "
    f"Only use absolute paths for system files under /etc/, /usr/, /run/, etc. "
    f"systemd unit files: system-wide units go in /etc/systemd/system/ and are managed with "
    f"`sudo systemctl`; per-user units go in {HOME_DIR}/.config/systemd/user/ "
    f"(NEVER /etc/systemd/user/) and are managed with `systemctl --user` (no sudo). "
    f"The home directory is {HOME_DIR} — NEVER use ~ or placeholder words like 'username' or 'user' in paths. "
    f"In ALL Bash commands, replace ~ with the full path {HOME_DIR} "
    f"(e.g. use '{HOME_DIR}/Music' not '~/Music', use '{HOME_DIR}/Documents' not '~/Documents').\n"
    f"10. You have a maximum of {MAX_TOOL_ITERATIONS} total tool calls per task. Use them efficiently.\n"
    "11. Planning tools (TodoWrite, TodoRead) do NOT count as progress. After any planning tool, "
    "immediately call the next action tool (Write, Bash, etc.).\n"
    '12. NEVER output markdown code blocks (``` ... ```) as a substitute for tool calls. '
    'NEVER output bare JSON like {"title": ...}. If you need to run code or write a file, '
    "use a <tool_call> block — always.\n"
    "13. NEVER write 'Files created:', 'Commands executed:', or any completion summary until "
    "every required Bash command has returned exit 0 via a tool result. Saying you did something "
    "is NOT the same as doing it.\n"
    f"14. For systemd unit file setup use exactly 3 tool calls and NO MORE: "
    f"(1) Write the .service file to the absolute path {HOME_DIR}/.config/systemd/user/NAME.service. "
    f"(2) Write the .timer file to the absolute path {HOME_DIR}/.config/systemd/user/NAME.timer. "
    f"(3) One Bash call: mkdir -p {HOME_DIR}/.config/systemd/user && systemctl --user daemon-reload "
    f"&& systemctl --user enable --now NAME.timer && systemctl --user status NAME.timer. "
    "NEVER use bash heredocs to write unit files — use the Write tool. "
    "NEVER combine file writes with systemctl in one Bash call.\n"
    "15. For Python files (.py): use 4 spaces per indent level. NEVER use tab characters (\\t) for indentation — "
    "Python 3 rejects mixed tabs and spaces and will throw IndentationError.\n"
    "16. If Edit or Update fails with 'oldString not found', 'Error editing file', or 'Could not find': "
    "DO NOT retry with the same old_string. Call Read on that file first to see its current content, "
    "then craft a new Edit using the exact text you find in the file."
)


def build_tools_system_prompt(tool_entries: List[str], working_dir: str = "") -> str:
    """Build the complete tools system prompt from a list of pre-serialized tool JSON strings.

    Each entry is a JSON object string describing one tool in OpenAI function format.
    Callers are responsible for converting their tool format (Anthropic / OpenAI) into
    these entries before calling this function.
    """
    tools_block = "\n".join(tool_entries)
    working_dir_rule = ""
    if working_dir:
        working_dir_rule = (
            f"\n17. Your working directory for this session is: {working_dir}\n"
            f"    ALL files you Write or create MUST use the absolute path {working_dir}/FILENAME.\n"
            f"    ALL Bash commands that reference project files MUST use the absolute path {working_dir}/FILENAME.\n"
            f"    NEVER write files to any other directory (not ~, not /tmp, not /home/verita84, not /home/verita84/Music, etc.).\n"
            f"    If you need to run a script you wrote, run it as: python3 {working_dir}/script.py\n"
        )
    return (
        "# Tools\n\n"
        "You may call one or more functions to assist with the user query.\n\n"
        "You are provided with function signatures within <tools></tools> XML tags:\n"
        "<tools>\n"
        f"{tools_block}\n"
        "</tools>\n\n"
        "For each function call, return a json object with function name and arguments "
        "within <tool_call></tool_call> XML tags:\n"
        "<tool_call>\n"
        '{"name": <function-name>, "arguments": <args-json-object>}\n'
        "</tool_call>\n\n"
        + _TOOL_RULES
        + working_dir_rule
    )
