#!/usr/bin/env python3
"""The in-app Terminal draws block, box and braille characters as SOLID cells, the way a native terminal does.

    venv-unified/bin/python scripts/check_terminal_draws_solid_cells.py

"why is ptop still look like ass when I ssh into server1! you never fixed it, it looks like a totally different
app" -- reported 10-11, 10-12 and 10-15, and every round rendered ptop's frame somewhere other than where the user
looks at it. They look at it in THIS terminal (xterm.js; server1's sshd saw the session come from server1 itself, as
root, 201x81, TERM=xterm, truecolor, UTF-8). There, xterm's DOM renderer set every `█` as font text: each cell of a
bar kept a seam, the half-block portrait broke into tiles, braille graphs drew nothing, and the cells came out wider
than the font so the panels drifted. term.js now loads the WebGL renderer (@xterm/addon-webgl, customGlyphs).

Drives the SHIPPED term.js in a real Chrome (check_terminal_ui.py's page: the desktop's local PTY stubbed), writes a
row of ptop's bar characters into the live shell, and measures the screenshot:

  solid      along the middle of a 40-cell red `█` bar no column is darker than the bar -- no seam between cells.
  glyph      `▸` (the symbol face) is drawn, not an empty box: it has lit pixels in its left half only.
  fallback   with WebGL unavailable the terminal still mounts and shows the shell's prompt (the DOM renderer).

Reads PC_CHECK_PORT / PC_CHECK_PROFILE. Exit 0 pass, 1 fail, 2 could-not-run.
"""
import asyncio
import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_terminal_ui as base  # noqa: E402  (its page, stubs and static server)

ROOT = base.ROOT
PORT = int(os.environ.get("PC_CHECK_PORT") or 9547)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-terminal-cells-check"

# Capture the shell-output subscriber term.js registers on the stubbed local PTY, so the check can print into the
# live terminal exactly as a program would.
TAP = """<script>
(function(){ const p = window.pcTerm; if(!p) return; const on = p.onData;
  p.onData = (fn) => { (window.__ttyOut = window.__ttyOut || []).push(fn); return on(fn); }; })();
if(new URLSearchParams(location.search).get('nogl')){ HTMLCanvasElement.prototype.getContext = (function(g){
  return function(k, o){ return /webgl/i.test(k) ? null : g.call(this, k, o); }; })(HTMLCanvasElement.prototype.getContext); }
</script>
<script src="/static/js/client/term.js"></script>"""

RED = (255, 59, 92)
BAR = "\x1b[38;2;%d;%d;%dm" % RED + "█" * 40 + "\x1b[0m"
LINE = "\r\n\r\n  " + BAR + "\r\n  \x1b[38;2;214;222;255m▸\x1b[0m\r\n"


def _png(b64):
    from PIL import Image
    return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")


async def drive(url):
    import websockets
    shutil.rmtree(PROFILE, ignore_errors=True)
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium") or shutil.which("chromium-browser"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    # WebGL through SwiftShader: headless Chrome has no GPU, and --disable-gpu would test only the fallback.
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--no-sandbox", "--hide-scrollbars", "--use-angle=swiftshader",
         "--enable-unsafe-swiftshader", f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
         "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    problems = []
    try:
        page = None
        for _ in range(120):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
                page = [t for t in tabs if t["type"] == "page"][0]
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not page:
            print("SKIP  could not start Chrome")
            return 2
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        return msg.get("result") or {}

            async def js(expr, awaited=False):
                r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": awaited})
                if r.get("exceptionDetails"):
                    print("  js error: " + json.dumps(r["exceptionDetails"])[:600])
                    return None
                return (r.get("result") or {}).get("value")

            async def until(expr, tries=80):
                for _ in range(tries):
                    if await js(expr):
                        return True
                    await asyncio.sleep(0.25)
                return False

            await call("Page.enable")
            await call("Runtime.enable")
            await call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 800, "deviceScaleFactor": 1,
                                                              "mobile": False})

            # --- solid + glyph ---------------------------------------------------------------------------------
            await call("Page.navigate", {"url": url + "/page.html"})
            if not await until("!!window.__ready && (window.__ttyOut||[]).length > 0 && !!document.querySelector('.xterm')"):
                print("SKIP  the terminal never mounted")
                return 2
            await until("!!document.querySelector('.xterm-screen canvas')", 40)   # the GPU renderer, if it comes
            sid = await js("(()=>{const h=document.querySelector('.tty-tab.on,[data-sid].on');return h&&h.dataset.sid||''})()")
            await js("(window.__ttyOut||[]).forEach(fn=>{for(const id of ['2','3','4'])fn({id,t:'out',d:%s,seq:9e6}); });"
                     % json.dumps(LINE))
            await asyncio.sleep(1.2)
            box = await js("(()=>{const r=document.querySelector('.xterm-screen').getBoundingClientRect();"
                           "const c=document.querySelector('.xterm-helper-textarea');"
                           "return {x:r.left,y:r.top,w:r.width,h:r.height}})()")
            shot = _png((await call("Page.captureScreenshot", {"format": "png"}))["data"])
            if os.environ.get("PC_CELLS_SHOT"):
                shot.save(os.environ["PC_CELLS_SHOT"])
            # Find the bar: the longest run of RED pixels on any row of the terminal.
            best = None
            for y in range(int(box["y"]), int(box["y"] + box["h"])):
                run = start = 0
                for x in range(int(box["x"]), int(box["x"] + box["w"])):
                    r, g, b = shot.getpixel((x, y))
                    if abs(r - RED[0]) < 40 and abs(g - RED[1]) < 40 and abs(b - RED[2]) < 40:
                        if run == 0:
                            start = x
                        run += 1
                        if not best or run > best[2]:
                            best = (y, start, run)
                    else:
                        run = 0
            if not best or best[2] < 200:
                problems.append("solid: no red bar was drawn at all (%r) -- sid %r" % (best, sid))
            else:
                ys = [y for y in range(best[0] - 12, best[0] + 12)
                      if sum(1 for x in range(best[1], best[1] + best[2])
                             if max(abs(a - b) for a, b in zip(shot.getpixel((x, y)), RED)) < 40) > best[2] * 0.6]
                mid = ys[len(ys) // 2] if ys else best[0]
                # Exactly the bar's colour, cell after cell: the DOM renderer's seams are ~37 levels darker (255 -> 218),
                # the GPU renderer's row is uniform. 12 levels absorbs antialiasing at the two ENDS only.
                xs = [x for x in range(int(box["x"]), int(box["x"] + box["w"]))
                      if max(abs(a - b) for a, b in zip(shot.getpixel((x, mid)), RED)) < 60]
                seams = [x for x in range(xs[0] + 2, xs[-1] - 1)
                         if max(abs(a - b) for a, b in zip(shot.getpixel((x, mid)), RED)) > 12] if xs else []
                # Two bars on consecutive lines are ONE block vertically (what a half-block portrait relies on).
                cx = (xs[0] + xs[-1]) // 2 if xs else 0
                col = [y for y in range(mid - 40, mid + 60)
                       if max(abs(a - b) for a, b in zip(shot.getpixel((cx, y)), RED)) <= 12]
                vgaps = [y for y in range(col[0], col[-1]) if y not in set(col)] if col else []
                if seams or vgaps:
                    problems.append("solid: %d seam pixel(s) between cells (first x=%s) and %d between lines (first y=%s) -- "
                                    "block characters are drawn as font text" % (len(seams), seams[:1], len(vgaps), vgaps[:1]))
                else:
                    print("PASS  solid: two stacked 40-cell bars are one unbroken block, %d x %d px"
                          % (xs[-1] - xs[0] + 1, col[-1] - col[0] + 1))
                # glyph: the row two lines under the bar holds the ▸ at the bar's first cell.
                cw = (xs[-1] - xs[0] + 1) / 40.0 if xs else 8
                ch = await js("(()=>{const t=document.querySelector('.xterm-rows>div,.xterm-screen');"
                              "return t?Math.round(document.querySelector('.xterm-screen').getBoundingClientRect().height/"
                              "Math.max(1,(window.__lastRows||0)||document.querySelectorAll('.xterm-rows>div').length||24)):17})()")
                lit_l = lit_r = 0
                gy0 = (col[-1] if col else mid) + 2
                for y in range(gy0, gy0 + int(2.2 * (ch or 17))):
                    for x in range(xs[0], xs[0] + int(cw)):
                        if sum(shot.getpixel((x, y))) > 300:
                            if x < xs[0] + cw / 2:
                                lit_l += 1
                            else:
                                lit_r += 1
                if lit_l == 0:
                    problems.append("glyph: nothing drawn where ▸ should be")
                elif lit_r > lit_l * 0.6:
                    problems.append("glyph: ▸ drew as a box (left %d, right %d lit pixels) -- the symbol face was "
                                    "not loaded before the renderer drew it" % (lit_l, lit_r))
                else:
                    print("PASS  glyph: ▸ is a right-pointing triangle (left %d, right %d lit pixels)" % (lit_l, lit_r))

            # --- fallback ----------------------------------------------------------------------------------------
            await call("Page.navigate", {"url": url + "/page.html?nogl=1"})
            if not await until("!!window.__ready && !!document.querySelector('.xterm')"):
                problems.append("fallback: with no WebGL the terminal never mounted")
            else:
                await asyncio.sleep(1.0)
                text = await js("(document.querySelector('.xterm-rows')||{}).textContent||''")
                if "user@box" not in (text or ""):
                    problems.append("fallback: with no WebGL the prompt is not on screen (%r)" % (text or "")[:120])
                else:
                    print("PASS  fallback: no WebGL -> the DOM renderer shows the shell")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(PROFILE, ignore_errors=True)
    for p in problems:
        print("FAIL  " + p)
    return 1 if problems else 0


def main():
    import importlib.util
    for mod in ("websockets", "PIL"):
        if importlib.util.find_spec(mod) is None:
            print("SKIP  %s not installed" % mod)
            return 2
    for p in ("static/js/client/term.js", "static/vendor/xterm/xterm.js", "static/vendor/xterm/webgl.js"):
        if not os.path.exists(os.path.join(ROOT, p)):
            print(f"SKIP  {p} is missing -- re-point this check")
            return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="termcells-")
    with open(os.path.join(tmp, "page.html"), "w") as fh:
        fh.write(base.PAGE.replace("__HTMLCLS__", "").replace("__CHROME__", "")
                 .replace('<script src="/static/js/client/term.js"></script>', TAP))

    class H(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            path = path.split("?")[0].split("#")[0]
            if path == "/static/js/client/term.js" and os.environ.get("PC_TERM_JS"):
                return os.environ["PC_TERM_JS"]                 # the pre-fix file, to prove this check can fail
            if path.startswith("/static/"):
                return os.path.join(ROOT, path.lstrip("/"))
            return os.path.join(tmp, path.lstrip("/") or "page.html")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        return asyncio.run(drive(f"http://127.0.0.1:{srv.server_port}"))
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
