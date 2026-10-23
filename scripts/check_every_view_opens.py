#!/usr/bin/env python3
"""EVERY SCREEN OPENS WITHOUT A SINGLE JAVASCRIPT ERROR — the full client, not a lifted region.

    venv-unified/bin/python scripts/check_every_view_opens.py
    venv-unified/bin/python scripts/check_every_view_opens.py --simulate broken-view

Written for the app.js split. app.js is being carved into modules that load on first use, and the
failure mode of every such move is the same and is invisible to everything else we run: a name that
resolved in the old scope and does not in the new one is a `ReferenceError` AT CLICK TIME. `node
--check` passes (the syntax is fine), the page loads, the unit tests that lift one function pass, and
the screen throws the first time somebody opens it. The only instrument that sees that is opening the
screen — every screen — in the real client and counting errors.

So this boots the SHIPPED client.html with all its scripts in headless Chrome (only HTTP and the relay
socket are fixtures — the same fixture the *_full_app tests use), signs in with a throwaway key, and
then opens every destination the sidebar offers, plus the screens that are not sidebar rows (own
profile, a thread, settings, drafts, notifications). For each it waits for the view to settle and
records every uncaught exception and every unhandled promise rejection raised while it opened.
Both desktop width (the classic sidebar layout) and phone width are driven.

The fixture answers every API with `{}`; the one endpoint whose screen cannot survive that shape
(the API-key list, which is an array) is answered with `[]` here, so what is reported is the
client's own errors and not the fixture's.

  --simulate broken-view   inject a ReferenceError into one screen's entry point, proving the check
                           reports what it exists to report.

Exit 0 = clean, 1 = regressions, 2 = could not run.
"""
import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CHROME = shutil.which("google-chrome-stable") or "/opt/google/chrome/chrome"
PROFILE = os.environ.get("PC_CHECK_PROFILE") or ""
CDP_PORT = int(os.environ.get("PC_CHECK_PORT") or 0)
WIDTHS = [(1440, 1000, False), (390, 844, True)]
LANES = max(1, int(os.environ.get("PC_CHECK_LANES") or 2))     # pages per width, driven at the same time

# Screens that are reached some other way than a sidebar row, but are still whole views.
EXTRA_VIEWS = ["profile", "drafts", "notifications", "settings", "bookmarks", "messages", "mail",
               "ai", "trending", "files", "music", "streams", "articles", "repos", "torrents",
               "media-center", "translate", "calls", "signer"]
# Views that open something outside #feed (a modal, a sheet, an external app) — not screens.
NO_FEED = {"compose", "more", "files-menu", "discover", "games", "logout", "admin"}

ERRORS_JS = r'''
window.__viewErrors=[];
addEventListener('error',e=>{__viewErrors.push('error: '+(e.message||e.type)+' @'+(e.filename||'').split('/').pop()+':'+e.lineno)});
addEventListener('unhandledrejection',e=>{const r=e.reason;__viewErrors.push('rejection: '+String(r&&r.stack||r&&r.message||r).slice(0,300))});
// A module that failed to load is reported by app.js's loader as a warning plus a toast, not a throw.
{ const w=console.warn; console.warn=function(...a){ if(a[0]==='[module]') __viewErrors.push('module load failed: '+a.slice(1).map(String).join(' ')); return w.apply(this,a); }; }
'''
FIXTURE_JS = r'''
{ const __fx=window.fetch; window.fetch=function(u,o){
    if(String(u).includes('/api/auth/api-keys')) return Promise.resolve(new Response('[]',{status:200,headers:{'Content-Type':'application/json'}}));
    return __fx.apply(this,arguments); }; }
'''


def _import_fixture():
    try:
        from tests.client import test_effects_full_app as full
    except Exception as exc:  # pragma: no cover - environment
        print(f"SKIP could not import the full-app fixture: {exc}")
        raise SystemExit(2)
    return full


async def run(simulate=None):
    import httpx
    import websockets
    full = _import_fixture()
    full.ROOT = ROOT
    server = ThreadingHTTPServer(("127.0.0.1", 0), full.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    problems = []
    opened = 0
    with tempfile.TemporaryDirectory(prefix="pc-every-view-") as tmp:
        profile = PROFILE or tmp
        shutil.rmtree(profile, ignore_errors=True)
        os.makedirs(profile, exist_ok=True)
        proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
                                 "--window-size=1440,1000", f"--remote-debugging-port={CDP_PORT}",
                                 "--user-data-dir=" + profile, "about:blank"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            # Chrome 153 writes DevToolsActivePort only for --remote-debugging-port=0. Given a FIXED port
            # (PC_CHECK_PORT, which checkall always sets) it listens and writes nothing -- so this check
            # SKIPPED on every suite run and the screen-by-screen gate verified nothing. Ask the port.
            port = None
            for _ in range(150):
                if Path(profile, "DevToolsActivePort").exists():
                    port = Path(profile, "DevToolsActivePort").read_text().splitlines()[0]
                    break
                if CDP_PORT:
                    try:
                        async with httpx.AsyncClient(timeout=1) as h:
                            if (await h.get(f"http://127.0.0.1:{CDP_PORT}/json/version")).status_code == 200:
                                port = str(CDP_PORT)
                                break
                    except Exception:
                        pass
                await asyncio.sleep(.1)
            if not port:
                print("SKIP chrome never published its DevTools port")
                raise SystemExit(2)
            async with httpx.AsyncClient() as h:
                browser_ws = (await h.get(f"http://127.0.0.1:{port}/json/version")).json()["webSocketDebuggerUrl"]
            inject = ERRORS_JS + full.INIT + FIXTURE_JS
            if simulate == "broken-view":
                # Break one screen's entry point the way a missed dependency would.
                inject += ("\naddEventListener('DOMContentLoaded',()=>{const sv=__PC.switchView;"
                           "__PC.switchView=v=>{ if(v==='bookmarks') setTimeout(()=>{ undefinedHelperFromTheOldScope(); });"
                           " return sv(v); };});")
            # LANES, CONCURRENTLY: each width's screens are split into LANES contiguous runs, and every run is driven
            # in its own page in its OWN browser context (its own storage -- a fresh device that boots and signs in
            # by itself). The per-screen wait is unchanged; what used to be ~110 sequential 1.2 s waits now overlap.
            async with websockets.connect(browser_ws, max_size=40_000_000) as bws:
                browser, root_lock = full.Browser(bws), asyncio.Lock()

                async def root(method, params=None):         # one browser socket, shared by the lanes: one call at a time
                    async with root_lock:
                        return await browser.call(method, params)

                async def lane(w, h_, mobile, index):
                    nonlocal opened
                    ctx = (await root("Target.createBrowserContext"))["browserContextId"]
                    tid = (await root("Target.createTarget", {"url": "about:blank", "browserContextId": ctx}))["targetId"]
                    try:
                        async with websockets.connect(f"ws://127.0.0.1:{port}/devtools/page/{tid}",
                                                      max_size=40_000_000) as ws:
                            b = full.Browser(ws)
                            await b.call("Page.enable")
                            await b.call("Network.setBlockedURLs", {"urls": ["https://*", "wss://*"]})
                            await b.call("Page.addScriptToEvaluateOnNewDocument", {"source": inject})
                            await b.call("Emulation.setDeviceMetricsOverride",
                                         {"width": w, "height": h_, "deviceScaleFactor": 1, "mobile": mobile})
                            await b.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_port}/client"})
                            await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                            await b.until("document.body.classList.contains('guest') || !!__PC.me()")
                            await b.js("(()=>{const key=new Uint8Array(32).fill(7);"
                                       "window.__events=Array.from({length:6},(_,i)=>NostrTools.finalizeEvent({kind:1,"
                                       "created_at:Math.floor(Date.now()/1000)-i,content:'every view '+i,tags:[]},key));"
                                       "if(!__PC.me()){document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);"
                                       "document.querySelector('#btn-nsec-login').click()}})()")
                            await b.until("!!__PC.me() && document.querySelectorAll('.note').length>=1")
                            await asyncio.sleep(1.5)
                            boot_errors = await b.js("__viewErrors.splice(0)")
                            for e in boot_errors:
                                problems.append(f"[{w}px] error during boot: {e}")
                            if index == 0:      # the first lane of a width reads the list; the others take their share
                                views = await b.js("[...new Set([...document.querySelectorAll('[data-view]')]"
                                                   ".map(e=>e.dataset.view).filter(Boolean))]")
                                for v in EXTRA_VIEWS:
                                    if v not in views:
                                        views.append(v)
                                listed[w] = [v for v in views if v not in NO_FEED]
                                ready[w].set()
                            await ready[w].wait()
                            per = -(-len(listed[w]) // LANES)
                            mine = listed[w][index * per:(index + 1) * per]
                            seen_views.setdefault(w, []).extend(mine)
                            for v in mine:
                                await b.js(f"__PC.switchView({json.dumps(v)})")
                                await asyncio.sleep(1.2)
                                state = await b.js("({errs:__viewErrors.splice(0)})")
                                opened += 1
                                for e in state["errs"]:
                                    problems.append(f"[{w}px] error opening {v}: {e}")
                            if index == LANES - 1:
                                # A thread and the own profile via their real entry points.
                                for label, expr in (("thread", "__PC.openThread(__events[0].id)"),
                                                    ("own profile", "__PC.openProfile(__PC.me().pubkey)")):
                                    await b.js(f"(()=>{{try{{ {expr}; }}catch(e){{ __viewErrors.push('error: '+e.message); }}}})()")
                                    await asyncio.sleep(1.2)
                                    opened += 1
                                    for e in await b.js("__viewErrors.splice(0)"):
                                        problems.append(f"[{w}px] error opening {label}: {e}")
                    finally:
                        try:
                            await root("Target.closeTarget", {"targetId": tid})
                            await root("Target.disposeBrowserContext", {"browserContextId": ctx})
                        except Exception:
                            pass

                seen_views, listed = {}, {}
                ready = {w: asyncio.Event() for (w, _h, _m) in WIDTHS}
                lanes = [asyncio.ensure_future(lane(w, h_, mobile, i)) for (w, h_, mobile) in WIDTHS for i in range(LANES)]
                try:
                    await asyncio.gather(*lanes)
                finally:
                    for t in lanes:
                        t.cancel()
                # Together the lanes opened every listed screen, once.
                for w, got in seen_views.items():
                    if sorted(got) != sorted(listed.get(w, [])):
                        problems.append(f"[{w}px] the lanes did not open the listed screens exactly once")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            server.shutdown()
            server.server_close()
    return opened, problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", choices=["broken-view"])
    args = ap.parse_args()
    if not (CHROME and Path(CHROME).exists()):
        print("SKIP no Chrome")
        return 2
    try:
        opened, problems = asyncio.run(run(args.simulate))
    except SystemExit as e:
        return int(e.code or 2)
    print(f"opened {opened} screens")
    for p in problems:
        print("  FAIL", p)
    if problems:
        print(f"{len(problems)} problem(s)")
        return 1
    print("OK every screen opened with zero JavaScript errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
