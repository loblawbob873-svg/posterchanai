"""AN APP DIALOG MUST NOT TAKE THE MACHINE'S TASKBAR WITH IT.

Reported as "I sent email and then desktop is not working anymore, cant focus a window or search in
start menu", followed by "second monitor desktop is compleetely useless". Nothing was broken: a
Reply composer was open on that output. Its backdrop is `position:fixed; inset:0; z-index:100`, and
the taskbar is a `position:relative` flex child with no z-index at all — so the dialog painted over
the start menu, the clock and every task button, and swallowed their clicks.

From the person's side that is indistinguishable from a dead desktop, which is exactly how it was
reported. Pressing Escape closed the composer (it saved to drafts) and the machine was fine.

No desktop behaves this way. An application's dialog is modal to its APPLICATION; the shell's own
chrome stays reachable, because it is the only way to see what else is running and get back to it.
This file's own stylesheet already agreed with that in one place and not the other: the macOS-style
dock is `z-index:309`, above every modal, while the default taskbar sat below them.

So the default taskbar joins it at 309 — above `.modal-bg` (100) and `.modal-bg.modal-sub` (200),
below the start menu and flyouts (340/360) which must open OVER it, and below `.uiconfirm-bg`
(500), which is a real yes/no somebody has to answer and is allowed to block everything.

Measured with elementFromPoint in a real browser against the shipped stylesheet, because "which
element gets the click" is a stacking-context question and nothing that reads CSS as text can
answer it — an ancestor with its own stacking context would scope the z-index and silently make
this change do nothing.
"""
import json
import re
import shutil
import subprocess
import tempfile
from html import unescape
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chrome")

pytestmark = pytest.mark.skipif(not CHROME, reason="Chrome unavailable")

#: The shell as os.js builds it (see the innerHTML in mount()), plus an overlay under test.
#: THE ORDER IS THE POINT: `#modal-root` is in templates/client.html and os.js APPENDS `.os-root`
#: to the body afterwards, so the desktop is the later sibling. Written the other way round this
#: whole file passes without proving anything, which is how a stacking test quietly stops testing.
PAGE = """<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<style>html,body{{margin:0;height:100%}}{css}</style>
<div id="modal-root">{overlay}</div>
<div class="os-root{style}" id="os-root">
  <nav class="os-mac-menu" id="os-mac-menu"></nav>
  <div class="os-desk" id="os-desk"></div>
  <div class="os-bar" id="os-bar"><button id="os-start">Start</button></div>
</div>
<pre id="out"></pre><script>requestAnimationFrame(()=>{{
  const bar = document.getElementById('os-bar').getBoundingClientRect();
  const x = Math.round(bar.left + 20), y = Math.round(bar.top + bar.height/2);
  const hit = document.elementFromPoint(x, y);
  out.textContent = JSON.stringify({{
    barH: Math.round(bar.height),
    hit: hit ? (hit.id || hit.className || hit.tagName) : null,
    inBar: !!(hit && hit.closest && hit.closest('#os-bar'))}});
}});</script>"""

MODAL = '<div class="modal-bg"><div class="modal glass">a reply composer</div></div>'
SUB_MODAL = '<div class="modal-bg modal-sub"><div class="modal glass">the Files picker</div></div>'
CONFIRM = '<div class="uiconfirm-bg"><div class="uiconfirm">Delete this?</div></div>'


def hit_test(overlay="", style=""):
    html = PAGE.format(css=CSS, overlay=overlay, style=style)
    with tempfile.TemporaryDirectory() as td:
        page = Path(td) / "d.html"
        page.write_text(html, encoding="utf-8")
        done = subprocess.run([
            CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
            "--window-size=1400,900", "--force-device-scale-factor=1",
            "--virtual-time-budget=1500", "--dump-dom", page.as_uri()],
            capture_output=True, text=True, timeout=90)
        assert done.returncode == 0, done.stderr[-1200:]
        m = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S)
        assert m, done.stdout[-1200:]
        return json.loads(unescape(m.group(1)))


def test_the_taskbar_is_reachable_with_no_dialog_open():
    """The control case. If this ever fails the harness is wrong, not the stylesheet."""
    got = hit_test()
    assert got["barH"] > 0, got
    assert got["inBar"], f"the taskbar is not even clickable with nothing over it: {got}"


def test_a_reply_composer_does_not_swallow_the_start_menu():
    """THE REPORT. A click on the taskbar has to reach the taskbar."""
    got = hit_test(MODAL)
    assert got["inBar"], (
        f"a modal backdrop is taking the taskbar's clicks — this is 'cant focus a window or "
        f"search in start menu' with a dialog open somewhere on the screen: {got}")


def test_a_modal_opened_over_a_modal_does_not_either():
    """`.modal-bg.modal-sub` is z-index 200 — the Blossom picker opened from inside the composer,
    i.e. exactly the stack the reporter was in."""
    assert hit_test(SUB_MODAL)["inBar"]


def test_a_real_confirm_is_still_allowed_to_block_everything():
    """The line this must NOT cross. `.uiconfirm-bg` is a yes/no somebody has to answer; letting
    the taskbar punch through it would be a different bug, and a worse one — it is what stands in
    front of deleting things."""
    got = hit_test(CONFIRM)
    assert not got["inBar"], (
        f"the taskbar now sits above a confirmation dialog, so a destructive prompt can be walked "
        f"around instead of answered: {got}")


def test_the_mac_dock_already_behaved_and_still_does():
    """The precedent this change follows, kept honest: the macOS-style dock was always above
    modals, which is why the default taskbar being below them read as an oversight rather than a
    decision."""
    assert hit_test(MODAL, style=" os-style-mac")["inBar"]
