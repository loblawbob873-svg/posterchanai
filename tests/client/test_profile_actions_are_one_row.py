"""THE PROFILE HEADER'S CONTROLS MUST BE ONE SIZE.

Reported: *"Profile Settings: the Edit , Settings, mtip , hamburger menu, are all different
sizes!"*. Measured against the shipped stylesheet, side by side in one row:

    Edit 22px   ⚙ Settings 25px   ɱ Tip 24px   ⋯ 23px

Each button's height was whatever its CONTENT made it: a button holding an inline SVG builds a
different line box from one holding text, and one holding two spans (`.lbl` + `.ic`) builds a third.
`inline-flex` + `min-height` makes the box the ROW's decision rather than the label's, and
`line-height:1` removes the last pixel of font-metric difference between a glyph like ⚙ and plain
text.

Measured in a browser rather than asserted from the source: the numbers above are what a person
sees, and no reading of the CSS would have produced them.
"""
from pathlib import Path
import asyncio
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import tempfile
import threading
import urllib.request

import pytest


ROOT = Path(__file__).resolve().parents[2]
CSS = ROOT / "static/css/client.css"

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/css/client.css"></head>
<body><div class="prof"><div class="prof-actions">
<button class="btn btn-cyan small" id="edit-prof">Edit</button>
<button class="btn btn-ghost small" id="open-settings"><span class="lbl">&#9881; Settings</span><span class="ic">&#9881;</span></button>
<button class="btn btn-ghost small" id="xmrtip-prof">&#625; Tip</button>
<button class="btn btn-ghost small prof-menu-btn" id="prof-menu"><svg class="ic b-ic" aria-hidden="true"><use href="#i-menu"></use></svg></button>
</div></div></body></html>"""


def test_the_row_decides_the_height_not_the_label():
    css = CSS.read_text(encoding="utf-8")
    rule = css.split(".prof .prof-actions .btn{", 1)[1].split("}", 1)[0]
    assert "inline-flex" in rule, rule
    assert "min-height" in rule, rule
    assert "line-height:1" in rule, rule


def test_every_control_in_the_row_is_the_same_height():
    chrome = (shutil.which("google-chrome") or shutil.which("google-chrome-stable")
              or shutil.which("chromium"))
    if not chrome:
        pytest.skip("no chrome on this node")
    try:
        import websockets
    except Exception:
        pytest.skip("websockets module missing")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "index.html").write_text(PAGE, encoding="utf-8")

        class H(http.server.SimpleHTTPRequestHandler):
            def translate_path(self, path):
                path = path.split("?", 1)[0]
                if path.startswith("/static/"):
                    return str(ROOT / path.lstrip("/"))
                return str(root / (path.lstrip("/") or "index.html"))

            def log_message(self, *a):
                pass

        srv = socketserver.TCPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        profile = root / "profile"
        proc = subprocess.Popen(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             f"--user-data-dir={profile}", "--remote-debugging-port=0",
             "--window-size=1400,900", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        async def measure():
            devtools = None
            for _ in range(80):
                await asyncio.sleep(0.25)
                f = profile / "DevToolsActivePort"
                if f.is_file():
                    try:
                        devtools = int(f.read_text().splitlines()[0]); break
                    except Exception:
                        continue
            if not devtools:
                pytest.skip("chrome never wrote its DevToolsActivePort")
            d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{devtools}/json/version"))
            async with websockets.connect(d["webSocketDebuggerUrl"], max_size=None) as c:
                seq = [0]

                async def call(m, p=None, s=None):
                    seq[0] += 1
                    msg = {"id": seq[0], "method": m, "params": p or {}}
                    if s:
                        msg["sessionId"] = s
                    await c.send(json.dumps(msg))
                    while True:
                        r = json.loads(await c.recv())
                        if r.get("id") == seq[0]:
                            return r

                t = (await call("Target.createTarget", {"url": "about:blank"}))["result"]
                sess = (await call("Target.attachToTarget",
                                   {"targetId": t["targetId"], "flatten": True}))["result"]["sessionId"]
                await call("Page.enable", None, sess)
                await call("Runtime.enable", None, sess)
                await call("Page.navigate", {"url": f"http://127.0.0.1:{port}/index.html"}, sess)
                await asyncio.sleep(1.2)
                r = await call("Runtime.evaluate", {"expression":
                    "JSON.stringify([...document.querySelectorAll('.prof-actions .btn')]"
                    ".map(b=>[(b.textContent||'⋯').trim().slice(0,12),"
                    "Math.round(b.getBoundingClientRect().height)]))",
                    "returnByValue": True}, sess)
                return json.loads(r["result"]["result"]["value"])

        try:
            rows = asyncio.run(measure())
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            srv.shutdown()

    assert len(rows) == 4, rows
    heights = {h for _, h in rows}
    assert len(heights) == 1, "four controls, %d different heights: %s" % (len(heights), rows)
