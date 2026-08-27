"""Computed layout for live Office/Preview hosts inside the desktop window slot."""
import json
import html as html_lib
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
CHROME = (shutil.which("google-chrome-stable") or shutil.which("google-chrome") or
          shutil.which("chromium"))


@pytest.mark.skipif(not CHROME, reason="Chrome is unavailable")
def test_live_document_slots_are_opaque_and_own_their_layout():
    css = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
    page = f"""<!doctype html><style>{css}</style>
      <div id=office class='osw-slot office-win'></div>
      <div id=preview class='osw-slot pv-host pv-win'></div>
      <script>
      const pick=id=>{{const s=getComputedStyle(document.getElementById(id));
        return {{opacity:s.opacity,padding:s.padding,overflow:s.overflow}}}};
      document.title=JSON.stringify({{office:pick('office'),preview:pick('preview')}});
      </script>"""
    with tempfile.TemporaryDirectory(prefix="pc-document-css-") as tmp:
        html = Path(tmp) / "index.html"
        html.write_text(page, encoding="utf-8")
        run = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                              "--dump-dom", html.as_uri()], capture_output=True, text=True,
                             timeout=60, check=True)
    titles = re.findall(r"<title>(.*?)</title>", run.stdout, re.S)
    assert titles, run.stdout[-1000:]
    got = json.loads(html_lib.unescape(titles[-1]))
    assert got["office"] == {"opacity": "1", "padding": "10px", "overflow": "hidden"}
    assert got["preview"] == {"opacity": "1", "padding": "0px", "overflow": "hidden"}
