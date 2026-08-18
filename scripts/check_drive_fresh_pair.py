#!/usr/bin/env python3
"""TWO cold devices, ONE account, at the same time — can each read what the other uploads?

The class of failure no single-process test can see, and the one that burned a real fresh sync
pair for four days (2026-08-18): both devices pulled an empty drive index, both minted a master
key, and last-writer-wins on the pointer re-keyed the account under whichever saved last — every
byte the other had sealed became "sealed with a different key". The server now keeps the FIRST key
for ever; this drives two real cold browser sessions on one throwaway account, uploads from both
concurrently, and asserts each side decrypts the other's file. Two processes, one server: the
actual topology of the actual failure.

Exit 0 pass, 1 fail, 2 could-not-run.
"""
import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3051"
PORT_A = int(os.environ.get("PC_CHECK_PORT") or 9541)
PORT_B = PORT_A + 1
PROF_A = (os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-freshpair") + "-a"
PROF_B = (os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-freshpair") + "-b"

LOGIN = r"""(async (nsec) => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const $ = s => document.querySelector(s);
  try{ localStorage.clear(); sessionStorage.clear(); }catch(_){}
  // the REAL nsec login, through the real UI, so `signer` and ME exist like on any device
  document.body.classList.remove('guest');
  const g=$('#auth-gate'); if(g) g.classList.remove('hidden');
  const l=$('#auth-login'); if(l) l.classList.remove('hidden');
  const nb=$('#btn-nsec'); if(nb) nb.click(); await sleep(80);
  const inp=$('#nsec-input'); if(!inp) return false;
  inp.value = nsec;
  const go=$('#btn-nsec-login'); if(!go) return false;
  go.click();
  for(let i=0;i<40;i++){ await sleep(250); if(window.__PC && __PC.me && __PC.me()) return true; }
  return false;
})"""

COLD = r"""(async () => {
  // The cold path the mint race lived on: pull finds nothing, a key is minted, the pointer saved.
  try{ return { ok: await window.__PC.driveColdStart() }; }
  catch(e){ return { ok:false, err: String((e && e.message) || e) }; }
})"""

SEAL = r"""(async (text) => {
  try{ return { ok:true, ct: await window.__PC.driveSeal(text) }; }
  catch(e){ return { ok:false, err: String((e && e.message) || e) }; }
})"""

OPEN = r"""(async (ct, expect) => {
  try{ const pt = await window.__PC.driveOpen(ct); return { ok: pt === expect, got: pt.slice(0,40) }; }
  catch(e){ return { ok:false, err: String((e && e.message) || e) }; }
})"""


class Tab:
    def __init__(self, port, profile):
        self.port, self.profile = port, profile
        self.proc = None
        self.ws = None
        self.n = 0

    async def start(self, chrome, url):
        import websockets
        subprocess.run(["rm", "-rf", self.profile], check=False)
        self.proc = subprocess.Popen(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             f"--remote-debugging-port={self.port}", f"--user-data-dir={self.profile}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        page = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list"))
                page = [t for t in tabs if t["type"] == "page"][0]
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not page:
            return False
        self.ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024)
        await self.call("Runtime.enable")
        await self.call("Page.enable")
        await self.call("Page.navigate", {"url": url})
        for _ in range(80):
            await asyncio.sleep(0.25)
            if await self.js("!!(window.Relay && Relay.worker && window.__PC)"):
                return True
        return False

    async def call(self, method, params=None):
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        while True:
            r = json.loads(await self.ws.recv())
            if r.get("id") == self.n:
                return r.get("result")

    async def js(self, expr, aw=False):
        r = await self.call("Runtime.evaluate",
                            {"expression": expr, "returnByValue": True, "awaitPromise": aw})
        if r.get("exceptionDetails"):
            if os.environ.get("PC_DEBUG"):
                print("  DEBUG:", json.dumps(r["exceptionDetails"])[:600])
            return None
        return r["result"].get("value")

    def stop(self):
        try:
            self.proc and self.proc.terminate()
        except Exception:
            pass
        subprocess.run(["rm", "-rf", self.profile], check=False)


async def drive(url):
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    a, b = Tab(PORT_A, PROF_A), Tab(PORT_B, PROF_B)
    problems = []
    try:
        ok = await asyncio.gather(a.start(chrome, url), b.start(chrome, url))
        if not all(ok):
            print("SKIP  a client never finished loading")
            return 2
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from app.services.nostr import bech32 as _b32
        nsec = _b32.encode("nsec", bytes.fromhex(secrets.token_hex(32)))
        for t in (a, b):
            if not await t.js(f"({LOGIN})({json.dumps(nsec)})", aw=True):
                print("SKIP  login failed")
                return 2
        # REGISTER the account (device A only — one account). An unregistered key's drive saves are
        # refused (by design, part of this same fix), so the race under test needs a real user row.
        reg = await a.js("""(async () => {
          try{
            const auth = await window.__PC.signAuth('login');
            const r = await fetch('/api/auth/nostr-login', { method:'POST',
              headers:{'Content-Type':'application/json'},
              body: JSON.stringify({ pubkey: window.__PC.me().pubkey,
                                     auth: btoa(JSON.stringify(auth)) }) });
            return r.ok;
          }catch(e){ return false; }
        })()""", aw=True)
        if not reg:
            print("SKIP  could not register the throwaway account")
            return 2
        # BOTH devices hit the cold path CONCURRENTLY — the race window itself
        ca, cb = await asyncio.gather(a.js(f"({COLD})()", aw=True), b.js(f"({COLD})()", aw=True))
        print("  cold-start:", json.dumps(ca), json.dumps(cb))
        for who, c in (("A", ca), ("B", cb)):
            if not (c and c.get("ok")):
                problems.append(f"device {who} could not cold-start the drive: {(c or {}).get('err')}")
        if not problems:
            # each pulls once more (the adopt path), then seals — the other must open it
            ka, kb = await asyncio.gather(a.js("window.__PC.drivePull()", aw=True),
                                          b.js("window.__PC.drivePull()", aw=True))
            srv = await a.js("""(async () => {
              const auth = await window.__PC.signAuth('files-index');
              const r = await fetch('/client/files-index', { method:'POST',
                headers:{'Content-Type':'application/json'},
                body: JSON.stringify({ pubkey: window.__PC.me().pubkey,
                                       auth: btoa(JSON.stringify(auth)) }) });
              const j = await r.json(); return (j.index && j.index.mk || '').slice(0, 24);
            })()""", aw=True)
            print("  keys: A=%s B=%s server=%s" % (str(ka)[:24], str(kb)[:24], srv))
            sa, sb = await asyncio.gather(
                a.js(f"({SEAL})('from-A')", aw=True), b.js(f"({SEAL})('from-B')", aw=True))
            if not (sa and sa.get("ok") and sb and sb.get("ok")):
                problems.append(f"seal failed: {json.dumps(sa)} {json.dumps(sb)}")
            else:
                ra, rb = await asyncio.gather(
                    a.js(f"({OPEN})({json.dumps(sb['ct'])}, 'from-B')", aw=True),
                    b.js(f"({OPEN})({json.dumps(sa['ct'])}, 'from-A')", aw=True))
                print("  cross-open:", json.dumps(ra), json.dumps(rb))
                if not (ra and ra.get("ok")):
                    problems.append("device A cannot open B's seal — the pair holds two drive keys: "
                                    + str((ra or {}).get("err")))
                if not (rb and rb.get("ok")):
                    problems.append("device B cannot open A's seal — the pair holds two drive keys: "
                                    + str((rb or {}).get("err")))
    finally:
        a.stop()
        b.stop()
    if problems:
        for p in problems:
            print("FAIL ", p)
        return 1
    print("PASS  two cold devices on one fresh account read each other's files")
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
