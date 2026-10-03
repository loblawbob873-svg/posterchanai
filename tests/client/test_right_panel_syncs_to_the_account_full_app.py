"""The right panel's shown/hidden choice follows the ACCOUNT, not the device ("make it sync to the
account"). Two browser profiles signed in with the same key: hiding the panel on the first publishes it in
the account's `pcai:client-prefs` document, and the second -- a fresh profile that has
never touched the setting -- comes up with the panel hidden and the Settings switch off, then turning it on
there publishes that.
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

from tests.client import test_effects_full_app as full
from tests.client import test_social_offline_refresh_full_app as soc

# The fixture relay, plus: remember every published event, and answer a 30078 query from a seeded store.
PREFS = r'''
window.__published=[];
(()=>{const Orig=window.WebSocket;
 const send=Orig.prototype.send;
 Orig.prototype.send=function(raw){let m;try{m=JSON.parse(raw);}catch(_){return send.call(this,raw);}
   if(Array.isArray(m)&&m[0]==='EVENT'&&m[1]&&m[1].kind===30078){window.__published.push(m[1]);}
   if(Array.isArray(m)&&m[0]==='REQ'&&m.slice(2).some(f=>(f.kinds||[]).includes(30078))){
     const want=m.slice(2).filter(f=>(f.kinds||[]).includes(30078));
     const seed=(window.__seedPrefs||[]).filter(ev=>want.some(f=>(!f['#d']||f['#d'].includes((ev.tags.find(t=>t[0]==='d')||[])[1]))));
     setTimeout(()=>{for(const ev of seed)this.fire('message',['EVENT',m[1],ev]);this.fire('message',['EOSE',m[1]]);},10);
     return;}
   return send.call(this,raw);};})();
'''


async def device(seed, act):
    full.ROOT = Path(__file__).resolve().parents[2]
    server = ThreadingHTTPServer(("127.0.0.1", 0), full.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="pc-rpsync-", ignore_cleanup_errors=True) as prof:
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
            async with websockets.connect(next(x for x in pages if x["type"] == "page")["webSocketDebuggerUrl"],
                                          max_size=20_000_000) as ws:
                b = full.Browser(ws)
                await b.call("Page.enable")
                await b.call("Network.setBlockedURLs", {"urls": ["https://*", "wss://*"]})
                await b.call("Emulation.setDeviceMetricsOverride", dict(width=1440, height=900, deviceScaleFactor=1,
                                                                        mobile=False, screenWidth=1440, screenHeight=900))
                await b.call("Page.addScriptToEvaluateOnNewDocument",
                             {"source": soc.INIT + PREFS + "window.__seedPrefs=" + json.dumps(seed) + ";"})
                await b.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_port}/client"})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                await b.until("document.body.classList.contains('guest')")
                await b.js("(()=>{const key=new Uint8Array(32).fill(1);document.querySelector('#nsec-input').value="
                           "NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()")
                await b.until('!!__PC.me() && Relay.status==="ok"')
                await b.js("__PC.switchView('global')")
                await b.until("window.__feedCards() >= 4")
                await asyncio.sleep(2.5)            # the account's prefs are restored after sign-in
                return await act(b)
        finally:
            p.terminate()
            p.wait(timeout=10)
            server.shutdown()


async def _switch(b):
    await b.js("__PC.switchView('settings')")
    await b.until("!!document.querySelector('.us-tab[data-tab=\"timeline\"]')")
    await b.js("document.querySelector('.us-tab[data-tab=\"timeline\"]').click()")
    await b.until("!!document.getElementById('set-right-panel')")


def _prefs_of(published):
    docs = [e for e in published if ["d", "pcai:client-prefs"] in [t[:2] for t in e.get("tags", [])]]
    return json.loads(docs[-1]["content"]) if docs else None


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_hiding_the_panel_on_one_device_hides_it_on_another():
    async def hide_on_a(b):
        assert await b.js("document.querySelector('.rightbar').getClientRects().length>0"), "panel not shown to begin with"
        await b.js("document.getElementById('rb-toggle').click()")
        await b.until("window.__published.some(e=>(e.tags||[]).some(t=>t[0]==='d'&&t[1]==='pcai:client-prefs'))", )
        return await b.js("window.__published")
    published_a = asyncio.run(device([], hide_on_a))
    prefs = _prefs_of(published_a)
    assert prefs is not None and prefs.get("rightPanel") is False, ("Hide did not save to the account", prefs)
    seed = [e for e in published_a if ["d", "pcai:client-prefs"] in [t[:2] for t in e.get("tags", [])]][-1:]

    async def on_b(b):
        out = {"panel": await b.js("document.querySelector('.rightbar').getClientRects().length>0")}
        await _switch(b)
        out["switch"] = await b.js("document.getElementById('set-right-panel').checked")
        await b.js("(()=>{const x=document.getElementById('set-right-panel');x.checked=true;x.dispatchEvent(new Event('change'));})()")
        await b.until("window.__published.some(e=>(e.tags||[]).some(t=>t[0]==='d'&&t[1]==='pcai:client-prefs'))")
        out["published"] = await b.js("window.__published")
        return out
    res = asyncio.run(device(seed, on_b))
    assert res["panel"] is False and res["switch"] is False, (
        "a second device ignored the account's hidden panel", res["panel"], res["switch"])
    assert (_prefs_of(res["published"]) or {}).get("rightPanel") is True, "turning it on was not saved to the account"
