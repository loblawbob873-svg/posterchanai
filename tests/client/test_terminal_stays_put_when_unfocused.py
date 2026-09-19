"""A TERMINAL MUST NOT RESIZE ITS PTY WHEN ITS WINDOW IS UNFOCUSED (backlog §4).

PosterChanOS parks/moves the shared feed when another window takes focus, and that transition fires
term.js's ResizeObserver with a temporary content size. Fitting xterm there sends SIGWINCH to the
PTY and makes the background terminal (and any full-screen program in it, vim/htop) visibly rewrap —
"the terminal shrinks when I click another window". `_fit()` guards against it: if the terminal's
`.osw` window frame is not `.focused`, it returns before touching FitAddon or the PTY.

The guard is DOM- and timing-coupled (ResizeObserver + an 80ms debounce + FitAddon), so this extracts
the shipped guard from term.js and RUNS it against a focused and an unfocused frame — a mutation to
it fails here.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TERM = ROOT / "static/js/client/term.js"
NODE = shutil.which("node") or shutil.which("nodejs")


def _guard():
    src = TERM.read_text(encoding="utf-8")
    m = re.search(
        r"const frame = box && box\.closest && box\.closest\('\.osw'\);\s*\n"
        r"\s*if\(frame && !frame\.classList\.contains\('focused'\)\) return;",
        src)
    assert m, "the unfocused-window guard is gone from term.js _fit()"
    return m.group(0)


def _run(focused):
    # A bare `return` in the guard exits this wrapper early → we never reach 'PROCEEDED'.
    js = (
        "const __out = process.stdout;\n"
        "(function(box){\n" + _guard() + "\n  __out.write('PROCEEDED');\n})("
        "{ closest:(s)=> s==='.osw' ? { classList:{ contains:(c)=> c==='focused' && "
        + ("true" if focused else "false") + " } } : null });\n")
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-500:]
    return r.stdout


def test_an_unfocused_terminal_does_not_reach_the_resize():
    assert NODE, "no node"
    # Unfocused frame: the guard must return BEFORE the fit/SIGWINCH — nothing is printed.
    assert _run(focused=False) == "", "an unfocused terminal fell through to FitAddon and SIGWINCH"


def test_a_focused_terminal_still_resizes():
    assert NODE, "no node"
    # Focused frame: the guard is the only discriminator; a focused window still fits normally.
    assert _run(focused=True) == "PROCEEDED", "a focused terminal was wrongly blocked from resizing"


def test_the_guard_runs_before_the_pty_is_resized():
    """Order matters: the focus check must precede fit.fit() and the size send, or the SIGWINCH is
    already on the wire before we decide not to send it."""
    src = TERM.read_text(encoding="utf-8")
    fit_body = src[src.index("function _fit()"):src.index("function _send(")]
    guard = fit_body.index("!frame.classList.contains('focused')) return")
    assert guard < fit_body.index("fit.fit()"), "the focus guard runs after FitAddon reflows the grid"
    assert guard < fit_body.index("_send({ t: 'size'"), "the focus guard runs after the PTY resize is sent"
