"""A PLAIN-TEXT MAIL MUST BE READABLE TO THE END — measured in a browser, not reasoned about.

Reported as "i can't even read the fucking email message entirely on mobile! no scroll on messages!"
after the mobile mail layout had already been fixed once and `check_mail_mobile.py` was green.

The check was green because every fixture it opens is an **HTML** message. HTML mail renders into a
sandboxed iframe, and `_sizeMailFrames` rescues that case at runtime: it measures the document and
writes `flex:none` plus a real pixel height onto the frame, which escapes the clamp below. A
**plain text** mail renders as an ordinary `.mail-text` div and has no such rescue.

The clamp is the single-message rule. A thread holding one message lets that message own the pane
instead of leaving slack under it, and it said:

    .mail-thread>.mail-msg:first-child:nth-last-child(2) .mail-body{flex:1;min-height:0;...}

`flex:1` is `flex:1 1 0%` — shrinking permitted — and `min-height:0` removes the content floor that
would normally stop a flex item collapsing under what is inside it. `.mail-msg` is `overflow:hidden`
(it has rounded corners), so the part that does not fit is clipped away, and because nothing
overflows the scroll container there is nothing to scroll. Measured at 390px:

    text needs 3225px · rendered in a 465px box · .mail-read scrollable distance 0px

Not a small window onto the message — the end of the message was simply unreachable. `flex:1 0 auto`
still fills the pane, which is the only reason the rule exists, but cannot shrink below the mail.

This measures REAL BOXES in a real browser against the shipped stylesheet, because "can this be
scrolled to" is a question about layout and nothing that reads CSS as text can answer it.
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
CSS_PATH = ROOT / "static/css/client.css"
CSS = CSS_PATH.read_text(encoding="utf-8")
CHROME = (shutil.which("google-chrome-stable") or shutil.which("chromium")
          or shutil.which("chrome"))

pytestmark = pytest.mark.skipif(not CHROME, reason="Chrome unavailable")

#: Long on purpose. A short mail fits any box, so only a long one can show whether the message is
#: read on the page or clipped at the pane.
LONG_TEXT = "<br>".join(
    f"Paragraph {i} of a long plain-text message that a person has to be able to read to the end."
    for i in range(60))
LONG_HTML_DOC = "".join(f"&lt;p&gt;Paragraph {i}&lt;/p&gt;" for i in range(60))


def measure(kind: str, css: str = CSS, width: int = 390, height: int = 780) -> dict:
    """Open one message in the mobile reader and report what is reachable."""
    body = (f'<div class="mail-text">{LONG_TEXT}</div>' if kind == "text"
            else f'<iframe class="mail-html" data-mail-autosize="1" srcdoc="{LONG_HTML_DOC}"></iframe>')
    # The singleton rule keys on `:first-child:nth-last-child(2)` — the message plus the reply row.
    page = f"""<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<style>html,body{{margin:0;height:100%}}{css}</style>
<div class="mail-read has-open">
  <div class="mail-read-hd">header</div>
  <div class="mail-thread">
    <div class="mail-msg open">
      <div class="mail-msg-hd">who</div>
      <div class="mail-msg-body"><div class="mail-body">{body}</div></div>
    </div>
    <div class="mail-reply-row">reply</div>
  </div>
</div>
<pre id="out"></pre><script>requestAnimationFrame(()=>{{
  const read=document.querySelector('.mail-read'),
        content=document.querySelector('.mail-text,.mail-html');
  read.scrollTop=1e6;                      /* scroll as far as the reader allows */
  out.textContent=JSON.stringify({{
    needed: Math.round(content.scrollHeight||0),
    rendered: Math.round(content.getBoundingClientRect().height),
    scrollable: Math.round(read.scrollHeight - read.clientHeight),
    bottomAfterScroll: Math.round(content.getBoundingClientRect().bottom),
    viewport: window.innerHeight}});}});</script>"""
    with tempfile.TemporaryDirectory() as td:
        html = Path(td) / "mail.html"
        html.write_text(page, encoding="utf-8")
        done = subprocess.run(
            [CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
             f"--window-size={width},{height}", "--force-device-scale-factor=1",
             "--virtual-time-budget=1500", "--dump-dom", html.as_uri()],
            capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr[-1200:]
        found = re.search(r'<pre id="out">(.*?)</pre>', done.stdout, re.S)
        assert found, done.stdout[-1200:]
        return json.loads(unescape(found.group(1)))


def test_the_whole_plain_text_message_is_laid_out():
    """THE BUG. The box has to be as tall as the mail; anything less is clipped by `overflow:hidden`
    with no scrollbar of its own to recover it."""
    got = measure("text")
    assert got["rendered"] >= got["needed"] - 4, (
        f"the message is {got['needed']}px tall and only {got['rendered']}px is laid out — "
        f"the rest is clipped away by .mail-msg's overflow:hidden")


def test_the_reader_can_actually_scroll_to_the_bottom_of_it():
    """The half that names the report. A tall message inside a clamped box produced a reader with
    ZERO scrollable distance — not a small window onto the mail, an unreachable end."""
    got = measure("text")
    assert got["scrollable"] > 0, (
        "the mail reader has no scrollable distance at all, so the end of a long message cannot be "
        "reached — this is 'no scroll on messages'")
    assert got["bottomAfterScroll"] <= got["viewport"] + 4, (
        f"scrolled to the end and the message still runs {got['bottomAfterScroll'] - got['viewport']}px "
        f"past the bottom of the screen")


def test_it_holds_on_the_smallest_phone_too():
    got = measure("text", width=360, height=640)
    assert got["rendered"] >= got["needed"] - 4 and got["scrollable"] > 0, got


def test_the_html_path_is_left_alone():
    """The iframe case is rescued at RUNTIME by `_sizeMailFrames`, which writes `flex:none` and a
    measured height onto the frame. This fix must not disturb it — an iframe with no script driving
    it keeps whatever the stylesheet gives it, and that is still true."""
    got = measure("html")
    assert got["rendered"] > 0

def test_the_single_message_rule_still_fills_the_pane():
    """Why the rule exists at all: one short message should own the pane rather than leaving a strip
    of empty background under it. The fix must not have turned that off — `flex:1 0 auto` grows."""
    rule = CSS.split(".mail-thread>.mail-msg:first-child:nth-last-child(2){", 1)[1].split("}", 1)[0]
    assert "flex:1 0 auto" in rule, "the singleton no longer grows to fill the pane"


def test_the_clamp_cannot_come_back():
    """Names the exact shape. `flex:1` (which is `flex:1 1 0%`) together with `min-height:0` is what
    let the box be smaller than the mail inside it."""
    for selector in (".mail-thread>.mail-msg:first-child:nth-last-child(2){",
                     ".mail-thread>.mail-msg:first-child:nth-last-child(2)>.mail-msg-body{",
                     ".mail-thread>.mail-msg:first-child:nth-last-child(2) .mail-body{"):
        rule = CSS.split(selector, 1)[1].split("}", 1)[0]
        assert "min-height:0" not in rule, (
            f"{selector} removes the content floor again — a plain-text mail will be clipped at the "
            f"pane with nothing to scroll it")


def test_this_check_can_fail():
    """MUTATION. The measurement above is only worth anything if the pre-fix stylesheet still fails
    it — a browser probe that passes on the bug is how this shipped green the first time."""
    broken, swaps = CSS, 0
    # The clamp was all three rules together: the message, its body wrapper and the mail body. One
    # of them alone does not reproduce it — the first attempt at this mutation reverted only the
    # innermost rule, still measured a scrollable reader, and would have passed on the bug.
    for selector in (".mail-thread>.mail-msg:first-child:nth-last-child(2){",
                     ".mail-thread>.mail-msg:first-child:nth-last-child(2)>.mail-msg-body{",
                     ".mail-thread>.mail-msg:first-child:nth-last-child(2) .mail-body{"):
        head = selector + ("display:flex;flex-direction:column;flex:1 0 auto"
                           if selector.endswith("(2){") else "flex:1 0 auto")
        if head in broken:
            broken = broken.replace(
                head,
                selector + ("display:flex;flex-direction:column;min-height:0;flex:1"
                            if selector.endswith("(2){") else "flex:1;min-height:0"), 1)
            swaps += 1
    assert swaps == 3, f"could not rebuild the pre-fix stylesheet (swapped {swaps}/3)"
    got = measure("text", css=broken)
    assert got["rendered"] < got["needed"] - 4 or got["scrollable"] == 0, (
        "the pre-fix stylesheet passes this probe, so the probe proves nothing")
