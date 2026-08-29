"""The browser/PWA mini-player stays compact without shrinking its controls below finger size."""

import html as html_lib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
CHROME = (shutil.which("google-chrome-stable") or shutil.which("google-chrome") or
          shutil.which("chromium"))


def test_mini_player_controls_have_accessible_names():
    assert 'class="mp-play" title="${playLabel}" aria-label="${playLabel}"' in APP
    assert 'class="mp-exp" title="Expand" aria-label="Expand player"' in APP


@pytest.mark.skipif(not CHROME, reason="Chrome is unavailable")
def test_mini_player_controls_are_tappable_and_fit_a_360px_phone():
    page = f'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
      <style>*{{box-sizing:border-box}}html,body{{margin:0}}{CSS}</style>
      <div id="music-player" class="mp mp-mini"><span class="mp-eq">🎵</span>
        <span class="mp-title">A deliberately very long track name.flac</span>
        <button class="mp-play">▶</button><button class="mp-exp">↗</button></div>
      <script>const p=document.querySelector('#music-player'), bs=[...p.querySelectorAll('button')];
      document.title=JSON.stringify({{viewport:innerWidth,left:p.getBoundingClientRect().left,
        right:p.getBoundingClientRect().right,widths:bs.map(b=>b.getBoundingClientRect().width),
        heights:bs.map(b=>b.getBoundingClientRect().height)}});</script>'''
    with tempfile.TemporaryDirectory(prefix="pc-music-mini-") as tmp:
        path = Path(tmp) / "mini.html"
        path.write_text(page, encoding="utf-8")
        run = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                              "--window-size=360,700", "--force-device-scale-factor=1",
                              "--dump-dom", path.as_uri()], capture_output=True, text=True,
                             timeout=60, check=True)
    titles = re.findall(r"<title>(.*?)</title>", run.stdout, re.S)
    assert titles, run.stdout[-1000:]
    got = json.loads(html_lib.unescape(titles[-1]))
    assert min(got["widths"]) >= 40
    assert min(got["heights"]) >= 40
    assert got["left"] >= 0
    assert got["right"] <= got["viewport"] + 1
