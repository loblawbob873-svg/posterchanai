"""A quoted post's author avatar is its 22px circle, never a full-width picture.

Measured on poster.place's Nostrverse at phone width, 2026-10-16 (while chasing Vyram's choppy APK feed): the
`.qav` avatar inside every quote rendered 263-289px wide -- a 1500x1500 original drawn as a post picture --
because the attachment rules (`.note .txt img:not(.emoji-inline)`, (0,2,1)+) out-specify `.quoted .qav` and
the quote's header lives INSIDE the quoting note's `.txt`. Renders the SHIPPED stylesheet with the exact markup
chain found live (a quote, and a quote inside a quote) and measures the avatar.
"""
import html as html_lib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHROME = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or shutil.which("chromium")
PX = ("data:image/svg+xml," + "%3Csvg xmlns='http://www.w3.org/2000/svg' width='1500' height='1500'%3E"
      "%3Crect width='1500' height='1500' fill='red'/%3E%3C/svg%3E")


@pytest.mark.skipif(not CHROME, reason="Chrome is unavailable")
def test_quote_avatars_are_22px_in_a_quote_and_in_a_quote_inside_a_quote():
    css = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
    def quote(qid, body):
        return ("<div class='quoted'><div class='hd'><img alt='' class='qav' id='" + qid + "' src=\"" + PX
                + "\"><span class='name'>a</span></div><div class='txt'>" + body + "</div></div>")
    inner = quote("q2", "hello")
    page = f"""<!doctype html><meta name=viewport content='width=393'><style>{css}</style>
      <div class='feed' style='width:393px'><div><article class='note'><div class='body'>
        <div class='txt clamp'>{quote("q1", "look " + inner)}</div></div></article></div></div>
      <script>window.onload=()=>{{const w=id=>Math.round(document.getElementById(id).getBoundingClientRect().width);
        document.title=JSON.stringify({{q1:w('q1'),q2:w('q2')}});}}</script>"""
    with tempfile.TemporaryDirectory(prefix="pc-qav-") as tmp:
        f = Path(tmp) / "index.html"
        f.write_text(page, encoding="utf-8")
        run = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--window-size=393,851",
                              "--virtual-time-budget=3000", "--dump-dom", f.as_uri()],
                             capture_output=True, text=True, timeout=60, check=True)
    titles = re.findall(r"<title>(.*?)</title>", run.stdout, re.S)
    assert titles, run.stdout[-800:]
    got = json.loads(html_lib.unescape(titles[-1]))
    assert got == {"q1": 22, "q2": 22}, ("a quote's avatar is drawn as a picture", got)
