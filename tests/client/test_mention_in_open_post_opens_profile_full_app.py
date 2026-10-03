"""Classic mode: a name in an OPEN post opens that person's profile -- on a tablet, by touch.

"i just got a mute notification but I can't click any usernames in the post on tablet". A name link is
`<a href="#" class="mention">`. linkcards.js has a document-wide capture handler that opens links to this
instance's own entities (`https://poster.place/npub1…`) in-app -- and with a post open the address bar IS
that post (`/nevent1…`), so `#` resolved to the post itself: the handler swallowed the tap and re-opened
the same post. Every name, hashtag and quote link inside an open post did nothing. The desktop never moves
the address bar, which is why it worked there.

Reuses the tablet harness of the desktop back-then-scroll check.
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

from tests.client import test_effects_full_app as full
from tests.client import test_social_offline_refresh_full_app as soc


async def run():
    full.ROOT = Path(__file__).resolve().parents[2]
    server = ThreadingHTTPServer(("127.0.0.1", 0), full.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="pc-deskback-", ignore_cleanup_errors=True) as prof:
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
                await b.call("Emulation.setDeviceMetricsOverride", dict(width=1340, height=800, deviceScaleFactor=2,
                                                                        mobile=True, screenWidth=1340, screenHeight=800))
                await b.call("Emulation.setTouchEmulationEnabled", dict(enabled=True, maxTouchPoints=5))
                await b.call("Page.addScriptToEvaluateOnNewDocument", {"source": soc.INIT})
                await b.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_port}/client"})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                await b.until("document.body.classList.contains('guest')")
                await b.js("(()=>{const key=new Uint8Array(32).fill(1);document.querySelector('#nsec-input').value="
                           "NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()")
                await b.until('!!__PC.me() && Relay.status==="ok"')
                await b.js("(()=>{const st=document.createElement('style');st.textContent='#toast-root{display:none!important}';document.head.appendChild(st);})()")
                await b.js("""(()=>{const me=__PC.me().pubkey;const other=NostrTools.getPublicKey(new Uint8Array(32).fill(9));
                  const npO=NostrTools.nip19.npubEncode(other), npM=NostrTools.nip19.npubEncode(me);
                  const ev=NostrTools.finalizeEvent({kind:1,created_at:Math.floor(Date.now()/1000)-30,
                    tags:[['p',other],['p',me]],content:'nostr:'+npO+' muted nostr:'+npM+' (on Nostr)'},new Uint8Array(32).fill(7));
                  Store.saveEvent(ev); window.__ev=ev;})()""")
                await b.js("__PC.openThread(__ev.id)")
                await b.until("[...document.querySelectorAll('#feed a.mention')].some(a=>a.getClientRects().length)")
                await asyncio.sleep(1)
                path = await b.js("location.pathname")
                r = await b.js("(()=>{const a=[...document.querySelectorAll('#feed a.mention')].find(x=>x.getClientRects().length);"
                               "const r=a.getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2];})()")
                await b.call("Input.dispatchTouchEvent", dict(type="touchStart", touchPoints=[dict(x=r[0], y=r[1])]))
                await asyncio.sleep(.05)
                await b.call("Input.dispatchTouchEvent", dict(type="touchEnd", touchPoints=[]))
                await asyncio.sleep(2)
                # The profile view replaces the post: its tab row is what a profile draws and a post does not.
                opened = await b.js("!!document.querySelector('#feed #prof-list, #feed .prof-tabs, #feed [data-ptab]')")
                still_post = await b.js("[...document.querySelectorAll('#feed .note .txt')].some(t=>t.innerText.includes('(on Nostr)'))")
                # The feature that handler exists for must still work: a full link to THIS instance's
                # npub inside a post opens the profile in-app.
                await b.js("""(()=>{const other=NostrTools.getPublicKey(new Uint8Array(32).fill(9));
                  const url=location.origin+'/'+NostrTools.nip19.npubEncode(other);
                  const ev=NostrTools.finalizeEvent({kind:1,created_at:Math.floor(Date.now()/1000)-20,
                    tags:[],content:'profile link '+url+' end'},new Uint8Array(32).fill(7));
                  Store.saveEvent(ev); window.__ev2=ev;})()""")
                await b.js("__PC.openThread(__ev2.id)")
                await b.until("[...document.querySelectorAll('#feed .note .txt a[href^=http]')].some(a=>a.getClientRects().length)")
                await asyncio.sleep(1)
                await b.js("[...document.querySelectorAll('#feed .note .txt a[href^=http]')].find(a=>a.getClientRects().length).click()")
                await asyncio.sleep(2)
                own_link = await b.js("!!document.querySelector('#feed #prof-list, #feed .prof-tabs, #feed [data-ptab]')")
                return path, opened, still_post, own_link
        finally:
            p.terminate()
            p.wait(timeout=10)
            server.shutdown()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_tapping_a_name_in_an_open_post_opens_the_profile():
    path, opened, still_post, own_link = asyncio.run(run())
    assert own_link, "a link to this instance's own npub no longer opens the profile in-app"
    assert opened and not still_post, (
        f"tapping a name in the open post (address {path}) did not open the profile", opened, still_post)
