"""AN APPLICATION WORKSPACE IS NOT A PROSE COLUMN.

Reported repeatedly — "why does concord window width still not maximize the screen? said this many
times and never fixed, on classic ui, desktop".

Half of the fix was already in the code, which is why it kept looking done. `switchView` sets
`body.concord-view` and hides the right rail, and the comment beside it says it does so "so its own
sheet can remove the right rail and width cap without a one-frame layout jump". The rail went. The
cap never did — there was no rule anywhere that mentioned `concord-view`.

So Concord stayed inside `--app-max` (1664px), centred. On a 3072px monitor that is a chat app
using barely half the display, with dead space either side.

The cap is right for a TIMELINE: line length is the whole point of it, and this file's own comment
explains that capping the feed alone "opens ~400px of dead space between the feed and each rail,
which reads as three drifting islands". A chat is Discord-shaped — it wants the width and manages
its own columns inside it.

Measured in a real browser, because a max-width interacting with a grid and a media query is a
question about boxes and nothing that reads CSS as text can answer it.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
APP_JS = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium")

pytestmark = pytest.mark.skipif(not CHROME, reason="Chrome unavailable")


def widths(body_class: str, viewport: int = 2400):
    html = f"""<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
    <style>html,body{{margin:0;height:100%}}{CSS}</style><body class="{body_class}">
    <div class="app"><div class="sidebar"></div><div class="main"><div class="feed"></div></div>
    <div class="rightbar"></div></div><pre id="o"></pre><script>
    const a=document.querySelector('.app').getBoundingClientRect(),
          m=document.querySelector('.main').getBoundingClientRect();
    o.textContent=JSON.stringify({{app:Math.round(a.width),main:Math.round(m.width)}});</script>"""
    with tempfile.TemporaryDirectory() as td:
        page = Path(td) / "a.html"
        page.write_text(html, encoding="utf-8")
        done = subprocess.run([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
                               f"--window-size={viewport},1000", "--force-device-scale-factor=1",
                               "--dump-dom", page.as_uri()], text=True, capture_output=True, timeout=90)
    assert done.returncode == 0, done.stderr[-800:]
    m = re.search(r'<pre id="o">(.*?)</pre>', done.stdout, re.S)
    assert m, done.stdout[-800:]
    return json.loads(m.group(1).replace("&quot;", '"'))


def test_concord_fills_a_wide_screen():
    """THE REPORT. On 2400px the chat gets the screen, not a centred 1664px cluster."""
    got = widths("concord-view")
    assert got["app"] >= 2300, f"Concord is still capped: {got}"
    assert got["main"] >= 1900, f"the conversation column is still narrow: {got}"


def test_the_timeline_keeps_its_reading_measure():
    """The cap is not a bug — it is what stops a post running the full width of a 3072px monitor.
    Only the application views may opt out of it."""
    got = widths("")
    assert got["app"] <= 1700, f"the timeline lost its width cap: {got}"
    assert 850 <= got["main"] <= 1000, f"the reading column moved: {got}"


def test_a_narrow_screen_is_unaffected():
    """Below the breakpoint there is no cap and no third column to remove, so the rule must be inert
    rather than subtly different."""
    plain, concord = widths("", 1000), widths("concord-view", 1000)
    assert plain == concord, f"the rule changed the narrow layout: {plain} vs {concord}"


def test_the_class_the_rule_keys_on_is_actually_set():
    """The failure this whole file exists for: the class was set and the rule was never written, so
    everything looked correct on both sides."""
    assert "classList.toggle('concord-view', v==='concord')" in APP_JS
    assert "body.concord-view .app{max-width:none" in CSS
