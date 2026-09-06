#!/usr/bin/env python3
"""After a relay restart, POSTING recovers and the BULK lane must too — measured, not assumed.

The report (2026-08-17): "blossom waiting forever for signer wtf" / "I could post fine". A post is
one `sign_event` on the interactive lane. Everything Blossom-shaped — the drive master key, a
folder-sync manifest, a DM restore — is `nip44_decrypt` on the BULK lane, whose requests get a 45s
ceiling, no re-send ladder, and one retry. A relay restart destroys every in-flight bulk request
(kind 24133 is ephemeral); if the lane doesn't hand slots back and recover, the next Blossom click
queues behind the dead ones for minutes while posting works instantly — the exact reported split.

This drives the SHIPPED client in a real browser against a real relay that is really restarted
while 30 bulk decrypts are in flight, then times one sign_event and one fresh nip44_decrypt.

Exit 0 pass, 1 fail, 2 could-not-run.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_nip46_signer as sig                      # noqa: E402
import check_nip46_reconnect as rec                   # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3051"
PORT = int(os.environ.get("PC_CHECK_PORT") or 9497)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-nip46-bulk"

BULKJS = r"""(async (n) => {
  const me = window.__PC.me();
  const ct = await window.__PC.nip44enc(me.pubkey, 'bulk probe');
  window.__BULK = { done: 0, failed: 0, n };
  for (let i = 0; i < n; i++)
    window.__PC.nip44dec(me.pubkey, ct)
      .then(() => window.__BULK.done++, () => window.__BULK.failed++);
  return true;
})"""

PROBEJS = r"""(async (ms) => {
  const me = window.__PC.me();
  const t0 = Date.now();
  const out = { sign: null, dec: null };
  try{
    const ev = await Promise.race([
      window.__PC.sign(1, 'lane probe ' + t0, []),
      new Promise((_,rej)=>setTimeout(()=>rej(new Error('sign gave up')), ms)) ]);
    out.sign = { ok: !!(ev && ev.sig), took: Date.now() - t0 };
  }catch(e){ out.sign = { ok:false, took: Date.now()-t0, err: String(e && e.message || e) }; }
  const t1 = Date.now();
  try{
    const ct = await window.__PC.nip44enc(me.pubkey, 'fresh after restart');
    const pt = await Promise.race([
      window.__PC.nip44dec(me.pubkey, ct),
      new Promise((_,rej)=>setTimeout(()=>rej(new Error('decrypt gave up')), ms)) ]);
    out.dec = { ok: pt === 'fresh after restart', took: Date.now() - t1,
                pt: String(pt).slice(0,80) };
  }catch(e){ out.dec = { ok:false, took: Date.now()-t1, err: String(e && e.message || e) }; }
  out.stats = window.__PC.dmStats();
  out.bulk = window.__BULK;
  return out;
})"""


async def drive(url):
    subprocess.run(["rm", "-rf", PROFILE], check=False)
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    import websockets

    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    problems = []
    relay = rec.Relay()
    relay_url = await relay.start()
    bunker = rec.clone_bunker(sig.Bunker(relay_url), relay_url)
    bunker.start()
    try:
        await bunker.wait_ready()
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

            async def js(expr, awaited=False):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True, "awaitPromise": awaited})
                if r.get("exceptionDetails"):
                    if os.environ.get("PC_DEBUG"):
                        print("  DEBUG:", json.dumps(r["exceptionDetails"])[:800])
                    return None
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")

            async def load():
                await call("Page.navigate", {"url": url})
                for _ in range(80):
                    await asyncio.sleep(0.25)
                    if await js("!!document.querySelector('#btn-amber')"):
                        return True
                return False

            if not await load():
                print("SKIP  the client never finished loading")
                return 2
            await js("try{ localStorage.clear(); sessionStorage.clear(); }catch(_){}")
            if not await load():
                print("SKIP  the client never finished loading")
                return 2

            uri = ("bunker://" + bunker.pk + "?relay="
                   + urllib.parse.quote(relay_url, safe="") + "&secret=s3cret")
            r = await js(f"({sig.LOGIN})({json.dumps(uri)}, {json.dumps(bunker.user_pk)})", awaited=True) or {}
            if not r.get("ok"):
                print(f"SKIP  could not log in ({r.get('err') or 'no answer'})")
                return 2
            await js("window.__PC_MARK = 'alive';")

            # 30 bulk decrypts in flight — slow enough that the restart lands MID-FLIGHT —
            # then the relay restarts under them.
            bunker.delay = 0.4
            ok = await js(f"({BULKJS})(30)", awaited=True)
            if not ok:
                print("SKIP  could not start the bulk load")
                return 2
            await asyncio.sleep(0.7)          # some in flight, some queued
            bunker.stop()
            await relay.kill()
            await asyncio.sleep(3)
            relay_url2 = await relay.start()
            assert relay_url2 == relay_url, "the relay came back on a different port"
            bunker = rec.clone_bunker(bunker, relay_url)
            bunker.delay = 0.4
            bunker.start()
            await bunker.wait_ready()
            await asyncio.sleep(2.0)

            out = await js(f"({PROBEJS})(60000)", awaited=True) or {}
            if not await js("window.__PC_MARK === 'alive'"):
                problems.append(("restart", "the page reloaded", "workaround, not fix"))
            sign, dec = out.get("sign") or {}, out.get("dec") or {}
            print(f"  sign after restart: ok={sign.get('ok')} took={sign.get('took')}ms "
                  f"{sign.get('err') or ''}")
            print(f"  bulk decrypt after restart: ok={dec.get('ok')} took={dec.get('took')}ms "
                  f"{dec.get('err') or ''} pt={dec.get('pt')!r}")
            print(f"  stats: {json.dumps(out.get('stats'))}")
            print(f"  bulk load: {json.dumps(out.get('bulk'))}")
            if not sign.get("ok"):
                problems.append(("sign", sign.get("err") or "no answer",
                                 "posting itself is broken after a relay restart"))
            if not dec.get("ok"):
                problems.append(("bulk", dec.get("err") or "no answer",
                                 "the reported wedge: blossom-shaped decrypt never answers"))
            elif dec.get("took", 0) > 25000:
                problems.append(("bulk-slow", f"{dec.get('took')}ms",
                                 "a fresh decrypt waited out dead slots — the reported 'forever'"))
    finally:
        try:
            bunker.stop()
        except Exception:
            pass
        try:
            await relay.kill()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        shutil.rmtree(PROFILE, ignore_errors=True)

    if problems:
        for why, err, note in problems:
            print(f"FAIL  {why}: {err} ({note})")
        return 1
    print("PASS  the bulk lane recovers from a relay restart")
    return 0


def main():
    try:
        urllib.request.urlopen(BASE + "/client", timeout=5)
    except Exception as e:
        print(f"SKIP  no instance at {BASE} ({e})")
        return 2
    return asyncio.run(drive(BASE + "/client"))


if __name__ == "__main__":
    sys.exit(main())
