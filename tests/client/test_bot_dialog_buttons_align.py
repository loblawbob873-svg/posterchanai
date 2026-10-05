"""Admin → Bots: the dialog's Save bot lines up with Cancel and Delete all posts.

"Save Bot not aligned with other buttons". admin-theme.css gives every `.btn-primary` `align-self:
flex-start` (so it does not stretch in the settings forms), and it loads AFTER admin-tabs.css -- so at
equal specificity it overrode the footer's own `align-self: center` and Save sat at the top of the row
while the other two were centred. Rendered with the SHIPPED stylesheets, in the order admin.html loads
them, around the shipped footer markup.
"""
import asyncio
import re
import subprocess
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.client import test_effects_full_app as full

ROOT = Path(__file__).resolve().parents[2]


def _page():
    admin = (ROOT / "templates/admin.html").read_text()
    sheets = re.findall(r'href="/static/css/([\w.-]+\.css)', admin)
    assert sheets.index("admin-tabs.css") < sheets.index("admin-theme.css"), sheets
    bots = (ROOT / "templates/admin/tabs/bots.html").read_text()
    foot = re.search(r'<div class="bot-modal-foot">.*?</div>', bots, re.S).group(0)
    links = "".join(f'<link rel="stylesheet" href="/static/css/{s}">' for s in sheets)
    return f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">{links}</head>
<body class="admin-page"><div class="admin-page"><div class="bot-modal"><div class="bot-modal-card">{foot}</div></div></div>
<script>
 // The row grows taller than the buttons whenever the message beside them says something -- the
 // "Saved, but the avatar could not be uploaded..." line is exactly that -- or the row wraps on a phone.
 if(location.hash==='#msg') document.getElementById('botModalError').textContent='Saved, but the avatar could not be uploaded (upload failed: blossom refused) — press Save again to retry it.';
 window.__m=()=>[...document.querySelectorAll('.bot-modal-foot button')].map(b=>{{const r=b.getBoundingClientRect();
 return {{t:b.textContent.trim(),mid:Math.round(r.top+r.height/2),h:Math.round(r.height)}};}});</script></body></html>"""


HASH = ""


async def _measure(width):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(ROOT)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    page = ROOT / "static" / "__bot_foot_check.html"
    page.write_text(_page())
    try:
        with tempfile.TemporaryDirectory() as prof:
            out = subprocess.run(["/opt/google/chrome/chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
                                  f"--window-size={width},800", "--virtual-time-budget=3000",
                                  "--run-all-compositor-stages-before-draw", f"--user-data-dir={prof}",
                                  "--dump-dom", f"http://127.0.0.1:{srv.server_port}/static/__bot_foot_check.html#{HASH}"],
                                 capture_output=True, text=True, timeout=60)
        return out.stdout
    finally:
        page.unlink(missing_ok=True)
        srv.shutdown()


def _rows(width, msg=False):
    global HASH
    HASH = "msg" if msg else ""
    # --dump-dom gives HTML only; measure through a tiny inline writer instead.
    global _page
    base = _page
    def with_dump():
        return base().replace("</body>", "<pre id=out></pre><script>document.getElementById('out').textContent=JSON.stringify(__m())</script></body>")
    _page = with_dump
    try:
        html = asyncio.run(_measure(width))
    finally:
        _page = base
    import json
    import html as _h
    m = re.search(r'<pre id="out">(.*?)</pre>', html, re.S)
    assert m and m.group(1), html[-500:]
    return json.loads(_h.unescape(m.group(1)))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 390])
@pytest.mark.parametrize("msg", [False, True])
def test_save_bot_lines_up_with_the_other_buttons(width, msg):
    rows = _rows(width, msg)
    assert len(rows) == 3 and rows[-1]["t"].lower().startswith("save"), rows
    mids = {r["mid"] for r in rows}
    heights = {r["h"] for r in rows}
    assert max(mids) - min(mids) <= 1, ("the footer buttons are not on one line", rows)
    assert max(heights) - min(heights) <= 1, ("the footer buttons are not the same height", rows)
