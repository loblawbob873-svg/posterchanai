"""One envelope the local relay will not take must not stop Concord's move onto it.

2026-10-13, the laptop: hours after the desktop update its Concord cache had moved 219 envelopes and kept ~4,500
bodies in IndexedDB. `migrate()` broke out of its loop the moment a batch came back partly acknowledged, so a single
envelope the relay refused (expired, too big, a kind it will not keep) stopped the whole move for that page load --
the next load moved at most another batch and stopped again on the same envelope.

Real Chrome, the SHIPPED store.js + concord-cache.js, the SHIPPED local relay: 450 envelopes are cached while the
relay is DOWN (so they sit in IndexedDB as bodies), the relay comes up, its acknowledgement of the FIRST envelope is
withheld, and ONE migrate() must move the other 449, leaving only the refused one as a body.
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
from pathlib import Path

import pytest

from tests import os_relay_harness as H
from tests.client.test_concord_cache_on_the_local_relay import CHROME, ROOT

PAGE = r"""<!doctype html><pre id=out></pre>
<script>window.pcShell={localRelay:()=>RELAYURL};</script>
<script src=/store.js></script>
<script>
const EV=EVENTS, S=JSON.stringify(['room','general']);
const AUTO=AUTOFLAG;
if(sessionStorage.getItem('phase')==='C'&&!AUTO){             // the relay "refuses" one -- installed before anything can move
  const refused=EV[0].id, put=Store.lrPut.bind(Store);
  Store.lrPut=async(evs,ms)=>{const ok=await put(evs,ms);ok.delete(refused);return ok;};}
</script><script src=/concord-cache.js></script>
<script>
function idbRows(){return new Promise(res=>{const q=indexedDB.open('posterchan-concord-v1');q.onsuccess=()=>{const db=q.result;
  const r=db.transaction('envelopes','readonly').objectStore('envelopes').getAll();r.onsuccess=()=>{db.close();res(r.result);};};});}
(async()=>{try{
  await Store.init();
  if((sessionStorage.getItem('phase')||'B')==='B'){
    await PCConcordCache.put(S,EV);
    const rows=await idbRows();
    sessionStorage.setItem('B',JSON.stringify({live:Store.lrLive(),bodies:rows.filter(r=>r.event).length}));
    out.textContent='B';return;
  }
  for(let i=0;i<200&&!Store.lrLive();i++)await new Promise(r=>setTimeout(r,25));
  const refused=EV[0].id;
  let moved=null,rows;
  if(AUTO){                       // nobody calls migrate(): the boot-time move must happen by itself
    for(let i=0;i<120;i++){rows=await idbRows();if(!rows.some(r=>r.event))break;await new Promise(r=>setTimeout(r,250));}
  }else{moved=await PCConcordCache.migrate();rows=await idbRows();}
  out.textContent=JSON.stringify({B:JSON.parse(sessionStorage.getItem('B')),live:Store.lrLive(),moved,
    bodies:rows.filter(r=>r.event).length,refs:rows.filter(r=>r.lr).length,
    refusedKept:rows.some(r=>r.id===refused&&r.event)});
}catch(e){out.textContent=JSON.stringify({threw:String(e.stack||e)});}})();
</script>"""


async def _drive(url, port, relay):
    import urllib.request
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
        assert await out() == "B", await out()
        relay.start()
        await call("Runtime.evaluate", {"expression": "sessionStorage.setItem('phase','C');location.reload()"})
        await asyncio.sleep(1)
        for _ in range(400):
            await asyncio.sleep(.25)
            v = await out()
            if v and v != "B":
                return json.loads(v)
    raise AssertionError("page did not finish")


def _run(auto):
    pytest.importorskip("websockets")
    tmp = H.tmpdir()
    relay = H.LocalRelay(tmp)          # NOT started: the envelopes are cached while it is down
    server = proc = None
    try:
        now = int(time.time())
        sk = bytes([7] * 32)
        evs = [H.ev(sk, 1059, "cipher-%04d" % i, [["p", "ab" * 32]], now - 2000 + i) for i in range(450)]
        page = (PAGE.replace("RELAYURL", json.dumps(relay.url)).replace("AUTOFLAG", "true" if auto else "false")
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

    return got


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome required")
def test_one_refused_envelope_does_not_stop_the_move_onto_the_local_relay():
    got = _run(auto=False)
    assert "threw" not in got, got
    assert not got["B"]["live"] and got["B"]["bodies"] == 450, ("the envelopes were not cached as bodies first", got)
    assert got["live"], ("the Store never reached the local relay", got)
    assert got["refusedKept"], ("the refused envelope lost its body", got)
    assert got["moved"] == 449 and got["bodies"] == 1 and got["refs"] == 449, (
        "one refused envelope stopped the move: the rest stayed in IndexedDB", got)


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome required")
def test_the_move_starts_at_boot_without_concord_being_opened():
    """The TV and the laptop kept their old Concord cache because the move only ran from Concord's own calls."""
    got = _run(auto=True)
    assert "threw" not in got, got
    assert got["live"], ("the Store never reached the local relay", got)
    assert got["bodies"] == 0 and got["refs"] == 450, ("nothing moved until somebody opened Concord", got)
