#!/usr/bin/env python3
"""Can the client actually READ an encrypted-drive blob through the service worker?

Run: venv-unified/bin/python scripts/check_drive_blob_fetch.py [sha]

This exists because three fixes in a row were reasoned about rather than measured, and the reports
kept coming back: "couldn't load image", "attachments still broken", "operation failed". Every test
until now stubbed the worker's globals and checked ROUTING — which branch a request takes — and none
of them ever moved a byte. That is exactly the gap the bugs lived in: the routing was right each
time, and the response was unusable.

So this drives a REAL browser against the REAL server: register the shipped sw.js, wait for it to
control the page, fetch a real blob through it, and compare what arrives against what the server
says it holds. Twice, so the second read is a cache HIT — the path where a stored copy with wrong
headers or a truncated body would surface.

Exit 0 = the bytes came through, 1 = they did not, 2 = could not run.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 9487
PROFILE = "/tmp/pc-drive-blob-check"
ORIGIN = os.environ.get("PC_ORIGIN", "http://127.0.0.1:3051")


def _blob():
    """A real keep blob and its stored size, straight from the relay's own database."""
    if len(sys.argv) > 1:
        return sys.argv[1], None
    sys.path.insert(0, ROOT)
    from app.database import SessionLocal          # noqa: E402
    from sqlalchemy import text                    # noqa: E402
    db = SessionLocal()
    row = db.execute(text("select sha256, size from blossom_blobs "
                          "where keep=true and size between 50000 and 900000 "
                          "order by created_at desc limit 1")).fetchone()
    return (row[0], row[1]) if row else (None, None)


PROBE = r"""(async () => {
  const out = { steps: [] };
  const sha = %s;
  try {
    // The shipped worker, at the scope and URL the web client really uses (/client/sw.js).
    const reg = await navigator.serviceWorker.register('/client/sw.js', { scope: '/client/' });
    out.steps.push('registered');
    await navigator.serviceWorker.ready;
    out.steps.push('ready');
    // `ready` resolves on activation, but the PAGE is only controlled after a claim; without a
    // controller the fetch never reaches the worker and this would silently test nothing.
    for (let i = 0; i < 50 && !navigator.serviceWorker.controller; i++)
      await new Promise(r => setTimeout(r, 100));
    out.controlled = !!navigator.serviceWorker.controller;

    for (const pass of ['miss', 'hit']) {
      const t0 = performance.now();
      let r;
      try { r = await fetch('/blossom/' + sha); }
      catch (e) { out['err_' + pass] = String(e && e.message || e); break; }
      const buf = await r.arrayBuffer().catch(e => { out['bodyErr_' + pass] = String(e && e.message || e); return null; });
      out[pass] = { status: r.status, ok: r.ok, bytes: buf ? buf.byteLength : -1,
                    len: r.headers.get('content-length'), ct: r.headers.get('content-type'),
                    ms: Math.round(performance.now() - t0) };
      await new Promise(r2 => setTimeout(r2, 900));     // let the background copy land
    }
  } catch (e) { out.fatal = String(e && e.message || e); }
  return out;
})()"""


async def main():
    import websockets
    sha, size = _blob()
    if not sha:
        print("SKIP  no keep blob to test with")
        return 2
    subprocess.run(["rm", "-rf", PROFILE], check=False)
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
         "--unsafely-treat-insecure-origin-as-secure=" + ORIGIN,
         "--allow-insecure-localhost", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        page = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
                page = [t for t in tabs if t["type"] == "page"][0]
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not page:
            print("SKIP  could not start Chrome")
            return 2
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        return msg.get("result")

            await call("Runtime.enable")
            await call("Page.enable")
            await call("Page.navigate", {"url": ORIGIN + "/client"})
            await asyncio.sleep(3.0)
            r = await call("Runtime.evaluate",
                           {"expression": PROBE % json.dumps(sha), "returnByValue": True,
                            "awaitPromise": True})
            if r.get("exceptionDetails"):
                print("probe threw:", json.dumps(r["exceptionDetails"])[:400])
                return 1
            got = r["result"]["value"]
    finally:
        proc.terminate()
        subprocess.run(["rm", "-rf", PROFILE], check=False)

    print(json.dumps(got, indent=1))
    problems = []
    if not got.get("controlled"):
        print("SKIP  the service worker never took control (nothing was exercised)")
        return 2
    for pass_ in ("miss", "hit"):
        d = got.get(pass_)
        if not d:
            problems.append(f"{pass_}: the fetch did not complete "
                            f"({got.get('err_' + pass_) or got.get('bodyErr_' + pass_) or 'no result'})")
            continue
        if not d["ok"]:
            problems.append(f"{pass_}: HTTP {d['status']}")
        if size and d["bytes"] != size:
            problems.append(f"{pass_}: got {d['bytes']} bytes, the server holds {size}")
        if d["bytes"] <= 0:
            problems.append(f"{pass_}: empty body")

    if problems:
        print("\nREGRESSIONS")
        for p in problems:
            print("  drive-blob-unreadable:", p)
        return 1
    print(f"OK  the blob reads back intact through the service worker "
          f"(miss {got['miss']['ms']}ms, hit {got['hit']['ms']}ms)")
    return 0


if __name__ == "__main__":
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        sys.exit(2)
    sys.exit(asyncio.run(main()))
