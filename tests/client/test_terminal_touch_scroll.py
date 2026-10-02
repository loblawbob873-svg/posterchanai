"""A swipe scrolls the terminal on a phone, including inside full-screen programs.

"android term has no scrolling?" Measured on the vendored xterm.js: in a plain shell a touch swipe
scrolls the scrollback natively, but in the ALTERNATE screen (tmux, less, an editor, htop) a swipe sent
the program nothing, so it could not scroll at all. term.js now turns a swipe there into wheel events.
This runs the SHIPPED swipe code (lifted out of term.js) on the real vendored xterm with real touch
events: wheel reports in mouse mode, arrow keys without it, and the plain shell left to native scroll.
"""
import asyncio
import functools
import shutil
import subprocess
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client import test_effects_full_app as full

ROOT = Path(__file__).resolve().parents[2]
TERM_JS = (ROOT / "static/js/client/term.js").read_text(encoding="utf-8")


def _swipe_code():
    a = TERM_JS.index("let _swipeY = null, _swipeAcc = 0;")
    end = "box.addEventListener('touchend', () => { _swipeY = null; }, {passive:true});"
    b = TERM_JS.index(end, a) + len(end)
    return TERM_JS[a:b]


PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="xterm.css"></head><body style="margin:0;background:#000">
<div id="box" style="width:390px;height:600px"><div id="t" style="width:390px;height:600px"></div></div>
<script src="xterm.js"></script><script>
const box=document.getElementById('box');
const term=new Terminal({scrollback:5000,rows:30,cols:44}); term.open(document.getElementById('t'));
window.term=term; window.__sent=[]; term.onData(d=>window.__sent.push(d));
let s=''; for(let i=0;i<200;i++) s+='line '+i+'\\r\\n'; term.write(s,()=>{window.__ready=true;});
%s
</script></body></html>"""


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


async def _swipe(b, y0, y1):
    await b.call("Input.dispatchTouchEvent", dict(type="touchStart", touchPoints=[dict(x=195, y=y0)]))
    for i in range(1, 16):
        await b.call("Input.dispatchTouchEvent", dict(type="touchMove", touchPoints=[dict(x=195, y=y0 + (y1 - y0) * i // 15)]))
        await asyncio.sleep(.016)
    await b.call("Input.dispatchTouchEvent", dict(type="touchEnd", touchPoints=[]))
    await asyncio.sleep(.4)


async def run(tmp):
    for f in ("xterm.js", "xterm.css"):
        shutil.copy(ROOT / "static/vendor/xterm" / f, tmp / f)
    (tmp / "index.html").write_text(PAGE % _swipe_code())
    srv = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(tmp)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="pc-termtouch-", ignore_cleanup_errors=True) as prof:
        p = subprocess.Popen(["/opt/google/chrome/chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
                              "--remote-debugging-port=0", "--user-data-dir=" + prof, "about:blank"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(prof, "DevToolsActivePort").exists():
                    break
                await asyncio.sleep(.1)
            port = Path(prof, "DevToolsActivePort").read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get(f"http://127.0.0.1:{port}/json")).json()
            async with websockets.connect(next(x for x in pages if x["type"] == "page")["webSocketDebuggerUrl"]) as ws:
                b = full.Browser(ws)
                await b.call("Page.enable")
                await b.call("Emulation.setDeviceMetricsOverride", dict(width=390, height=700, deviceScaleFactor=2, mobile=True))
                await b.call("Emulation.setTouchEmulationEnabled", dict(enabled=True, maxTouchPoints=5))
                await b.call("Page.navigate", {"url": f"http://127.0.0.1:{srv.server_port}/index.html"})
                await b.until("window.__ready===true")
                out = {}
                before = await b.js("term.buffer.active.viewportY")
                await b.js("__sent.length=0")
                await _swipe(b, 150, 500)
                out["shell"] = dict(moved=before - await b.js("term.buffer.active.viewportY"), sent=await b.js("__sent.slice()"))
                await b.js("term.write('\\x1b[?1049h\\x1b[?1000h\\x1b[?1006h')")
                await asyncio.sleep(.2)
                await b.js("__sent.length=0")
                await _swipe(b, 150, 500)
                out["mouse"] = await b.js("__sent.slice()")
                await b.js("term.write('\\x1b[?1000l\\x1b[?1006l')")
                await asyncio.sleep(.2)
                await b.js("__sent.length=0")
                await _swipe(b, 500, 150)
                out["plain"] = await b.js("__sent.slice()")
                return out
        finally:
            p.terminate()
            p.wait(timeout=10)
            srv.shutdown()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_swipe_scrolls_the_shell_and_full_screen_programs(tmp_path):
    out = asyncio.run(run(tmp_path))
    assert out["shell"]["moved"] > 5 and out["shell"]["sent"] == [], ("the plain shell must scroll natively", out["shell"])
    assert len(out["mouse"]) >= 3 and all(s.startswith("\x1b[<64;") for s in out["mouse"]), (
        "a swipe DOWN in a mouse-mode program (tmux, vim) must send wheel-UP reports", out["mouse"])
    assert out["plain"] and all("\x1b[B" in s for s in out["plain"]), (
        "a swipe UP in less/man must send down-arrows", out["plain"])
