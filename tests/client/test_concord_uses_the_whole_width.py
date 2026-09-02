"""CONCORD MUST USE THE WHOLE WINDOW — measured in a browser, at more than one width.

Reported, repeatedly, most recently as "concord UI still not maximizing to dwidth!". The comment in
`client.css` beside the previous attempt already records it being said "many times and never fixed",
and that attempt was real: `body.concord-view .app{max-width:none}` removed the 1664px prose cluster
a timeline wants and a chat app does not.

It did not work, and the reason is one unit. `concord.css` also carried

    html:has(body.concord-view), body.concord-view, …, .cc-messages{max-width:100vw!important}

which lands LATER and therefore wins. The client scales the whole UI with `body{zoom}` by viewport
(see the display-scaling rules), and `vw` resolves against the DEVICE viewport with no regard for
zoom. At zoom 0.67 that caps the app at 1280 CSS px inside a body that is genuinely 1910 CSS px
wide — a third of the monitor unused. Measured before the fix:

    1280px viewport -> app 858px   (422px wasted)   zoom 0.67
    1920px viewport -> app 1478px  (442px wasted)   zoom 0.77
    3072px viewport -> app 3072px  (correct)        zoom 1

The last line is why this survived so long: at zoom 1 the rule is harmless, so any check written at
one wide viewport passes against the bug. THIS FILE THEREFORE MEASURES AT SEVERAL WIDTHS, and that
is the point of it rather than an embellishment.
"""
from __future__ import annotations

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
CONCORD = (ROOT / "static/css/concord.css").read_text(encoding="utf-8")
CHROME = (shutil.which("google-chrome-stable") or shutil.which("chromium")
          or shutil.which("chrome"))

pytestmark = pytest.mark.skipif(not CHROME, reason="Chrome unavailable")

#: Widths that straddle the display-scaling breakpoints. A single wide viewport passes on the bug.
WIDTHS = [1280, 1600, 1920, 2560, 3072]


def measure(width: int, css: str = CSS, concord: str = CONCORD) -> dict:
    page = f"""<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<style>html,body{{margin:0;height:100%}}{css}
{concord}</style>
<body class="concord-view">
<div class="app">
  <aside class="sidebar">nav</aside>
  <main class="main">
    <div class="feed feed-dm" id="feed">
      <div class="cc-app">
        <div class="cc-rail">r</div><div class="cc-channels">ch</div>
        <div class="cc-conversation"><div class="cc-messages">msgs</div></div>
        <div class="cc-members-pane">m</div>
      </div>
    </div>
  </main>
</div>
<pre id="out"></pre><script>requestAnimationFrame(()=>{{
  const q=s=>document.querySelector(s), w=e=>e?Math.round(e.getBoundingClientRect().width):0;
  out.textContent=JSON.stringify({{viewport:window.innerWidth, app:w(q('.app')),
    main:w(q('.main')), ccApp:w(q('.cc-app')),
    zoom:getComputedStyle(document.body).zoom}});}});</script>"""
    with tempfile.TemporaryDirectory() as td:
        html = Path(td) / "c.html"
        html.write_text(page, encoding="utf-8")
        done = subprocess.run(
            [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
             f"--window-size={width},1000", "--force-device-scale-factor=1",
             "--virtual-time-budget=1200", "--dump-dom", html.as_uri()],
            capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr[-1000:]
        found = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S)
        assert found, done.stdout[-1000:]
        return json.loads(unescape(found.group(1)))


@pytest.mark.parametrize("width", WIDTHS)
def test_the_chat_fills_the_window(width):
    """THE REPORT. A chat app is Discord-shaped: it wants the whole width and manages its own
    columns inside it."""
    got = measure(width)
    wasted = got["viewport"] - got["app"]
    assert wasted <= 8, (
        f"at {width}px the app is {got['app']}px — {wasted}px of the window unused "
        f"(body zoom {got['zoom']})")


@pytest.mark.parametrize("width", WIDTHS)
def test_the_conversation_column_grows_with_it(width):
    """Filling the window is worth nothing if the chat itself stays narrow inside it."""
    got = measure(width)
    assert got["ccApp"] >= got["app"] - 320, (
        f"at {width}px the app is {got['app']}px but the chat is only {got['ccApp']}px")


def test_the_viewport_unit_does_not_come_back():
    """Names the exact defect. `vw` ignores `body{zoom}`, so it is never the right unit for a cap on
    a scaled element — and the rule reads as obviously correct, which is how it survived."""
    rule = CONCORD.split("html:has(body.concord-view)", 1)[1].split("}", 1)[0]
    assert "100vw" not in rule, (
        "the concord width cap is expressed in vw again — under body{zoom} that caps the app at the "
        "DEVICE width inside a larger CSS-pixel body, wasting a third of the screen")


def test_this_check_can_fail():
    """MUTATION. The probe is only worth something if the pre-fix stylesheet still fails it — and
    note it must fail at a SCALED width, since at zoom 1 the old rule was harmless."""
    broken = CONCORD.replace("max-width:100%!important;overflow-x:clip!important",
                             "max-width:100vw!important;overflow-x:clip!important", 1)
    assert broken != CONCORD, "could not rebuild the pre-fix stylesheet — re-read this test"
    got = measure(1280, concord=broken)
    assert got["viewport"] - got["app"] > 100, (
        "the pre-fix stylesheet fills the window in this probe, so the probe proves nothing")


def test_at_zoom_one_the_old_rule_looked_fine():
    """Why every previous fix appeared to work: at a viewport wide enough for zoom 1, `100vw` and
    `100%` agree. A check written only at 3072px passes against the bug."""
    broken = CONCORD.replace("max-width:100%!important;overflow-x:clip!important",
                             "max-width:100vw!important;overflow-x:clip!important", 1)
    got = measure(3072, concord=broken)
    assert got["viewport"] - got["app"] <= 8
