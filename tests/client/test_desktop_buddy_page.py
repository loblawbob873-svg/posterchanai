"""desktop/buddy.html -- the desktop PosterChan's own window on PosterChanOS.

The page Electron loads (as a file) into her always-on-top window: it must show her frames at the
chosen pace, react to a click, offer Hide on right-click, and report a drag as the offset from where
she was grabbed (desktop/buddy-host.js moves the window by it). Real mouse input in headless Chrome,
loaded from file:// exactly as Electron loads it, with the bridge recorded.
"""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client.test_effects_full_app import Browser

ROOT = Path(__file__).resolve().parents[2]
CHROME = "/opt/google/chrome/chrome"
BRIDGE = ("window.__log=[];window.pcBuddyWin={drag:(x,y)=>__log.push(['drag',x,y]),"
          "drop:()=>__log.push(['drop']),menu:a=>__log.push(['menu',a])};")


async def _mouse(b, kind, x, y, button="left", buttons=1):
    await b.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": button,
                                              "buttons": buttons, "clickCount": 1})


async def _run(page, hash=""):
    res = {}
    with tempfile.TemporaryDirectory(prefix="pc-buddy-page-") as profile:
        port = int(os.environ.get("PC_CHECK_PORT", "0"))
        proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--window-size=174,284",
                                 f"--remote-debugging-port={port}", "--user-data-dir=" + profile, "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(150):
                if Path(profile, "DevToolsActivePort").exists():
                    break
                await asyncio.sleep(.1)
            dport = Path(profile, "DevToolsActivePort").read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get(f"http://127.0.0.1:{dport}/json")).json()
            url = next(p for p in pages if p.get("type") == "page")["webSocketDebuggerUrl"]
            async with websockets.connect(url, max_size=20_000_000) as ws:
                b = Browser(ws)
                await b.call("Page.enable")
                await b.call("Emulation.setDeviceMetricsOverride", {"width": 174, "height": 284, "deviceScaleFactor": 1, "mobile": False})
                await b.call("Page.addScriptToEvaluateOnNewDocument", {"source": BRIDGE})
                await b.call("Page.navigate", {"url": page.as_uri() + hash})
                for _ in range(100):
                    if await b.js("!!window.__buddy && document.getElementById('im').complete"):
                        break
                    await asyncio.sleep(.1)
                res["loaded"] = await b.js("document.getElementById('im').naturalWidth")
                res["src"] = await b.js("document.getElementById('im').getAttribute('src')")
                res["hide_label"] = await b.js("document.querySelector('#menu [data-a=hide]').textContent")
                res["switch_label"] = await b.js("document.querySelector('#menu [data-a=switch]').textContent")
                res["pace"] = await b.js("new Promise(ok=>{let n=0,last=__buddy.frame();const t=setInterval(()=>{const f=__buddy.frame();if(f!==last){n++;last=f}},40);setTimeout(()=>{clearInterval(t);ok(n)},4000)})")
                # Click (no movement): she reacts with a line.
                await _mouse(b, "mousePressed", 87, 160); await _mouse(b, "mouseReleased", 87, 160, buttons=0)
                await asyncio.sleep(.1)
                res["said"] = await b.js("document.getElementById('say').classList.contains('on')")
                # Drag: grabbed at (87,160), pointer travels to (137,180).
                await b.js("__log.length=0;true")
                await _mouse(b, "mousePressed", 87, 160)
                for i in range(1, 6):
                    await b.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 87 + 10 * i, "y": 160 + 4 * i,
                                                              "button": "left", "buttons": 1})
                await _mouse(b, "mouseReleased", 137, 180, buttons=0)
                await asyncio.sleep(.1)
                res["drag"] = await b.js("__log")
                # Right-click -> menu -> Hide.
                await b.js("__log.length=0;true")
                await _mouse(b, "mousePressed", 87, 160, "right", 2); await _mouse(b, "mouseReleased", 87, 160, "right", 0)
                await asyncio.sleep(.1)
                res["menu"] = await b.js("document.getElementById('menu').classList.contains('on')")
                await b.js("document.querySelector('#menu [data-a=hide]').click();true")
                res["hide"] = await b.js("__log")
                await b.js("__log.length=0;document.querySelector('#menu [data-a=switch]').click();true")
                res["switch"] = await b.js("__log")
                # The host switches dancer by loading the same file with another #fragment: the page
                # must actually become the other one (a fragment change alone reloads nothing).
                other = "#posterchan" if hash == "#axolotl" else "#axolotl"
                await b.js(f"location.hash={json.dumps(other)};true")
                for _ in range(100):
                    src = await b.js("(document.getElementById('im')||{}).getAttribute&&document.getElementById('im').getAttribute('src')||''")
                    if (("/mascot/axolotl/" in src) if other == "#axolotl" else ("/mascot/dance/" in src)):
                        break
                    await asyncio.sleep(.1)
                res["after_switch_src"] = src
        finally:
            proc.kill()
    return res


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome required")
def test_her_window_page_dances_reacts_drags_and_hides():
    with tempfile.TemporaryDirectory(prefix="pc-buddy-files-") as d:
        # Same layout as the packaged app: buddy.html beside www/ (desktop/build-www.sh copies the frames).
        shutil.copy(ROOT / "desktop/buddy.html", Path(d, "buddy.html"))
        Path(d, "www/static/mascot").mkdir(parents=True)
        shutil.copytree(ROOT / "static/mascot/dance", Path(d, "www/static/mascot/dance"))
        shutil.copytree(ROOT / "static/mascot/axolotl", Path(d, "www/static/mascot/axolotl"))
        res = asyncio.run(_run(Path(d, "buddy.html")))
        axo = asyncio.run(_run(Path(d, "buddy.html"), "#axolotl"))
    assert "/mascot/dance/" in res["src"] and res["hide_label"] == "Hide PosterChan", res
    assert axo["loaded"] > 0 and "/mascot/axolotl/" in axo["src"], ("the axolotl did not load in her window", axo)
    assert axo["hide_label"] == "Hide Axolotl" and 3 <= axo["pace"] <= 6, axo
    # "there is no switch to posterchan": her own window's menu offers the other dancer.
    assert axo["switch_label"] == "Switch to PosterChan" and axo["switch"] == [["menu", "switch"]], axo
    assert res["switch_label"] == "Switch to Axolotl", res
    # "can't change axolotl to posterchan": switching on an OPEN page draws the other dancer.
    assert "/mascot/dance/" in axo["after_switch_src"], ("the open page kept the old dancer", axo["after_switch_src"])
    assert "/mascot/axolotl/" in res["after_switch_src"], ("the open page kept the old dancer", res["after_switch_src"])
    assert res["loaded"] > 0, "her frame did not load from file:// (the page's CSP or the path)"
    assert 3 <= res["pace"] <= 6, ("not the chosen pace (900ms a frame)", res["pace"])
    assert res["said"], "a click did nothing"
    drags = [e for e in res["drag"] if e[0] == "drag"]
    assert drags and drags[-1] == ["drag", 50, 20], ("a drag must report the offset from the grab point", res["drag"])
    assert res["drag"][-1] == ["drop"], res["drag"]
    # Opening her menu hands the keyboard back ("release"); choosing Hide sends hide.
    assert res["menu"] and [c for c in res["hide"] if c != ["menu", "release"]] == [["menu", "hide"]], res
