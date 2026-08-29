"""The File Manager picker toolbar must fit a narrow phone without losing core controls."""

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
CHROME = (shutil.which("google-chrome-stable") or shutil.which("google-chrome") or
          shutil.which("chromium"))


@pytest.mark.skipif(not CHROME, reason="Chrome is unavailable")
def test_picker_core_toolbar_fits_at_360px_and_optional_density_yields():
    page = f'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
      <style>*{{box-sizing:border-box}}html,body{{margin:0}}{CSS}</style>
      <div class="bp-file-picker"><div class="bp-head">
        <button class="mini bp-locations">☰ Locations</button><h3>Choose from File Manager</h3>
        <label class="bp-sort"><span>Sort</span><select><option>Newest</option></select></label>
        <span class="bp-density"><button class="mini">S</button><button class="mini">M</button></span>
        <button class="mini bp-close">×</button></div></div>
      <script>const h=document.querySelector('.bp-head'), q=s=>document.querySelector(s);
      document.title=JSON.stringify({{width:h.getBoundingClientRect().width,scroll:h.scrollWidth,
        density:getComputedStyle(q('.bp-density')).display,
        locations:q('.bp-locations').getBoundingClientRect().width,
        sort:q('.bp-sort').getBoundingClientRect().width,close:q('.bp-close').getBoundingClientRect().width}});</script>'''
    with tempfile.TemporaryDirectory(prefix="pc-picker-mobile-") as tmp:
        path = Path(tmp) / "picker.html"
        path.write_text(page, encoding="utf-8")
        run = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                              "--window-size=360,700", "--force-device-scale-factor=1",
                              "--dump-dom", path.as_uri()], capture_output=True, text=True,
                             timeout=60, check=True)
    titles = re.findall(r"<title>(.*?)</title>", run.stdout, re.S)
    assert titles, run.stdout[-1000:]
    got = json.loads(html_lib.unescape(titles[-1]))
    assert got["density"] == "none"
    assert got["scroll"] <= got["width"] + 1
    assert min(got["locations"], got["sort"], got["close"]) > 0
