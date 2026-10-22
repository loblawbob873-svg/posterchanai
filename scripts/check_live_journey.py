#!/usr/bin/env python3
"""Does the site WORK for a person who signs in? A real browser, a fresh profile, the real login button.

check_live_production.py asks production questions over HTTP. Every regression of 2026-10-22 passed those and the
whole suite, and still broke for people: Communities loaded nothing on a browser with empty storage, Telegram sat on
"CONNECTING…", the wallet probe filled the console with errors. All of it was visible the moment somebody signed in.
So this signs in -- headless Chrome, an EMPTY profile (a cold device, which is what Tor Browser is every session), the
NIP-07 button a person presses -- and walks the screens a person opens first:

  login      the app session exists and the client says who is signed in
  feed       the home timeline draws notes
  concord    Communities opens; every saved room draws its messages or says why not (never a blank pane)
  telegram   Telegram leaves "CONNECTING…" (signed in, setup, sign-in, or an error card -- never the boot screen)
  console    no console.error and no uncaught exception anywhere along the way

    venv-unified/bin/python scripts/check_live_journey.py https://poster.place

The test identity is a hex secret key in ~/.config/posterchanai/live-journey.key (0600, machine-local, never in the
repo). Without it the check SKIPs. Exit 0 = every step passed, 1 = a step FAILED, 2 = could not run. Each line names
the step, its time, and on failure what the page showed.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEY_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "posterchanai" / "live-journey.key"
CHROME = os.environ.get("PC_CHROME", "/opt/google/chrome/chrome")
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

# Console lines that are not the page being broken: the browser's own network chatter for a socket a page closed on
# purpose, and third-party relays refusing a stranger. Kept short and literal; anything else is a failure.
IGNORED_CONSOLE = (
    "was interrupted while the page was loading",
    "ResizeObserver loop",
)

SHIM = r"""
(()=>{const NT=window.__NT;if(!NT||!window.__LJ_SK)return;
 const sk=Uint8Array.from(window.__LJ_SK.match(/../g),h=>parseInt(h,16)),pk=NT.getPublicKey(sk);
 const ck=p=>NT.nip44.v2.utils.getConversationKey(sk,p);
 window.nostr={getPublicKey:async()=>pk,signEvent:async e=>NT.finalizeEvent({...e},sk),getRelays:async()=>({}),
  nip04:{encrypt:async(p,t)=>NT.nip04.encrypt(sk,p,t),decrypt:async(p,c)=>NT.nip04.decrypt(sk,p,c)},
  nip44:{encrypt:async(p,t)=>NT.nip44.v2.encrypt(t,ck(p)),decrypt:async(p,c)=>NT.nip44.v2.decrypt(c,ck(p))}};
})();
"""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Page:
    """The one tab, over the DevTools protocol: evaluate, and collect what the console said."""

    def __init__(self, ws):
        self.ws, self.n, self.errors, self.pending = ws, 0, [], {}

    async def pump(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if "id" in m and m["id"] in self.pending:
                self.pending.pop(m["id"]).set_result(m)
                continue
            meth, p = m.get("method"), m.get("params") or {}
            if meth == "Runtime.consoleAPICalled" and p.get("type") == "error":
                text = " ".join(str(a.get("value", a.get("description", ""))) for a in p.get("args") or [])
                self._note("console.error", text)
            elif meth == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails") or {}
                self._note("uncaught", (d.get("exception") or {}).get("description") or d.get("text") or "")
            elif meth == "Log.entryAdded" and (p.get("entry") or {}).get("level") == "error":
                e = p["entry"]
                self._note("browser:" + str(e.get("source")), str(e.get("text", ""))[:300] + " " + str(e.get("url", ""))[:160])

    def _note(self, kind, text):
        if not any(s in text for s in IGNORED_CONSOLE):
            self.errors.append((round(time.monotonic(), 1), kind, text[:400]))

    async def call(self, method, params=None, timeout=30):
        self.n += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.n] = fut
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        return await asyncio.wait_for(fut, timeout)

    async def js(self, expr, timeout=30):
        r = await self.call("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True}, timeout)
        res = (r.get("result") or {})
        if res.get("exceptionDetails"):
            raise RuntimeError(str(res["exceptionDetails"].get("text"))[:200])
        return (res.get("result") or {}).get("value")

    async def until(self, expr, seconds):
        """Seconds it took for `expr` to be truthy, or None when it never was."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            try:
                if await self.js(expr, timeout=10):
                    return round(time.monotonic() - t0, 1)
            except Exception:       # noqa: BLE001 -- a page mid-navigation answers nothing; ask again
                pass
            await asyncio.sleep(0.5)
        return None


async def journey(base: str, sk_hex: str) -> list:
    import httpx
    import websockets
    results = []
    prof = tempfile.mkdtemp(prefix="pc-live-journey-")
    port = _free_port()
    proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--window-size=1366,900",
                             "--no-first-run", f"--remote-debugging-port={port}", f"--user-data-dir={prof}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        target = None
        for _ in range(100):
            try:
                target = next(t for t in httpx.get(f"http://127.0.0.1:{port}/json").json() if t.get("type") == "page")
                break
            except Exception:       # noqa: BLE001
                await asyncio.sleep(0.1)
        if not target:
            return [("login", SKIP, 0, "Chrome did not start")]
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=None) as ws:
            page = Page(ws)
            pump = asyncio.create_task(page.pump())
            for m in ("Runtime.enable", "Log.enable", "Page.enable"):
                await page.call(m)
            bundle = (ROOT / "static/vendor/nostr/nostr.bundle.js").read_text()
            await page.call("Page.addScriptToEvaluateOnNewDocument", {"source": "(()=>{" + bundle + "\n;window.__NT=NostrTools;})();window.__LJ_SK="
                                                                                + json.dumps(sk_hex) + ";" + SHIM})
            await page.call("Page.navigate", {"url": base + "/"})

            # -- login: the button a person presses, then the app session -----------------------------------
            t = await page.until("!!document.querySelector('#btn-nip07') || !!(window.__PC && __PC.me && __PC.me() && __PC.me().pubkey)", 45)
            if t is None:
                results.append(("login", FAIL, 45, "no sign-in button and nobody signed in after 45 s"))
                return results
            await page.js("(()=>{const b=document.querySelector('#btn-nip07'); if(b) b.click(); return 1})()")
            t = await page.until("!!(window.__PC && __PC.me && __PC.me() && __PC.me().pubkey)", 45)
            results.append(("login", PASS if t is not None else FAIL, t or 45, "" if t is not None else "the NIP-07 sign-in never completed"))
            if t is None:
                return results

            # -- feed -----------------------------------------------------------------------------------------
            t = await page.until("document.querySelectorAll('#feed .note').length>0", 45)
            results.append(("feed", PASS if t is not None else FAIL, t or 45,
                            "" if t is not None else "no note drawn in 45 s: " + str(await page.js("(document.querySelector('#feed')||{}).innerText||''"))[:160]))

            # -- concord: every saved room says something -------------------------------------------------------
            await page.js("__PC.switchMessagesTab('concord'); 1")
            t = await page.until("document.body.classList.contains('concord-view') && !!document.querySelector('.cc-app')", 30)
            if t is None:
                results.append(("concord", FAIL, 30, "Communities did not open"))
            else:
                rooms = await page.js("document.querySelectorAll('[data-cc-server]').length") or 0
                if not rooms:
                    results.append(("concord", PASS, t, "opened (this account has no rooms to load)"))
                else:
                    await page.js("(document.querySelector('[data-cc-server]')||{}).click&&document.querySelector('[data-cc-server]').click(); 1")
                    t2 = await page.until("(()=>{const m=document.querySelector('.cc-messages');if(!m)return false;"
                                          "return m.querySelectorAll('.cc-message').length>0||/start of this|could not|not readable|no channels/i.test(m.innerText)})()", 45)
                    shown = await page.js("((document.querySelector('.cc-messages')||{}).innerText||'').slice(0,160)")
                    results.append(("concord", PASS if t2 is not None else FAIL, t2 or 45,
                                    f"{rooms} room(s); first room: " + ("messages drawn" if t2 is not None else "blank after 45 s: " + str(shown))))

            # -- telegram: never the boot screen ---------------------------------------------------------------
            await page.js("__PC.switchView('tg'); 1")
            t = await page.until("!!document.querySelector('.tg-app') && !document.querySelector('.tg-boot')", 40)
            what = await page.js("((document.querySelector('.tg-app')||{}).innerText||'').replace(/\\s+/g,' ').slice(0,120)")
            results.append(("telegram", PASS if t is not None else FAIL, t or 40,
                            ("shows: " + str(what)) if t is not None else "still CONNECTING… after 40 s"))

            await asyncio.sleep(2)
            pump.cancel()
            errs = page.errors
            results.append(("console", PASS if not errs else FAIL, 0,
                            "clean" if not errs else f"{len(errs)} error(s): " + " | ".join(f"{k}: {x}" for _, k, x in errs[:6])))
            return results
    finally:
        try:
            os.killpg(proc.pid, 15)
        except OSError:
            pass
        shutil.rmtree(prof, ignore_errors=True)


def main(argv) -> int:
    base = (argv[1] if len(argv) > 1 else "").rstrip("/")
    if not base:
        print("SKIP: needs an instance URL")
        return 2
    if not KEY_FILE.exists():
        print(f"SKIP: no test identity ({KEY_FILE} holds a 64-hex secret key)")
        return 2
    sk = KEY_FILE.read_text().strip().lower()
    if len(sk) != 64 or any(c not in "0123456789abcdef" for c in sk):
        print(f"SKIP: {KEY_FILE} is not a 64-hex secret key")
        return 2
    if not Path(CHROME).exists():
        print(f"SKIP: no Chrome at {CHROME}")
        return 2
    try:
        results = asyncio.run(asyncio.wait_for(journey(base, sk), 420))
    except Exception as e:      # noqa: BLE001 -- could not run is never a pass
        print(f"SKIP: the journey could not run: {type(e).__name__}: {e}")
        return 2
    for name, verdict, secs, why in results:
        print(f"{verdict:4}  {name:9} {secs:5.1f}s  {why}")
    if any(v == FAIL for _, v, _, _ in results):
        print(f"FAIL: a person signing in to {base} would see something broken")
        return 1
    if any(v == SKIP for _, v, _, _ in results):
        return 2
    print(f"OK: signing in to {base} works")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
