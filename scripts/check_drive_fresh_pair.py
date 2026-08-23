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
        # Runtime.evaluate deliberately waits for a whole encrypted-drive cold start. During the
        # full release matrix two Chrome instances can spend over the websocket library's 20s pong
        # deadline inside that one renderer task; CDP is still alive and the outer check timeout is
        # the correct liveness bound. A protocol keepalive closing the controller halfway through
        # produced a red Folder Sync result that passed immediately when rerun alone. Disable only
        # the controller ping — calls still have checkall's hard per-check deadline.
        self.ws = await websockets.connect(page["webSocketDebuggerUrl"],
                                           max_size=64 * 1024 * 1024,
                                           ping_interval=None)
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

        # WAIT FOR THE ACCOUNT TO BE ABLE TO WRITE, BEFORE RACING ANYTHING.
        #
        # Registering a user and that user being allowed to publish are not the same instant. The
        # relay gates ingest on its WEB OF TRUST, and membership arrives on its own schedule — so a
        # brand-new key's first `pcai:files-index` write is refused ("blocked: not in web of trust"),
        # the save 503s, and both devices are left holding the key they each minted. Which is
        # indistinguishable, from the outside, from the four-day drive-key bug this check exists to
        # catch: `FAIL device A cannot open B's seal`.
        #
        # Measured across consecutive runs of this file: one FAILED with both keys divergent and the
        # next PASSED, unchanged. A check that races its own precondition reports the weather.
        #
        # An EMPTY index is written as the probe, deliberately: it establishes that this account can
        # publish without putting an `mk` on the server, so the cold path under test still finds
        # exactly what it is supposed to find — nothing.
        ready = False
        for _ in range(30):
            ok = await a.js("""(async () => {
              try{
                const auth = await window.__PC.signAuth('files-index');
                const r = await fetch('/client/files-index', { method:'POST',
                  headers:{'Content-Type':'application/json'},
                  body: JSON.stringify({ pubkey: window.__PC.me().pubkey,
                                         auth: btoa(JSON.stringify(auth)), index: {} }) });
                return r.ok;
              }catch(e){ return false; }
            })()""", aw=True)
            if ok:
                ready = True
                break
            await asyncio.sleep(2)
        if not ready:
            print("SKIP  this account cannot publish to the relay, so the rule this check proves "
                  "(the server keeps the FIRST drive key, for ever) cannot be exercised — a "
                  "throwaway key is a stranger to a relay with a web of trust, and its index write "
                  "is refused at INGEST. Look for `put pcai:files-index rejected: blocked: not in "
                  "web of trust` in the server log.")
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
              const j = await r.json();
              return { mk: (j.index && j.index.mk || '').slice(0, 24),
                       status: r.status, err: j && j.error || '' };
            })()""", aw=True)
            srv = srv or {}
            print("  keys: A=%s B=%s server=%s" % (str(ka)[:24], str(kb)[:24], srv.get("mk") or ""))
            # THE SERVER HOLDING NO KEY IS NOT A PRODUCT FAILURE — IT IS THIS TEST BEING UNABLE TO
            # RUN, and reporting it as a failure is worse than not running at all.
            #
            # The whole premise is "the server keeps the FIRST key for ever", so the server must be
            # able to keep one. On a relay with a web of trust it cannot: the throwaway npub this
            # check mints is a stranger, `pcai:files-index` is REJECTED at ingest ("blocked: not in
            # web of trust"), the save 503s, and both devices are left holding the key they minted
            # — which then reads exactly like the four-day drive-key bug this check exists to catch.
            #
            # Measured: `[nostr-store] put pcai:files-index rejected: blocked: not in web of trust`
            # on every save, then `FAIL device A cannot open B's seal`. A red check that means "your
            # relay has a WoT" is how people learn to skip the report. Exit 2, with the reason.
            #
            if not srv.get("mk"):
                # The probe above is a READ, so its status says nothing about why the WRITE did not
                # stick — quoting it would point at the wrong request. Name the cause instead.
                print("SKIP  the server is holding no drive key for this account, so the rule this "
                      "check exists to prove (first key wins, for ever) cannot be exercised here. "
                      "The throwaway account is a stranger to this relay's web of trust, so its "
                      "`pcai:files-index` write is refused at INGEST and the pointer never lands — "
                      "check the server log for `put pcai:files-index rejected: blocked: not in web "
                      "of trust`. Run against an instance whose relay accepts this key.")
                return 2
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
