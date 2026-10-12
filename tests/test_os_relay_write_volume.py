"""MEASURED: bytes written to disk for one scripted session -- the client's IndexedDB vs the local relay.

The reason PosterChanOS's local relay exists (osmode.py) is SSD writes: on the two machines in use on
2026-10-12, Chromium's LevelDB wrote 327 MB (desktop) and 1,202 MB (laptop) of table files in 19 minutes,
10-31x what it flushed, because every compaction rewrites what the last one wrote. This replays ONE
scripted session -- a few thousand signed posts and profiles, arriving in bursts the way a timeline delivers
them -- through each store and counts what the kernel was asked to write, per process, from its own I/O
accounting (`ru_oublock` at exit, i.e. write_bytes, so a final flush is included):

  IndexedDB: the SHIPPED store.js in Chrome (Store.saveEvent / saveProfile), its profile on a real disk;
  relay:     the SHIPPED `relay_main.py --os-mode` receiving the same events as EVENT frames.

Each side runs once EMPTY first (no events), and that baseline -- startup files, Chrome's own profile -- is
subtracted. The numbers are printed (pytest -s) and the test fails if the relay writes more than IndexedDB.
Skipped where the only scratch space is tmpfs (no disk accounting to read).
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests import os_relay_harness as H

ROOT = Path(__file__).resolve().parents[1]
CHROME = "/opt/google/chrome/chrome"
N_POSTS, N_AUTHORS, BURST = 3000, 150, 100


def _scratch():
    base = os.path.expanduser("~/.cache")
    os.makedirs(base, exist_ok=True)
    d = tempfile.mkdtemp(prefix="pc-osrelay-io-", dir=base)
    fs = subprocess.run(["stat", "-f", "-c", "%T", d], capture_output=True, text=True).stdout.strip()
    if fs in ("tmpfs", "ramfs", "overlayfs"):
        shutil.rmtree(d, ignore_errors=True)
        pytest.skip("no disk-backed scratch directory (%s): write accounting would read zero" % fs)
    return d


def _session():
    now = int(time.time())
    keys = [bytes([10 + (i % 200)] * 31 + [i % 251]) for i in range(N_AUTHORS)]
    evs = []
    for i, k in enumerate(keys):
        evs.append(H.ev(k, 0, json.dumps({"name": "author %d" % i, "about": "x" * 120}), created_at=now - 7200))
    for i in range(N_POSTS):
        k = keys[i % N_AUTHORS]
        evs.append(H.ev(k, 1, "post %d %s" % (i, "lorem ipsum dolor sit amet " * (4 + i % 12)),
                        [["t", "tag%d" % (i % 7)]], created_at=now - 3600 + i))
    return evs


def _wait4(proc):
    _, status, ru = os.wait4(proc.pid, 0)
    proc.returncode = os.waitstatus_to_exitcode(status)
    return ru.ru_oublock * 512


# --------------------------------------------------------------------------------------------- IndexedDB side
PAGE = """<!doctype html><script src="store.js"></script><script>
window.__ready=(async()=>{await Store.init();return true})();
</script>"""


async def _cdp(port):
    import httpx
    import websockets
    for _ in range(200):
        try:
            async with httpx.AsyncClient(trust_env=False) as h:
                pages = (await h.get("http://127.0.0.1:%d/json" % port, timeout=2)).json()
            t = next(p for p in pages if p.get("type") == "page")
            return await websockets.connect(t["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024)
        except Exception:      # noqa: BLE001
            await asyncio.sleep(0.1)
    raise AssertionError("Chrome DevTools never came up")


def _idb_bytes(events, scratch, tag):
    site = Path(scratch, "site-" + tag)
    site.mkdir()
    (site / "index.html").write_text(PAGE)
    shutil.copy(ROOT / "static/js/client/store.js", site / "store.js")

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(site), **k)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    profile = Path(scratch, "chrome-" + tag)
    proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
                             "--remote-debugging-port=%d" % port, "--user-data-dir=" + str(profile),
                             "http://127.0.0.1:%d/index.html" % srv.server_port],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def drive():
        ws = await _cdp(port)
        seq = [0]

        async def js(expr):
            seq[0] += 1
            await ws.send(json.dumps({"id": seq[0], "method": "Runtime.evaluate",
                                      "params": {"expression": expr, "awaitPromise": True, "returnByValue": True}}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == seq[0]:
                    return m.get("result", {}).get("result", {}).get("value")
        for _ in range(100):
            if await js("!!window.__ready && window.__ready.then(()=>true)"):
                break
            await asyncio.sleep(0.1)
        for i in range(0, len(events), BURST):
            batch = json.dumps(events[i:i + BURST])
            await js("(()=>{for(const e of %s){ if(e.kind===0) Store.saveProfile(e); Store.saveEvent(e); } return 1})()" % batch)
            await asyncio.sleep(0.25)
        await asyncio.sleep(3)                                  # the last 700 ms write batch, compactions
        seq[0] += 1
        await ws.send(json.dumps({"id": seq[0], "method": "Browser.close"}))
        await ws.close()
    asyncio.run(drive())
    try:
        written = _wait4(proc)
    finally:
        srv.shutdown()
        srv.server_close()
    return written


# --------------------------------------------------------------------------------------------- relay side
def _relay_bytes(events, scratch, tag):
    tmp = Path(scratch, "relay-" + tag)
    tmp.mkdir()
    relay = H.LocalRelay(str(tmp))
    relay.start()

    async def feed():
        async with H.Client(relay.url) as c:
            for i in range(0, len(events), BURST):
                for e in events[i:i + BURST]:
                    await c.ws.send(json.dumps(["EVENT", e]))
                if events[i:i + BURST]:
                    await c.publish(events[min(i + BURST, len(events)) - 1])
                await asyncio.sleep(0.25)
            await asyncio.sleep(3)
    if events:
        asyncio.run(feed())
    rss = relay.rss_kb()
    relay.proc.terminate()
    written = _wait4(relay.proc)
    relay._logf.close()
    return written, rss


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome required")
def test_the_local_relay_writes_less_than_indexeddb_for_the_same_session():
    scratch = _scratch()
    try:
        events = _session()
        idb_base = _idb_bytes([], scratch, "empty")
        idb = _idb_bytes(events, scratch, "session")
        relay_base, _ = _relay_bytes([], scratch, "empty")
        relay, rss = _relay_bytes(events, scratch, "session")
        idb_net, relay_net = max(0, idb - idb_base), max(0, relay - relay_base)
        logical = sum(len(json.dumps(e)) for e in events)
        print("\n[write volume] session: %d events, %.1f MB of JSON" % (len(events), logical / 1e6))
        print("[write volume] IndexedDB (Chrome): %.2f MB written (%.2f MB baseline subtracted) = %.1fx the data"
              % (idb_net / 1e6, idb_base / 1e6, idb_net / max(1, logical)))
        print("[write volume] local relay:        %.2f MB written (%.2f MB baseline subtracted) = %.1fx the data,"
              " RSS %d MB" % (relay_net / 1e6, relay_base / 1e6, relay_net / max(1, logical), rss // 1024))
        assert idb_net > 0, "Chrome wrote nothing -- the measurement is not measuring (tmpfs profile?)"
        assert relay_net < idb_net, ("the local relay wrote MORE than IndexedDB for the same session",
                                     relay_net, idb_net)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
