"""The APK in a pocket must go QUIET: no polling, no open relay sockets.

Reported as "bad battery drain on my phone… 10 percent used for today so far". The client was written
for a phone that FREEZES a backgrounded app, and that stopped being true the day notifications moved
to PosterChan Direct: its socket lives in a FOREGROUND SERVICE, and Android never freezes a process
that runs one. So the whole WebView kept running in the pocket — every relay socket open (each one
pinged by its relay every 30s), the online-count fetch every 15s, the reminder poll every 60s — a radio
wake every few seconds, all day, for a screen nobody was looking at. Closed-app notifications are the
native service's job; the page has nothing to do while hidden.

The real client, the real App-plugin signals; only HTTP, WebSocket and Capacitor are fixtures.
Measured: the window after the 20s grace must contain no request and no open socket, and coming back
must reconnect.
"""
import asyncio
import json
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client.test_effects_full_app import Browser, Handler, INIT

CAP = r'''
window.__appL={};
window.Capacitor={isNativePlatform:()=>true,getPlatform:()=>'android',Plugins:{
 App:{addListener:(n,f)=>{(__appL[n]=__appL[n]||[]).push(f);return{remove(){}}}},
 HomeScreen:{consumeLaunchView:async()=>({view:''}),addListener:()=>({remove(){}}),formFactor:async()=>({form:'phone'})}}};
'''
OPEN = "__sockets.filter(s=>s.readyState===1).length"


async def run(observe):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="pc-bg-quiet-") as profile:
        proc = subprocess.Popen(["/opt/google/chrome/chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
                                 "--remote-debugging-port=0", "--user-data-dir=" + profile, "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(300):
                if Path(profile, "DevToolsActivePort").exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, "DevToolsActivePort").read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get("http://127.0.0.1:" + port + "/json")).json()
            url = next(p for p in pages if p.get("type") == "page")["webSocketDebuggerUrl"]
            async with websockets.connect(url, max_size=20_000_000) as ws:
                b = Browser(ws)
                await b.call("Page.enable")
                await b.call("Network.setBlockedURLs", {"urls": ["https://*", "wss://*"]})
                await b.call("Emulation.setDeviceMetricsOverride", {"width": 412, "height": 900, "deviceScaleFactor": 2, "mobile": True})
                await b.call("Page.addScriptToEvaluateOnNewDocument", {"source": INIT + CAP})
                await b.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_port}/client"})
                await b.until("!!window.__PC && document.readyState==='complete'")
                await b.until("document.body.classList.contains('guest')")
                await b.js("(()=>{const key=new Uint8Array(32).fill(1);document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()")
                await b.until("!!__PC.me()")
                await asyncio.sleep(3)
                awake = await b.js(OPEN)
                # Home pressed: the Activity's signal, then the WebView's own.
                await b.js("(__appL.appStateChange||[]).forEach(f=>f({isActive:false}));"
                           "Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});"
                           "Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>'hidden'});"
                           "document.dispatchEvent(new Event('visibilitychange'))")
                await asyncio.sleep(22)          # the grace a glance at the notification shade gets
                before = await b.js("__requests.length")
                await asyncio.sleep(observe)
                got = {
                    "awake_sockets": awake,
                    "pocket_requests": await b.js(f"__requests.slice({before}).map(r=>r[0].split('?')[0])"),
                    "pocket_sockets": await b.js(OPEN),
                }
                await b.js("Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});"
                           "Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>'visible'});"
                           "document.dispatchEvent(new Event('visibilitychange'));"
                           "(__appL.appStateChange||[]).forEach(f=>f({isActive:true}))")
                await asyncio.sleep(3)
                got["back_sockets"] = await b.js(OPEN)
                got["errors"] = await b.js("__errors")
                await b.call("Browser.close")
                return got
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            server.shutdown()
            server.server_close()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_backgrounded_apk_makes_no_requests_and_holds_no_sockets_then_reconnects():
    got = asyncio.run(run(40))
    print(json.dumps(got))
    assert got["awake_sockets"] > 0, got
    assert got["pocket_requests"] == [], got
    assert got["pocket_sockets"] == 0, got
    assert got["back_sockets"] > 0, got
