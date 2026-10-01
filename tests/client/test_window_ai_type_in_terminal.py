"""Window ✨ on a terminal: "Type in terminal" puts ONE command at the prompt and never presses Enter.

Task: "Window AI: inline answers + approved per-app actions (... terminal type-in ...)". The shipped
functions are RUN under node:

  * os.js `_aiCommand` picks the one command out of an answer -- the first fenced block, else the
    first `inline` code, a leading "$ " dropped -- and gives NOTHING for a multi-line script, a very
    long line or anything with a control character (Copy is the way to take those);
  * term.js `typeable` is the terminal's own refusal, so a caller that skips `_aiCommand` still
    cannot type a newline (= Enter) or ESC into the shell; `typeIn` pastes and never sends '\\r'.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OS = open(os.path.join(ROOT, "static", "js", "client", "os.js"), encoding="utf-8").read()
TERM = open(os.path.join(ROOT, "static", "js", "client", "term.js"), encoding="utf-8").read()
NODE = shutil.which("node")


def _fn(src, name):
    """The shipped function's source, by brace matching from its declaration."""
    i = src.index("function " + name + "(")
    j, depth = src.index("{", i), 0
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[i:k + 1] + "\n"
    raise AssertionError(name)


ANSWERS = [
    ("fenced block", "Run this:\n```bash\nsudo apt install libfoo-dev\n```\nthen rebuild.", "sudo apt install libfoo-dev"),
    ("dollar prompt dropped", "```\n$ make -j4\n```", "make -j4"),
    ("inline code", "Try `git status` to see what changed.", "git status"),
    ("multi-line script is Copy's job", "```sh\ncd /tmp\nrm -rf build\n```", ""),
    ("prose only", "The build failed because a header is missing.", ""),
    ("escape sequence refused", "```\necho hi\x1b[2J\n```", ""),
]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_one_command_is_picked_out_of_an_answer():
    prog = _fn(OS, "_aiCommand") + "console.log(JSON.stringify(JSON.parse(process.argv[1]).map(_aiCommand)));"
    r = subprocess.run([NODE, "-e", prog, json.dumps([a for _, a, _ in ANSWERS])], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    for (name, _, want), got in zip(ANSWERS, json.loads(r.stdout)):
        assert got == want, name


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_terminal_refuses_anything_that_could_press_enter():
    prog = _fn(TERM, "typeable") + "console.log(JSON.stringify(JSON.parse(process.argv[1]).map(typeable)));"
    cases = [("ls -la", True), ("ls\n", False), ("ls\r", False), ("rm -rf x\n\x1b", False), ("", False), ("x" * 401, False)]
    r = subprocess.run([NODE, "-e", prog, json.dumps([c for c, _ in cases])], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [w for _, w in cases]
    body = _fn(TERM, "typeIn")
    assert "typeable(text)" in body and "term.paste(" in body
    assert "\\r" not in body and "_send(" not in body, "typing never presses Enter or writes to the PTY directly"
    assert "typeIn, run, _typeable: typeable" in TERM
    run = _fn(TERM, "run")
    # ▶ Run is the person's click standing in for Enter: the SAME one-line guard first, then one CR.
    assert run.index("if(!typeIn(text)) return false;") < run.index("_send({ t: 'in', d: '\\r' })"), run
