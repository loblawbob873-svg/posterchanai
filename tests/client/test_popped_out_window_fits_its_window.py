"""A POPPED-OUT WINDOW MUST END WHERE THE WINDOW ENDS.

Reported: *"concord chat is cut off on the bottom, you conly see half the message input box"*, and
the same shape had already been visible as a clipped last post on the timeline without anybody
naming it.

`html.pc-oswin body` reserves 38px at the top for the fixed title bar the window draws itself, and
`.app` is `height:100vh` — sized from the VIEWPORT, which knows nothing about that padding. So the
column is a full screen tall starting 38px down and the bottom 38 device-pixels are below the fold.
Measured on the real desktop in four windows at once: feed bottom 1447 against an innerHeight of
1409, 1968 against 1930, 1793 against 1755, and 1564 against 1535 in the Concord window — where the
bottom of the view IS the message composer.

Why nothing saw it: every other layout check runs in the ORDINARY page, where there is no
`html.pc-oswin` and no title bar, and `check_os_desktop.py` drives the window manager against a stub
that paints a placeholder. This one LAYS OUT the shipped stylesheet in a real browser at the heights
a window is actually opened at and measures the bottom edge — so a new way of being too tall fails
it, not only the one that was fixed.
"""
from pathlib import Path
import http.server
import json
import shutil
import socketserver
import subprocess
import tempfile
import threading
import urllib.request

import pytest


ROOT = Path(__file__).resolve().parents[2]
CSS = ROOT / "static/css/client.css"
HEIGHTS = (760, 1409, 1535, 1755)

PAGE = """<!doctype html><html class="pc-oswin"><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/css/client.css"></head>
<body class="native desktop">
  <header id="pc-oswin-chrome"><span class="pc-oswin-title">a window</span>
    <span class="pc-oswin-buttons"><button data-action="close">x</button></span></header>
  <div class="app" id="app"><aside class="sidebar"></aside>
    <main class="main"><div id="feed" class="feed"></div></main></div>
</body></html>"""


def test_the_override_is_still_correcting_the_rule_it_names():
    """A static floor, so the browser half below cannot pass for the wrong reason."""
    css = CSS.read_text(encoding="utf-8")
    reserved = int(css.split("html.pc-oswin body{padding-top:", 1)[1].split("px", 1)[0])
    assert reserved > 0
    rule = [l for l in css.splitlines() if l.startswith("html.pc-oswin #app")]
    assert rule, "nothing re-sizes the app column inside a popped-out window"
    assert "height:100%!important" in rule[0] and "min-height:0!important" in rule[0], rule
    app = next(l for l in css.splitlines() if l.startswith(".app{"))
    assert "height:100vh" in app, (
        "the .app height changed — re-measure a popped-out window before trusting this override")


def test_nothing_extends_past_the_bottom_of_the_window():
    chrome = (shutil.which("google-chrome") or shutil.which("google-chrome-stable")
              or shutil.which("chromium"))
    if not chrome:
        pytest.skip("no chrome on this node")
    try:
        import websockets  # noqa: F401
    except Exception:
        pytest.skip("websockets module missing")
    import asyncio
    import websockets as ws_mod

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "win.html").write_text(PAGE, encoding="utf-8")

        class H(http.server.SimpleHTTPRequestHandler):
            def translate_path(self, path):
                path = path.split("?", 1)[0]
                if path.startswith("/static/"):
                    return str(ROOT / path.lstrip("/"))
                return str(root / (path.lstrip("/") or "win.html"))

            def log_message(self, *a):
                pass

        srv = socketserver.TCPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        profile = root / "profile"
        proc = subprocess.Popen(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--remote-debugging-port=0", f"--user-data-dir={profile}",
             "--window-size=1200,900", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

        async def measure():
            devtools = None
            for _ in range(80):
                await asyncio.sleep(0.25)
                port_file = profile / "DevToolsActivePort"
                if port_file.is_file():
                    try:
                        devtools = int(port_file.read_text().splitlines()[0])
                        break
                    except Exception:
                        continue
            if not devtools:
                pytest.skip("chrome never wrote its DevToolsActivePort")
            d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{devtools}/json/version"))
            async with ws_mod.connect(d["webSocketDebuggerUrl"], max_size=None) as c:
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
                            return r.get("result", {})

                t = await call("Target.createTarget", {"url": "about:blank"})
                sess = (await call("Target.attachToTarget",
                                   {"targetId": t["targetId"], "flatten": True}))["sessionId"]
                await call("Page.enable", None, sess)
                await call("Runtime.enable", None, sess)
                bad = []
                for h in HEIGHTS:
                    await call("Emulation.setDeviceMetricsOverride",
                               {"width": 1200, "height": h, "deviceScaleFactor": 1,
                                "mobile": False}, sess)
                    await call("Page.navigate", {"url": f"http://127.0.0.1:{port}/win.html"}, sess)
                    await asyncio.sleep(0.6)
                    r = await call("Runtime.evaluate", {"expression": """(()=>{
                      const rows=[];
                      for (const sel of ['#app','.main','#feed']) {
                        const el=document.querySelector(sel); if(!el) continue;
                        const r=el.getBoundingClientRect();
                        rows.push([sel, Math.round(r.bottom), Math.round(r.bottom-innerHeight)]);
                      }
                      return JSON.stringify({inner:innerHeight,rows});})()""",
                        "returnByValue": True}, sess)
                    got = json.loads(r["result"]["value"])
                    for sel, bottom, over in got["rows"]:
                        # A pixel of rounding is not a report; the failure was 29-38.
                        if over > 2:
                            bad.append(f"at {h}px tall, {sel} ends {over}px past the window "
                                       f"(bottom {bottom}, window {got['inner']})")
                return bad

        try:
            bad = asyncio.run(measure())
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            srv.shutdown()
    assert bad == [], "\n".join(bad)
