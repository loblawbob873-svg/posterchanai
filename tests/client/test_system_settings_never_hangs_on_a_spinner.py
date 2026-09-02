"""SYSTEM SETTINGS MUST NOT SIT ON A SPINNER WHEN A BRIDGE NEVER ANSWERS.

Reported as "System Settings has window controls but nothing else? wtf" — a window with a title bar
and an empty body.

`renderSystemSettings` sets `host.innerHTML = '<div class="spinner"></div>'` and then reads the
machine's bridges. Every read was an unbounded `await` inside a try/catch, which handles a bridge
that THROWS and does nothing at all for one that never resolves: the spinner stays, with no error,
no log, and no way out. `displays` is the DEFAULT page, so a hung display daemon is the whole app.

The comment already in that function claims scoping the reads per page fixed this ("a hung display
daemon left the whole Settings app on a spinner"). Scoping cannot fix it — the page you land on is
the page that hangs.

Every read is bounded now, and a TIMEOUT is reported as its own state, separate from an error:
"did not answer" and "answered with an error" send a person to different places.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    start = OS_JS.index(f"  function {name}(")
    depth, i = 0, OS_JS.index("{", start)
    for j in range(i, len(OS_JS)):
        if OS_JS[j] == "{":
            depth += 1
        elif OS_JS[j] == "}":
            depth -= 1
            if depth == 0:
                return OS_JS[start:j + 1]
    raise AssertionError(name)


def _render_src() -> str:
    start = OS_JS.index("  async function renderSystemSettings(){")
    return OS_JS[start:start + 6000]


def run_race(behaviour: str) -> dict:
    """Drive the shipped _settingsRead against a bridge that hangs / throws / answers."""
    bridge = {
        "hang": "() => new Promise(() => {})",
        "throw": "() => Promise.reject(new Error('daemon refused'))",
        "answer": "() => Promise.resolve(['DP-1'])",
    }[behaviour]
    program = """
      %(fn)s
      const started = Date.now();
      _settingsRead((%(bridge)s)(), 'displays').then(r => {
        process.stdout.write(JSON.stringify({ok:!!r.ok, timedOut:!!r.timedOut,
          error:r.error||'', tookMs: Date.now()-started}));
        process.exit(0);
      });
    """ % {"fn": _fn("_settingsRead").replace("const _SETTINGS_TIMEOUT = 6000;", ""),
           "bridge": bridge}
    # the constant lives outside the function; inline a short one so the test is quick
    program = "const _SETTINGS_TIMEOUT = 250;\n" + program
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-600:]
    return json.loads(done.stdout)


def test_a_bridge_that_never_answers_is_given_up_on():
    """THE BUG. Without this the settings window is a spinner for the life of the desktop."""
    got = run_race("hang")
    assert got["ok"] is False and got["timedOut"] is True
    assert got["tookMs"] < 3000, "the read is still effectively unbounded"


def test_a_bridge_that_throws_is_reported_as_an_error_not_a_timeout():
    """They are different facts, and they send a person to different places."""
    got = run_race("throw")
    assert got["ok"] is False and got["timedOut"] is False
    assert "refused" in got["error"]


def test_a_bridge_that_answers_is_not_disturbed():
    got = run_race("answer")
    assert got["ok"] is True


def test_every_bridge_read_in_the_renderer_is_bounded():
    """Four pages, four bridges. One left unbounded is one page that can still hang for ever."""
    src = _render_src()
    for bridge in ("pcDisplays.status()", "pcPower.status()", "pcSystem.snapshot(false)",
                   "pcOS.identity()"):
        assert f"await {bridge}" not in src, f"{bridge} is awaited unbounded again"
        assert bridge in src, f"{bridge} is no longer read at all — re-read this test"
    assert src.count("_settingsRead(") >= 4


def test_a_timeout_says_so_on_screen():
    """An empty page and a page that explains itself are different products."""
    src = _render_src()
    assert "did not answer" in src


def test_the_displays_page_is_still_the_default():
    """Which matters because it is the page whose bridge hangs; if this ever changes, the failure
    moves and this file should be re-read rather than quietly passing."""
    assert "_osSettingsPage='displays'" in OS_JS
