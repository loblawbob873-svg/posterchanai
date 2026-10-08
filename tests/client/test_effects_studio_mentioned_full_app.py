"""The Effects studio asks what got mentioned, and sends it.

Reported: "mention not working? response is Say what got mentioned, e.g. mentioned pizza." The studio had one
text box, a meme CAPTION, so picking Mentioned could only ever build a bare `mentioned` (or `mentioned meme
pizza`, which is a caption over the meme, not the effect's word) and the server rightly asked for the word.
With Mentioned picked the box now asks "What got mentioned?", Apply waits for an answer, the word goes right
after the effect name (before modifiers, which the server strips from the END), and reopening the studio on
that command puts the word back in its box.
"""
import asyncio
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client import test_effects_full_app as fx

INIT = fx.INIT.replace("effects:[{name:'glow',description:'Glow'}],motions:[]",
                       "effects:[{name:'glow',description:'Glow'},{name:'mentioned',description:'MENTIONED'}],"
                       "motions:['zoom','shake','pulse','trippy']")
assert INIT != fx.INIT, "fixture: the effects catalogue stub moved"


async def run(check):
    server = ThreadingHTTPServer(("127.0.0.1", 0), fx.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="pc-fx-mentioned-") as profile:
        proc = subprocess.Popen(["/opt/google/chrome/chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
                                 "--window-size=1440,1000", "--remote-debugging-port=0", "--user-data-dir=" + profile,
                                 "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(200):
                if Path(profile, "DevToolsActivePort").exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, "DevToolsActivePort").read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get("http://127.0.0.1:" + port + "/json")).json()
            url = next(p for p in pages if p.get("type") == "page")["webSocketDebuggerUrl"]
            async with websockets.connect(url, max_size=20_000_000) as ws:
                b = fx.Browser(ws)
                await b.call("Page.enable")
                await b.call("Network.setBlockedURLs", {"urls": ["https://*", "wss://*"]})
                await b.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 860, "deviceScaleFactor": 1, "mobile": True})
                await b.call("Page.addScriptToEvaluateOnNewDocument", {"source": "window.__hasChats=false;" + INIT})
                await b.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_port}/client"})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                await b.until("document.body.classList.contains('guest')")
                await b.js("""(()=>{const key=new Uint8Array(32).fill(1);window.__events=[NostrTools.finalizeEvent({kind:1,created_at:Math.floor(Date.now()/1000),content:'pic '+location.origin+'/fixture.png',tags:[]},key)];
                  document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()""")
                await b.until("!!__PC.me() && document.querySelectorAll('.note').length>=1")
                await check(b)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
            server.shutdown()
            server.server_close()


async def open_studio(b):
    await b.js("__PC.switchView('global')")
    await b.until("!!document.querySelector('.note [data-a=menu]')")
    await b.js("document.querySelector('.note [data-a=menu]').click()")
    await b.until("!!document.querySelector('.menu-pop [data-m=effect]')")
    await b.js("document.querySelector('.menu-pop [data-m=effect]').click()")
    await b.until("!!document.querySelector('#fxs-go')")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome is required")
def test_mentioned_asks_for_its_word_and_sends_it_before_the_modifiers():
    got = {}

    async def check(b):
        await open_studio(b)
        await b.js("document.querySelector('.fxs-chip.fxs-eff[data-pick=mentioned]').click()")
        got["label"] = await b.js("document.getElementById('fxs-cap-label').textContent")
        got["blocked"] = await b.js("document.getElementById('fxs-go').disabled")
        await b.js("document.querySelector('.fxs-chip.fxs-mot[data-pick=zoom]').click()")
        await b.js("const c=document.getElementById('fxs-cap');c.value='pizza';c.dispatchEvent(new Event('input'))")
        got["cmd"] = await b.js("document.getElementById('fxs-cmd').textContent")
        got["ready"] = not await b.js("document.getElementById('fxs-go').disabled")
        await b.js("document.getElementById('fxs-go').click()")
        await b.until("!document.querySelector('#fxs-go')")
        got["input"] = await b.js("document.getElementById('ai-input').value")
        # Reopening resumes: the word is back in its box, zoom is still a modifier.
        await b.until("[...document.querySelectorAll('.fx-act')].some(x=>/Effects/.test(x.textContent))")
        await b.js("[...document.querySelectorAll('.fx-act')].find(x=>/Effects/.test(x.textContent)).click()")
        await b.until("!!document.querySelector('#fxs-go')")
        got["reopen_cap"] = await b.js("document.getElementById('fxs-cap').value")
        got["reopen_cmd"] = await b.js("document.getElementById('fxs-cmd').textContent")

    asyncio.run(run(check))
    assert got["label"] == "What got mentioned?", got
    assert got["blocked"], ("Apply must wait for the word — a bare `mentioned` only gets asked for it", got)
    assert got["cmd"] == "mentioned pizza zoom" and got["ready"], got
    assert got["input"] == "mentioned pizza zoom", got
    assert got["reopen_cap"] == "pizza" and got["reopen_cmd"] == "mentioned pizza zoom", got
