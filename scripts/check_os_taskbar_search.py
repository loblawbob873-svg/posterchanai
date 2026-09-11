#!/usr/bin/env python3
"""ONE CLICK ON THE TASKBAR SEARCH BOX MUST BE ENOUGH TO TYPE IN IT.

Reported as "have to click search on the taskbar twice to be able to type in search query".

THE MECHANISM IS ONE THIS FILE'S NEIGHBOUR ALREADY DOCUMENTS TWICE. `drawBar()` rebuilds
`bar.innerHTML`, which DETACHES the search input — and several things call it from a `pointerdown`
handler (the tray click-away closing a panel, a window-focus change). A browser focuses an input on
mousedown, so the sequence is:

    pointerdown  →  something calls drawBar()  →  the input the click was aimed at is detached
                 →  the browser's focus lands on a node that is no longer in the document
                 →  no caret, and the keystrokes go nowhere

The second click works because nothing rebuilds the bar that time, which is exactly what makes it
read as flaky rather than broken. `barQuery`/`barFocused` were written to survive a rebuild, but
they read `document.activeElement` — and at pointerdown time the focus has NOT LANDED YET, so the
restore has nothing to restore.

This drives the SHIPPED os.js in a real headless Chrome with real mouse events, and TYPES: the
assertion is that characters arrive in the box, not that some element reports focus. It checks the
plain case and the case where a tray panel is open, because only the second one used to fail and a
check that tests the easy path would have passed throughout.

Exit 2 = could not run, never a pass.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_os_desktop import PAGE, ROOT  # noqa: E402  (the shipped desktop, one definition)

PORT = int(os.environ.get("PC_CHECK_PORT") or 9471)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-os-taskbar-search"


async def rpc(ws, method, params=None, ident=[0]):
    ident[0] += 1
    mine = ident[0]
    await ws.send(json.dumps({"id": mine, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == mine:
            if "error" in msg:
                raise RuntimeError(msg["error"])
            return msg.get("result", {})


async def js(ws, expr):
    got = await rpc(ws, "Runtime.evaluate",
                    {"expression": expr, "awaitPromise": True, "returnByValue": True})
    if got.get("exceptionDetails"):
        raise RuntimeError(got["result"].get("description") or got["exceptionDetails"])
    return got.get("result", {}).get("value")


async def click(ws, x, y):
    """A REAL mouse press and release. The whole bug lives between the two — a synthetic
    `el.click()` or `el.focus()` skips the pointerdown that triggers the rebuild."""
    for kind in ("mousePressed", "mouseReleased"):
        await rpc(ws, "Input.dispatchMouseEvent",
                  {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1})
        await asyncio.sleep(.05)


async def typed(ws, text):
    for ch in text:
        await rpc(ws, "Input.dispatchKeyEvent", {"type": "keyDown", "text": ch})
        await rpc(ws, "Input.dispatchKeyEvent", {"type": "keyUp"})
        await asyncio.sleep(.02)
    return await js(ws, "(document.querySelector('#os-q-bar')||{}).value || ''")


#: Clearing the box the way a person does — through the event the app listens to.
CLEAR = """(()=>{const e=document.querySelector('#os-q-bar');if(!e)return;
  e.value='';e.dispatchEvent(new Event('input',{bubbles:true}));e.blur();})()"""


async def box(ws):
    return await js(ws, """(()=>{const e=document.querySelector('#os-q-bar');
      if(!e)return null;const r=e.getBoundingClientRect();
      return {x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2),
              w:r.width,h:r.height};})()""")


async def one_click_then_type(ws, label, problems, prepare=None):
    # Clear through the INPUT EVENT, not the DOM value: the text lives in a module variable that
    # survives the rebuild (that is the whole point of it), and drawBar restores from there — so
    # setting `.value` alone leaves the box repainting yesterday's query.
    await js(ws, CLEAR)
    if prepare:
        await js(ws, prepare)
        await asyncio.sleep(.25)
    where = await box(ws)
    if not where or where["w"] < 10:
        problems.append(f"{label}: the taskbar search box is not on screen: {where}")
        return
    await click(ws, where["x"], where["y"])
    await asyncio.sleep(.25)
    got = await typed(ws, "hello")
    if got != "hello":
        focused = await js(ws, "(document.activeElement||{}).id || document.activeElement?.tagName || '?'")
        connected = await js(ws, "!!document.querySelector('#os-q-bar')")
        problems.append(
            f"{label}: after ONE click the box holds {got!r} instead of 'hello' — the keystrokes "
            f"went to {focused!r} (search input present: {connected}). That is the "
            "'have to click search twice' report.")


async def drive(url):
    import websockets
    shutil.rmtree(PROFILE, ignore_errors=True)
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--window-size=1440,900",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    problems = []
    try:
        page = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
                page = [t for t in tabs if t["type"] == "page"][0]
                break
            except Exception:
                await asyncio.sleep(.5)
        if not page:
            print("SKIP  could not start Chrome")
            return 2
        async with websockets.connect(page["webSocketDebuggerUrl"],
                                      max_size=64 * 1024 * 1024) as ws:
            await rpc(ws, "Runtime.enable")
            await rpc(ws, "Page.enable")
            await rpc(ws, "Emulation.setDeviceMetricsOverride",
                      {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
            await rpc(ws, "Page.navigate", {"url": url})
            for _ in range(120):
                if await js(ws, "window.__ready === true && !!window.PCOS"):
                    break
                await asyncio.sleep(.1)
            else:
                print("SKIP  the desktop module never loaded")
                return 2
            # The taskbar only exists once the desktop is entered — the same call the instance logo
            # makes at >=1024px.
            await js(ws, "PCOS.enter()")
            for _ in range(120):
                if await js(ws, "!!document.querySelector('#os-q-bar')"):
                    break
                await asyncio.sleep(.1)
            else:
                print("SKIP  the desktop never painted its taskbar")
                return 2
            await asyncio.sleep(.4)

            # 1. Nothing else going on.
            await one_click_then_type(ws, "plain", problems)

            # 2. A tray panel is open. Clicking away closes it, and closing it calls drawBar() from
            #    a pointerdown handler — which is what detaches the input mid-click.
            await one_click_then_type(
                ws, "with the notification centre open", problems,
                prepare="(()=>{const b=document.querySelector('#os-bell');if(b)b.click();})()")

            # 3. The start menu is open, for the same reason by a different route.
            await one_click_then_type(
                ws, "with the start menu open", problems,
                prepare="(()=>{const s=document.querySelector('#os-start');if(s)s.click();})()")

            # 4. And a redraw landing WHILE the click is happening — a relay reconnect, a clock
            #    tick, a window focus change. The restore must survive it.
            await js(ws, CLEAR)
            where = await box(ws)
            await rpc(ws, "Input.dispatchMouseEvent",
                      {"type": "mousePressed", "x": where["x"], "y": where["y"],
                       "button": "left", "clickCount": 1})
            await js(ws, "try{ PCOS && PCOS.redrawBar && PCOS.redrawBar(); }catch(_){}")
            await rpc(ws, "Input.dispatchMouseEvent",
                      {"type": "mouseReleased", "x": where["x"], "y": where["y"],
                       "button": "left", "clickCount": 1})
            await asyncio.sleep(.3)
            got = await typed(ws, "abc")
            if got != "abc":
                problems.append(
                    "a taskbar redraw between press and release lost the caret: the box holds "
                    f"{got!r}")
    finally:
        proc.terminate()
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(PROFILE, ignore_errors=True)

    if problems:
        print("FAIL " + "\nFAIL ".join(problems))
        return 1
    print("OK  one click on the taskbar search box is enough to type in it")
    return 0


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    tmp = tempfile.mkdtemp(prefix="ostaskbar-")
    with open(os.path.join(tmp, "index.html"), "w") as fh:
        fh.write(PAGE)

    class H(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            path = path.split("?")[0].split("#")[0]
            if path.startswith("/static/"):
                return os.path.join(ROOT, path.lstrip("/"))
            return os.path.join(tmp, path.lstrip("/") or "index.html")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    try:
        return asyncio.run(drive(url))
    finally:
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
