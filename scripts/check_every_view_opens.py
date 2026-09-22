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
            for _ in range(150):
                if Path(profile, "DevToolsActivePort").exists():
                    break
                await asyncio.sleep(.1)
            else:
                print("SKIP chrome never published its DevTools port")
                raise SystemExit(2)
            port = Path(profile, "DevToolsActivePort").read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get(f"http://127.0.0.1:{port}/json")).json()
            url = next(p for p in pages if p.get("type") == "page")["webSocketDebuggerUrl"]
            async with websockets.connect(url, max_size=40_000_000) as ws:
                b = full.Browser(ws)
                await b.call("Page.enable")
                await b.call("Network.setBlockedURLs", {"urls": ["https://*", "wss://*"]})
                inject = ERRORS_JS + full.INIT + FIXTURE_JS
                if simulate == "broken-view":
                    # Break one screen's entry point the way a missed dependency would.
                    inject += ("\naddEventListener('DOMContentLoaded',()=>{const sv=__PC.switchView;"
                               "__PC.switchView=v=>{ if(v==='bookmarks') setTimeout(()=>{ undefinedHelperFromTheOldScope(); });"
                               " return sv(v); };});")
                await b.call("Page.addScriptToEvaluateOnNewDocument", {"source": inject})
                for (w, h_, mobile) in WIDTHS:
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
                    views = await b.js("[...new Set([...document.querySelectorAll('[data-view]')]"
                                       ".map(e=>e.dataset.view).filter(Boolean))]")
                    for v in EXTRA_VIEWS:
                        if v not in views:
                            views.append(v)
                    for v in views:
                        if v in NO_FEED:
                            continue
                        await b.js(f"__PC.switchView({json.dumps(v)})")
                        await asyncio.sleep(1.2)
                        state = await b.js("({errs:__viewErrors.splice(0)})")
                        opened += 1
                        for e in state["errs"]:
                            problems.append(f"[{w}px] error opening {v}: {e}")
                    # A thread and the own profile via their real entry points.
                    for label, expr in (("thread", "__PC.openThread(__events[0].id)"),
                                        ("own profile", "__PC.openProfile(__PC.me().pubkey)")):
                        await b.js(f"(()=>{{try{{ {expr}; }}catch(e){{ __viewErrors.push('error: '+e.message); }}}})()")
                        await asyncio.sleep(1.2)
                        opened += 1
                        for e in await b.js("__viewErrors.splice(0)"):
                            problems.append(f"[{w}px] error opening {label}: {e}")
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
