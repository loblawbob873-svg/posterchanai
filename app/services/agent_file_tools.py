"""Structured file tools for the node agent (read / write / edit / grep).

WHY these exist as tools rather than shell. The agent used to have exactly one tool,
`run_command`, so every file operation had to be expressed as shell — `cat > f << 'EOF' …`.
That is the single biggest source of wasted steps: quoting and heredoc bodies go wrong
silently, and a model that cannot see WHY its write did not take just writes it again with
the body tweaked. `_cmd_signature` in node_service exists precisely because of one such run
(20 steps rewriting one script). An `edit_file` that matches an exact string and FAILS LOUDLY
when the match is missing or ambiguous removes that whole class of spin.

TRANSPORT. These do NOT touch the controller's filesystem. They ride the same executor the
agent's shell commands do (`run_to_completion` → host subprocess / `docker exec` into the
user's sandbox / a Nostr worker's own local job), so a file tool always acts on the machine
the agent is managing. That is why the work is done by a small Python program shipped TO the
target rather than by `open()` here.

QUOTING. The program and its JSON argument are both base64-encoded into

    python3 -c "$(printf %s '<prog>' | base64 -d)" '<payload>'

so neither the program's own quoting nor the user's file content is ever parsed by the shell
— base64 is `[A-Za-z0-9+/=]`, which is inert inside single quotes, and the output of `$(…)`
inside double quotes is not rescanned for expansions. This is the one thing that must stay
true if anyone edits the wrapper: no untrusted text may reach the command line unencoded.
"""
import base64
import json
from typing import Any, Awaitable, Callable, Optional

# Reject a write whose payload would make a command line the kernel refuses to exec.
#
# THE BOUND IS NOT ARG_MAX. It is MAX_ARG_STRLEN — 32 pages, 128 KiB on every 4 KiB-page Linux —
# which caps a SINGLE argv string, and the whole `sh -c "<command>"` is one such string. ARG_MAX
# (~2 MB) is the total across all arguments and never applies here.
#
# This was 512 KiB, chosen against ARG_MAX, and MEASURED it does not work: base64 inflates the
# payload by 4/3, so ~95 KB of content already builds a 134 KB command and exec fails with E2BIG.
# Everything from ~92 KB to 512 KB passed this check and then died as `Argument list too long` —
# the exact "raw shell error it will try to fix by editing a file" the JSON protocol below exists
# to keep away from the model.
#
# 64 KiB of content is a ~95 KB command: comfortably inside the limit, with room for `docker exec`
# overhead and for platforms whose page size makes the real ceiling lower than measured here. Still
# well above any sane source file. tests/test_agent_file_tools.py execs a write at exactly this cap.
_MAX_WRITE_BYTES = 64 * 1024

# The worker program. Runs on the TARGET, prints one JSON object on stdout. Deliberately
# contains no single quotes (it is base64-encoded anyway, but keeping it quote-free means the
# wrapper stays safe if it is ever changed to interpolate directly).
_FILE_OP_PY = r'''
import sys, os, json, base64

def out(d):
    sys.stdout.write(json.dumps(d))
    sys.exit(0)

try:
    req = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
except Exception as e:
    out({"ok": False, "error": "bad request: %s" % e})

op = req.get("op") or ""
path = req.get("path") or ""

def read_text(p):
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        return f.read()

def numbered(lines, start):
    return "".join("%6d\t%s\n" % (start + i, l) for i, l in enumerate(lines))

try:
    if op == "read":
        if not os.path.exists(path):
            out({"ok": False, "error": "no such file: %s" % path})
        if os.path.isdir(path):
            out({"ok": False, "error": "%s is a directory, not a file" % path})
        off = int(req.get("offset") or 1)
        lim = int(req.get("limit") or 400)
        if off < 1:
            off = 1
        if lim < 1:
            lim = 400
        lines = read_text(path).splitlines()
        total = len(lines)
        sel = lines[off - 1: off - 1 + lim]
        body = numbered(sel, off)
        shown = (off - 1) + len(sel)
        if not sel:
            body = "(no lines at offset %d; the file has %d line(s))\n" % (off, total)
        elif shown < total:
            body += "... %d more line(s). Read again with offset=%d to continue.\n" % (total - shown, shown + 1)
        out({"ok": True, "content": body})

    elif op == "write":
        content = req.get("content")
        if content is None:
            out({"ok": False, "error": "write needs content"})
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        existed = os.path.exists(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        out({"ok": True, "content": "%s %s (%d bytes, %d lines)" % (
            "overwrote" if existed else "created", path,
            len(content.encode("utf-8")), len(content.splitlines()))})

    elif op == "edit":
        if not os.path.exists(path):
            out({"ok": False, "error": "no such file: %s" % path})
        old = req.get("old_string")
        new = req.get("new_string")
        if old is None or new is None:
            out({"ok": False, "error": "edit needs old_string and new_string"})
        if old == new:
            out({"ok": False, "error": "old_string and new_string are identical - nothing to do"})
        if old == "":
            out({"ok": False, "error": "old_string is empty; use write to create a file"})
        src = read_text(path)
        n = src.count(old)
        if n == 0:
            out({"ok": False, "error": "old_string was NOT found in %s. It must match the file "
                 "byte for byte, including indentation and blank lines. Read the file and copy "
                 "the text verbatim - do not retype it from memory." % path})
        all_ = bool(req.get("replace_all"))
        if n > 1 and not all_:
            out({"ok": False, "error": "old_string matches %d places in %s. Include more "
                 "surrounding lines so it matches exactly once, or pass replace_all=true." % (n, path)})
        dst = src.replace(old, new) if all_ else src.replace(old, new, 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(dst)
        # Echo the changed region back so the model can SEE the applied result and does not
        # need a second read to believe the edit landed.
        idx = dst.find(new)
        first = dst[:idx].count("\n") if idx >= 0 else 0
        lines = dst.splitlines()
        lo = max(0, first - 3)
        hi = min(len(lines), first + new.count("\n") + 4)
        out({"ok": True, "content": "replaced %d occurrence(s) in %s\n%s" % (
            n if all_ else 1, path, numbered(lines[lo:hi], lo + 1))})

    elif op == "grep":
        import re, fnmatch
        pat = req.get("pattern") or ""
        glob = req.get("glob") or ""
        root = path or "."
        try:
            rx = re.compile(pat)
        except Exception as e:
            out({"ok": False, "error": "bad regular expression: %s" % e})
        if not os.path.exists(root):
            out({"ok": False, "error": "no such path: %s" % root})
        files = []
        if os.path.isfile(root):
            files = [root]
        else:
            skip = set([".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache"])
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in skip]
                for fn in filenames:
                    files.append(os.path.join(dirpath, fn))
        cap = 200
        hits = []
        for fp in files:
            if glob and not fnmatch.fnmatch(os.path.basename(fp), glob):
                continue
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if rx.search(line):
                            hits.append("%s:%d:%s" % (fp, i, line.rstrip()[:300]))
                            if len(hits) >= cap:
                                break
            except Exception:
                continue
            if len(hits) >= cap:
                break
        body = "\n".join(hits) if hits else "(no matches)"
        if len(hits) >= cap:
            body += "\n... capped at %d matches - narrow the pattern, path or glob." % cap
        out({"ok": True, "content": body})

    else:
        out({"ok": False, "error": "unknown op: %s" % op})
except Exception as e:
    out({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
'''

_PROG_B64 = base64.b64encode(_FILE_OP_PY.encode("utf-8")).decode("ascii")


def _command_for(payload: dict) -> str:
    """The shell command that runs one file op on the TARGET. Both the program and the payload
    are base64, so nothing in the file content is ever seen by the shell (see the module note)."""
    arg = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return f"python3 -c \"$(printf %s '{_PROG_B64}' | base64 -d)\" '{arg}'"


# OpenAI-style schemas, appended to the agent's tool list. Descriptions carry the usage rules
# the model must follow (read before edit, exact matching) — the system prompt repeats the
# important ones, but a model that only reads schemas still gets them here.
FILE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file from the managed host, with line numbers. Prefer this "
                           "over `cat` — the line numbers are what edit_file and grep results refer to. "
                           "Large files are paged: read again with a higher offset to continue.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file."},
                    "offset": {"type": "integer", "description": "1-based first line to read (default 1)."},
                    "limit": {"type": "integer", "description": "How many lines to read (default 400)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Change part of an existing file by replacing an exact string. You MUST "
                           "read_file first — old_string has to match the file byte for byte, including "
                           "indentation. It must match exactly one place unless replace_all is true; if "
                           "it is ambiguous, include more surrounding lines rather than guessing. This "
                           "is the correct way to modify a file — do NOT rewrite a whole file with shell "
                           "redirection to change a few lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file."},
                    "old_string": {"type": "string", "description": "Exact text to replace, copied verbatim from read_file output (WITHOUT the line-number column)."},
                    "new_string": {"type": "string", "description": "The replacement text."},
                    "replace_all": {"type": "boolean", "description": "Replace every occurrence instead of requiring a unique match."},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new file, or replace one wholesale, with the given content. Missing "
                           "parent directories are created. Use edit_file to change part of an existing "
                           "file — this OVERWRITES it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file."},
                    "content": {"type": "string", "description": "The full file content."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents under a path with a regular expression, returning "
                           "file:line:text matches. Use this to find where something is defined or used.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regular expression."},
                    "path": {"type": "string", "description": "File or directory to search (default: current directory)."},
                    "glob": {"type": "string", "description": "Only search files whose NAME matches this glob, e.g. '*.py'."},
                },
                "required": ["pattern"],
            },
        },
    },
]

FILE_TOOL_NAMES = {t["function"]["name"] for t in FILE_TOOLS}
# Ops that only look. They are cheap and safe to repeat (paging a big file is several reads of
# the same path), so the agent's repeat/loop breakers deliberately ignore them.
READ_ONLY_TOOLS = {"read_file", "grep"}

_OPS = {"read_file": "read", "edit_file": "edit", "write_file": "write", "grep": "grep"}


def label_for(name: str, args: dict) -> str:
    """Short human-readable label for the play-by-play and the 'ran N things' footer."""
    if name == "grep":
        where = args.get("path") or "."
        return f"grep {args.get('pattern', '')!r} in {where}"
    return f"{name} {args.get('path', '')}"


async def run_file_op(
    exec_cmd: Callable[[str], Awaitable[tuple]],
    name: str,
    args: dict,
) -> tuple[bool, str]:
    """Run one file tool on the target and return (ok, text_for_the_model).

    `exec_cmd(command)` must run a shell command ON THE TARGET and return
    `(exit_code, combined_output)` — node_service passes its own job runner, which is what
    keeps these tools working identically on the host, in a sandbox and on a Nostr worker.
    """
    op = _OPS.get(name)
    if not op:
        return False, f"(unknown file tool: {name})"

    payload: dict[str, Any] = {"op": op, "path": args.get("path") or ""}
    if op == "read":
        for k in ("offset", "limit"):
            if args.get(k) is not None:
                try:
                    payload[k] = int(args[k])
                except (TypeError, ValueError):
                    pass
    elif op == "write":
        content = args.get("content")
        if content is None:
            return False, "write_file needs `content`."
        content = str(content)
        if len(content.encode("utf-8")) > _MAX_WRITE_BYTES:
            return False, (f"content is too large to write in one call "
                           f"({len(content.encode('utf-8'))} bytes, limit {_MAX_WRITE_BYTES}). "
                           "Write it in pieces, or generate it on the host with run_command.")
        payload["content"] = content
    elif op == "edit":
        payload["old_string"] = args.get("old_string")
        payload["new_string"] = args.get("new_string")
        payload["replace_all"] = bool(args.get("replace_all"))
    elif op == "grep":
        payload["pattern"] = args.get("pattern") or ""
        payload["glob"] = args.get("glob") or ""

    if op != "grep" and not payload["path"]:
        return False, f"{name} needs `path`."

    try:
        _exit, out = await exec_cmd(_command_for(payload))
    except Exception as e:                                  # transport died, not a tool failure
        return False, f"could not run {name} on the host: {e}"

    text = (out or "").strip()
    # The program prints exactly one JSON object. Anything else means it never ran — almost
    # always a target without python3 — so say so plainly instead of feeding the model a raw
    # shell error it will try to "fix" by editing a file.
    try:
        res = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception:
        hint = ""
        if "not found" in text.lower() or _exit == 127:
            hint = (" This host appears to have no `python3`, which the file tools need. "
                    "Use run_command with shell tools instead.")
        return False, f"{name} failed: {text[:400] or 'no output'}{hint}"

    if not res.get("ok"):
        return False, str(res.get("error") or "unknown error")
    return True, str(res.get("content") or "(done)")
