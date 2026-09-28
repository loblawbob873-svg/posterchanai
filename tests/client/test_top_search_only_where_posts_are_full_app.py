"""The phone's top search shows only where posts are the point.

Reported one screen at a time: "Notes and Email make no sense to have search at the top since they
have their own search", then News, the Meme Builder, Communities, Remote Desktop, Virtual Machines,
the Monero Wallet, the Signer, Bookmarks. It is an ALLOW-list now (app.js TOP_SEARCH_VIEWS), so this
checks both directions -- and the path that bypasses switchView: a profile, a thread and search
results set VIEW directly, and must not inherit the previous screen's answer.
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

NO_SEARCH = ["notes", "mail", "news", "meme", "concord", "vms", "wallet", "signer", "bookmarks", "notifications"]
SEARCH = ["home"]
VISIBLE = "(()=>{const s=document.querySelector('.topbar .searchbox');return !!s&&s.offsetParent!==null})()"
# The PosterChan avatar at the end of every phone title row ("do that for mobile"), search or not.
AVATAR = ("(()=>{const t=document.querySelector('.topbar');if(!t||t.offsetParent===null)return 'no topbar';"
          "const a=getComputedStyle(t,'::after');const r=t.getBoundingClientRect();"
          "return a.content!=='none'&&a.backgroundImage.includes('url(')&&parseFloat(a.width)===36})()")


async def run():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="pc-topsearch-") as profile:
        proc = subprocess.Popen(["/opt/google/chrome/chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
                                 "--remote-debugging-port=0", "--user-data-dir=" + profile, "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(profile, "DevToolsActivePort").exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, "DevToolsActivePort").read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get("http://127.0.0.1:" + port + "/json")).json()
            page = next(p for p in pages if p.get("type") == "page")
            async with websockets.connect(page["webSocketDebuggerUrl"], max_size=20_000_000) as ws:
                b = Browser(ws)
                await b.call("Page.enable")
                await b.call("Emulation.setDeviceMetricsOverride",
                             {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True})
                await b.call("Page.addScriptToEvaluateOnNewDocument", {"source": "window.__hasChats=false;" + INIT})
                await b.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_port}/client"})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                await b.until("document.body.classList.contains('guest')")
                await b.js("""(()=>{const k=new Uint8Array(32).fill(1);window.__events=[];
                  document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(k);
                  document.querySelector('#btn-nsec-login').click()})()""")
                await b.until("!!__PC.me()")
                out = {}
                for v in NO_SEARCH + SEARCH:
                    await b.js(f"__PC.switchView({json.dumps(v)})")
                    await asyncio.sleep(0.4)
                    out[v] = await b.js(VISIBLE)
                    out["avatar:" + v] = await b.js(AVATAR)
                # Notes -> a profile, which sets VIEW without switchView.
                await b.js("__PC.switchView('notes')")
                await asyncio.sleep(0.4)
                await b.js("__PC.openProfile(__PC.me().pubkey)")
                await b.until("document.querySelectorAll('.prof-tab').length>0")
                out["notes->profile"] = await b.js(VISIBLE)
                return out
        finally:
            proc.terminate()
            server.shutdown()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_top_search_is_only_on_screens_about_posts():
    out = asyncio.run(run())
    wrong = [v for v in NO_SEARCH if out[v]] + [v for v in SEARCH if not out[v]]
    assert not wrong, f"top search visibility wrong on {wrong}: {out}"
    assert out["notes->profile"], "a profile opened from Notes kept Notes' hidden search"
    no_avatar = [v for v in NO_SEARCH + SEARCH if out["avatar:" + v] is not True and out["avatar:" + v] != "no topbar"]
    assert not no_avatar, f"no PosterChan avatar top-right on the phone title row of {no_avatar}: {out}"
