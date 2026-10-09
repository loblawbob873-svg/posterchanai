"""DESKTOP + TOR + NO NETWORK: OPEN THE APP NOW, WITHOUT EVER TURNING TOR OFF.

The boot card held the window for up to 120 s and its only way out was "Continue without Tor" — which
switches Tor OFF, so a machine opened offline later came back online and talked in the clear. Now the
card offers "Open offline — Tor stays on" (immediately when there is no network): the app loads with the
proxy still on Tor's SOCKS port, so every request fails closed until Tor bootstraps.
Two halves: the shipped boot.html in real Chrome, and the shipped openKeepingTor/applyProxy under node.
"""
import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
import pytest
import websockets

ROOT = Path(__file__).resolve().parents[1]
CHROME = "/opt/google/chrome/chrome"
NODE = shutil.which("node")

STUB = r"""<script>window.__calls=[];
Object.defineProperty(navigator,'onLine',{configurable:true,get:()=>false});
window.pcShell={tor:{status:async()=>({enabled:true,progress:0}),onStatus:()=>{},
  set:async o=>{__calls.push(['set',o]);},restart:async()=>{__calls.push(['restart']);},
  openOffline:async()=>{__calls.push(['openOffline']);}}};</script>"""


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome required")
def test_the_boot_card_offers_opening_offline_with_tor_kept_on():
    from tests.client.test_effects_full_app import Browser
    html = (ROOT / "desktop/boot.html").read_text()
    page = html.replace("<head>", "<head>" + STUB, 1) if "<head>" in html else STUB + html

    async def run():
        with tempfile.TemporaryDirectory(prefix="pc-tor-boot-") as profile:
            proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=0",
                                     "--user-data-dir=" + profile, "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(150):
                    if Path(profile, "DevToolsActivePort").exists():
                        break
                    await asyncio.sleep(.1)
                port = Path(profile, "DevToolsActivePort").read_text().splitlines()[0]
                async with httpx.AsyncClient(trust_env=False) as h:
                    pages = (await h.get(f"http://127.0.0.1:{port}/json")).json()
                url = next(p for p in pages if p.get("type") == "page")["webSocketDebuggerUrl"]
                async with websockets.connect(url, max_size=20_000_000) as ws:
                    b = Browser(ws)
                    await b.call("Page.enable")
                    frame = (await b.call("Page.getFrameTree"))["frameTree"]["frame"]["id"]
                    await b.call("Page.setDocumentContent", {"frameId": frame, "html": page})
                    await asyncio.sleep(.5)
                    got = {"visible": await b.js("!document.getElementById('offline').classList.contains('hidden')"),
                           "title": await b.js("document.getElementById('title').textContent")}
                    await b.js("document.getElementById('offline').click()")
                    await asyncio.sleep(.2)
                    got["calls"] = await b.js("__calls")
                    return got
            finally:
                proc.kill()

    got = asyncio.run(run())
    assert got["visible"], ("with no network the card must offer opening offline at once", got)
    assert "No network" in got["title"], got
    assert got["calls"] == [["openOffline"]], ("opening offline must never switch Tor off", got)


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_opening_offline_keeps_the_proxy_on_tor_and_never_goes_direct():
    src = (ROOT / "desktop/main.js").read_text()
    def fn(name):
        i = src.index("async function " + name + "(")
        depth, j = 0, src.index("{", i)
        while True:
            depth += {"{": 1, "}": -1}.get(src[j], 0)
            if depth == 0:
                return src[i:j + 1]
            j += 1
    script = r"""
const order=[];let enabled=true;
const tor={status:()=>({enabled}),proxyRules:()=>'socks5://127.0.0.1:41234'};
const session={defaultSession:{setProxy:async o=>{order.push(['proxy',o]);}}};
const loadGens=new WeakMap();const APP_URL='app://posterchan/index.html';
const win={isDestroyed:()=>false,loadURL:async u=>{order.push(['load',u]);}};
""" + fn("applyProxy") + "\n" + fn("openKeepingTor") + r"""
(async()=>{const a=await openKeepingTor(win);enabled=false;const b=await openKeepingTor(win);
console.log(JSON.stringify({a,b,order}));})();"""
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-1500:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["a"] is True and got["b"] is False, got
    assert got["order"][0] == ["proxy", {"proxyRules": "socks5://127.0.0.1:41234"}], ("the proxy was not on Tor first", got)
    assert got["order"][1] == ["load", "app://posterchan/index.html"], got
    assert not any(o[0] == "proxy" and o[1].get("mode") == "direct" for o in got["order"]), ("went direct", got)
