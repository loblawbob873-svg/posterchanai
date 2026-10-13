"""PosterChanOS: Concord's envelope cache keeps its envelopes on the LOCAL RELAY and only an index in IndexedDB.

Measured 2026-10-13 on the desktop (.102): its IndexedDB rewrote ~46 MB an hour, Concord's envelope rows among
it -- every refresh handed concord-cache.js a room's whole history and every row was put() again, 64 KB bodies
and all. Real Chrome, the SHIPPED store.js + concord-cache.js, the SHIPPED local relay process
(`relay_main.py --os-mode`):

  A. relay UP: 300 envelopes cached -> IndexedDB holds 300 small reference rows and NO envelope bodies; reading
     the stream (get and page) returns all 300, byte-identical, from the relay; caching the same history again
     writes NOTHING to IndexedDB.
  B. relay DOWN (stopped, page reloaded): new envelopes are cached whole in IndexedDB exactly as before and read
     back; the stream whose bodies are on the absent relay reads as a miss (empty), never an error.
  C. relay BACK: the bodies already in IndexedDB MIGRATE onto it (what every desktop that cached Concord before
     this shipped holds) and become reference rows; every envelope still reads back.

Run: venv-unified/bin/python -m pytest tests/client/test_concord_cache_on_the_local_relay.py -q
"""
import asyncio
import http.server
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from tests import os_relay_harness as H

ROOT = Path(__file__).resolve().parents[2]
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or "/opt/google/chrome/chrome"

PAGE = r"""<!doctype html><pre id=out></pre>
<script>window.pcShell={localRelay:()=>RELAYURL};</script>
<script src=/store.js></script><script src=/concord-cache.js></script>
<script>
const EV=EVENTS, LATE=LATE_EVENTS, S1=JSON.stringify(['room','general']), S2=JSON.stringify(['room','late']);
function idbRows(){return new Promise(res=>{const q=indexedDB.open('posterchan-concord-v1');q.onsuccess=()=>{const db=q.result;
  const r=db.transaction('envelopes','readonly').objectStore('envelopes').getAll();r.onsuccess=()=>{db.close();res(r.result);};};});}
let puts=0;const P=IDBObjectStore.prototype.put;IDBObjectStore.prototype.put=function(v,k){if(this.name==='envelopes')puts++;return P.call(this,v,k);};
(async()=>{try{
  await Store.init();
  const phase=sessionStorage.getItem('phase')||'A';
  if(phase==='A'){
    for(let i=0;i<200&&!Store.lrLive();i++)await new Promise(r=>setTimeout(r,25));
    const live=Store.lrLive();
    await PCConcordCache.put(S1,EV);const firstPuts=puts;puts=0;
    await PCConcordCache.put(S1,EV);const reputs=puts;
    const rows=await idbRows(),got=await PCConcordCache.get(S1);
    const pages=[];let before='';for(;;){const p=await PCConcordCache.page(S1,{before,limit:120});pages.push(...p.events.map(e=>e.id));before=p.before;if(p.done)break;}
    const byId=new Map(EV.map(e=>[e.id,e]));
    sessionStorage.setItem('A',JSON.stringify({live,firstPuts,reputs,rows:rows.length,bodies:rows.filter(r=>r.event).length,refs:rows.filter(r=>r.lr).length,
      got:got.length,identical:got.every(e=>JSON.stringify(e)===JSON.stringify(byId.get(e.id))),paged:new Set(pages).size}));
    out.textContent='A';return;
  }
  if(phase==='C'){
    for(let i=0;i<200&&!Store.lrLive();i++)await new Promise(r=>setTimeout(r,25));
    const moved=await PCConcordCache.migrate();const rows=await idbRows(),late=await PCConcordCache.get(S2),s1=await PCConcordCache.get(S1);
    out.textContent=JSON.stringify({...JSON.parse(sessionStorage.getItem('AB')),C:{live:Store.lrLive(),moved,bodies:rows.filter(r=>r.event).length,refs:rows.filter(r=>r.lr).length,late:late.length,s1:s1.length}});return;
  }
  const live=Store.lrLive();
  await PCConcordCache.put(S2,LATE);
  const rows=await idbRows(),late=await PCConcordCache.get(S2);let s1=null,err='';try{s1=(await PCConcordCache.get(S1)).length;}catch(e){err=String(e);}
  sessionStorage.setItem('AB',JSON.stringify({A:JSON.parse(sessionStorage.getItem('A')),B:{live,late:late.length,lateBodies:rows.filter(r=>r.stream===S2&&r.event).length,s1,err}}));out.textContent='B';
}catch(e){out.textContent=JSON.stringify({threw:String(e.stack||e)});}})();
</script>"""


async def _drive(url, port, relay):
    import websockets
    tab = None
    for _ in range(80):
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list"))
            tab = next((t for t in tabs if t.get("type") == "page" and t.get("url", "").startswith(url)), None)
            if tab:
                break
        except Exception:
            pass
        await asyncio.sleep(.25)
    async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=None) as ws:
        seq = 0

        async def call(method, params=None):
            nonlocal seq
            seq += 1
            await ws.send(json.dumps({"id": seq, "method": method, "params": params or {}}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == seq:
                    return m.get("result", {})

        async def out():
            r = await call("Runtime.evaluate", {"expression": "document.getElementById('out')&&out.textContent",
                                                "returnByValue": True})
            return (r.get("result") or {}).get("value") or ""

        for _ in range(240):
            await asyncio.sleep(.25)
            if await out():
                break
        assert await out() == "A", await out()
        relay.stop()
        await call("Runtime.evaluate", {"expression": "sessionStorage.setItem('phase','B');location.reload()"})
        await asyncio.sleep(1)
        for _ in range(240):
            await asyncio.sleep(.25)
            if await out() == "B":
                break
        assert await out() == "B", await out()
        relay.start()
        await call("Runtime.evaluate", {"expression": "sessionStorage.setItem('phase','C');location.reload()"})
        await asyncio.sleep(1)
        for _ in range(240):
            await asyncio.sleep(.25)
            v = await out()
            if v and v not in ("A", "B"):
                return json.loads(v)
    raise AssertionError("page did not finish")


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome required")
def test_concord_envelopes_live_on_the_local_relay_and_only_an_index_in_indexeddb():
    pytest.importorskip("websockets")
    tmp = H.tmpdir()
    relay = H.LocalRelay(tmp)
    server = proc = None
    try:
        relay.start()
        now = int(time.time())
        sk = bytes([9] * 32)
        evs = [H.ev(sk, 1059, "cipher-%04d-" % i + "x" * 900, [["p", "ab" * 32]], now - 1000 + i) for i in range(300)]
        late = [H.ev(sk, 1059, "late-%02d" % i, [["p", "cd" * 32]], now - 10 + i) for i in range(20)]
        page = (PAGE.replace("RELAYURL", json.dumps(relay.url)).replace("LATE_EVENTS", json.dumps(late))
                .replace("EVENTS", json.dumps(evs)))
        files = {"/store.js": ROOT / "static/js/client/store.js",
                 "/concord-cache.js": ROOT / "static/js/client/concord-cache.js"}

        class Handler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                p = self.path.split("?")[0]
                body = files[p].read_bytes() if p in files else page.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript" if p in files else "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                                 f"--remote-debugging-port={port}", "--user-data-dir=" + os.path.join(tmp, "profile"),
                                 url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        got = asyncio.run(_drive(url, port, relay))
    finally:
        if proc:
            proc.kill()
        if server:
            server.shutdown()
        relay.stop()
        shutil.rmtree(tmp, ignore_errors=True)

    assert "threw" not in got, got
    a, b = got["A"], got["B"]
    assert a["live"], ("the Store never reached the local relay", got)
    assert a["got"] == 300 and a["identical"] and a["paged"] == 300, ("reading the stream back lost envelopes", got)
    assert a["bodies"] == 0 and a["refs"] == 300, ("with the local relay up IndexedDB still holds envelope bodies", got)
    assert a["reputs"] == 0, ("caching the same history again rewrote IndexedDB rows", got)
    assert not b["live"] and b["late"] == 20 and b["lateBodies"] == 20, ("with the relay down the cache must keep bodies", got)
    assert b["err"] == "" and b["s1"] == 0, ("a stream on the absent relay must read as a miss, not an error", got)
    c = got["C"]
    assert c["live"] and c["moved"] == 20 and c["bodies"] == 0 and c["refs"] == 320, (
        "the bodies cached while the relay was down did not move onto it when it came back", got)
    assert c["late"] == 20 and c["s1"] == 300, ("after the move every envelope still reads back", got)
