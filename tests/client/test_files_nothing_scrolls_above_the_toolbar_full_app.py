"""Files: scrolling a long list never shows a file in the strip ABOVE the toolbar.

"the files are going above the toolbar in that tiny gap" (PosterChanOS, 2026-10-13). The pane that scrolls
(.fx-main) has 6px of top padding and the sticky toolbar stuck below it, so rows scrolling past were drawn in
that strip. The old test (test_files_explorer_layout.py) only read the CSS text for `position:sticky`, which was
true the whole time. This loads the SHIPPED client.css in Chrome, scrolls a long list, and asks the browser what
is painted in the top rows of the scroller.

Run: venv-unified/bin/python -m pytest tests/client/test_files_nothing_scrolls_above_the_toolbar_full_app.py -q
"""
import asyncio
import http.server
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or "/opt/google/chrome/chrome"

PAGE = """<!doctype html><html><head><link rel=stylesheet href="/client.css"></head>
<body><div id=feed class="feed feed-files" style="height:600px;display:flex;flex-direction:column">
<div id=files-pane style="flex:1;min-height:0;display:flex;flex-direction:column">
<div class=fx-explorer><div class=fx-side></div><div class=fx-main>
<div class=fx-bar><div class=fx-crumbs><button class=fx-crumb>Files</button></div><button class=fx-newfolder>New folder</button></div>
<div class="files-grid" id=bl-grid>CARDS</div></div></div></div></div>
<script>
async function probe(){
  // whichever box actually scrolls at this width: the list (.fx-main) on a wide window, the whole pane narrower
  const cands=[document.querySelector('.fx-main'),document.getElementById('files-pane')];
  const m=cands.find(e=>e.scrollHeight>e.clientHeight+2&&/(auto|scroll)/.test(getComputedStyle(e).overflowY))||cands[0];
  const inFile=[]; let gap=0, scrolled=0;
  // many scroll offsets and many x positions: a single sample can land between two rows of tiles
  for(let st=40; st<=1240; st+=37){
    m.scrollTop=st; await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))); scrolled=Math.max(scrolled,m.scrollTop);
    const box=m.getBoundingClientRect(), bar=document.querySelector('.fx-bar').getBoundingClientRect(); gap=Math.round(bar.top-box.top);
    for(let x=box.left+20; x<box.right-20; x+=40) for(let y=Math.ceil(box.top); y<bar.top; y++){
      const el=document.elementFromPoint(x,y); const c=el?String(el.className||el.tagName):'';
      if(el && el.closest && el.closest('.file-card')) inFile.push({st,x:Math.round(x-box.left),y:Math.round(y-box.top),cls:c});
    }
  }
  return JSON.stringify({scroller:m.id||m.className,scrolled,gap,inFile});
}
</script></body></html>"""


async def _run(url, port):
    import websockets
    tab = None
    for _ in range(80):
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list"))
            tab = next((t for t in tabs if t.get("type") == "page" and t.get("url", "").startswith(url)), None)
            if tab:
                break
        except Exception:
            pass
        await asyncio.sleep(.25)
    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=None) as ws:
        ids = iter(range(1, 10 ** 6))

        async def evaluate(expression):
            n = next(ids)
            await ws.send(json.dumps({"id": n, "method": "Runtime.evaluate",
                                      "params": {"expression": expression, "awaitPromise": True,
                                                 "returnByValue": True}}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == n:
                    res = m.get("result") or {}
                    if res.get("exceptionDetails") or "value" not in (res.get("result") or {}):
                        raise AssertionError(("the page threw", expression, res))
                    return res["result"]["value"]
        # The tab is listed as soon as it NAVIGATES; `probe` exists only once the stylesheet in <head> has loaded and
        # the script after it has run. A fixed one-second wait was "FLAKY UNDER LOAD" in the gate (KeyError: 'value':
        # probe() was a ReferenceError on a slow machine). Wait for the page itself, with a deadline.
        loop = asyncio.get_running_loop()
        end = loop.time() + 30
        while not await evaluate("document.readyState==='complete' && typeof probe==='function'"):
            assert loop.time() < end, "the page never finished loading"
            await asyncio.sleep(.1)
        return json.loads(await evaluate("probe()"))


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 700, 390])
def test_no_file_is_painted_above_the_files_toolbar(width):
    pytest.importorskip("websockets")
    cards = "".join('<div class="file-card"><div class="file-icon"><span>pdf</span></div>'
                    '<div class="meta"><span class="fname">file %d.pdf</span></div></div>' % i for i in range(200))
    page = PAGE.replace("CARDS", cards).encode()
    css = (ROOT / "static/css/client.css").read_bytes()

    class H(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            body, ct = (css, "text/css") if self.path.startswith("/client.css") else (page, "text/html")
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    tmp = tempfile.mkdtemp()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", f"--remote-debugging-port={port}",
                             f"--window-size={width},800", "--user-data-dir=" + tmp, url],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        r = asyncio.run(_run(url, port))
    finally:
        proc.kill()
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    assert r["scrolled"] > 0, ("the list did not scroll", r)
    assert not r["inFile"], ("files are painted above the toolbar, in a %dpx strip" % r["gap"], r["inFile"][:4])
