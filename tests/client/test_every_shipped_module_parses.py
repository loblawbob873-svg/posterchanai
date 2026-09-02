"""EVERY SHIPPED JS MODULE MUST PARSE — the guard that was missing when this bug came back.

A backtick inside a comment that lives inside a template literal closes the literal. The file then
throws a SyntaxError at PARSE time, so nothing in it runs, nothing is logged, and the symptom is
whatever that module was responsible for silently vanishing.

This has now happened twice:

  * `sprite.js` — a backtick in a comment inside the one template literal the whole SVG lives in.
    Every icon in the client went blank with no error. `test_icon_sprite_loads.py` was written for
    it, and it only covers sprite.js.
  * `os.js` — the same mistake, in a comment added to the taskbar's window-control markup. The
    desktop shell stopped parsing: no taskbar bindings, no drag-snapping, no window controls.
    Reported within minutes as "mouse dragged window snapping no longer works now".

The specific guard did not generalise, so this is the general one: 61 client modules and the desktop
main/preload, each handed to node's own parser. It is the cheapest possible test and it catches the
entire class — a stray backtick, an unbalanced brace, a bad regex — for every file at once, rather
than one file at a time after each incident.

It parses the file as a SCRIPT (never imports it): these modules touch `window` at load, and the
question here is only whether the parser accepts them.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: Everything the browser or Electron is handed directly.
TARGETS = sorted(
    [p for p in (ROOT / "static/js/client").glob("*.js")]
    + [p for p in (ROOT / "static/js").glob("*.js")]
    + [p for p in (ROOT / "desktop").glob("*.js") if p.name != "build-www.sh"]
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")


def test_this_test_is_looking_at_something():
    """A glob that matches nothing passes vacuously — which is how a guard quietly stops guarding."""
    assert len(TARGETS) >= 40, f"only found {len(TARGETS)} modules — has the layout moved?"
    names = {p.name for p in TARGETS}
    for expected in ("os.js", "app.js", "sprite.js", "monero-wallet.js"):
        assert expected in names, f"{expected} is no longer being checked"


@pytest.mark.parametrize("path", TARGETS, ids=lambda p: p.name)
def test_it_parses(path: Path):
    done = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=90)
    assert done.returncode == 0, (
        f"{path.relative_to(ROOT)} does not parse — nothing in it runs and nothing is logged:\n"
        + done.stderr[-1200:])


def test_a_backtick_in_a_template_literal_is_still_caught():
    """MUTATION, on a copy: reproduce the exact mistake and prove the check goes red. Without this
    the test above could be passing because node --check silently succeeded on nothing."""
    import tempfile
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    # Put a backtick inside the taskbar markup's own comment, which is inside a template literal.
    marker = "NO BACKTICKS IN THIS COMMENT"
    assert marker in src, "the comment this mutation targets has moved — re-read this test"
    broken = src.replace(marker, "a `backtick` here", 1)
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "os.js"
        bad.write_text(broken, encoding="utf-8")
        done = subprocess.run(["node", "--check", str(bad)], capture_output=True, text=True, timeout=90)
    assert done.returncode != 0, (
        "a backtick inside a template literal no longer breaks the parse, so this check proves "
        "nothing — re-read it before trusting it")
