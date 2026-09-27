#!/usr/bin/env python3
"""A private repository shows on its owner's Git page, and opens, in the REAL client on a REAL node.

    PC_CHECK_GIT_NSEC=<nsec of a key that owns a private repo> \\
    venv-unified/bin/python scripts/check_private_repo_git_page_live.py [base_url] [repo_id]

Reported 2026-09-27 as "I don't see configs in my git page". Two halves had to work, and both only
work against a live node, because each crosses a boundary no unit test can: the relay has to answer
`auth-required` for the owner's own repos and relay.js has to sign in and ask again, and every
browse call has to carry the viewer's signed read token through /client/git/* to the git host's
read gate. The steps a person takes, asserted on what they would see:

  listed    the repo's card is on the Git page, and it says Private;
  opens     clicking it opens the repo view;
  files     the Files tab lists entries -- the read gate accepted the viewer's token.

It needs a key that OWNS a private repo, so it cannot run on a key minted for the occasion:
`PC_CHECK_GIT_NSEC` absent is exit 2 ("could not run"), never a pass.

Exit 0 = all three, 1 = a step failed (printed), 2 = could not run.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://poster.place").rstrip("/")
REPO = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("PC_CHECK_GIT_REPO", "configs")
PORT = int(os.environ.get("PC_CHECK_PORT") or 9531)
PROFILE_DIR = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-check-private-repo"


def _sk_hex():
    raw = (os.environ.get("PC_CHECK_GIT_NSEC") or "").strip()
    if not raw:
        return None
    from app.services.nostr import nostr_service
    return nostr_service.decode_seckey(raw).hex() if raw.startswith("nsec") else raw


async def _run(ws_url, sk):
    import websockets
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as w:
        n = [0]

        async def call(method, params=None):
            n[0] += 1
            await w.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
            while True:
                r = json.loads(await w.recv())
                if r.get("id") == n[0]:
                    if "error" in r:
                        raise RuntimeError(r["error"])
                    return r.get("result", {})

        async def js(expr):
            r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
            return r.get("result", {}).get("value")

        async def until(expr, secs):
            for _ in range(int(secs / 0.5)):
                v = await js(expr)
                if v:
                    return v
                await asyncio.sleep(0.5)
            return None

        await call("Page.enable")
        await call("Runtime.enable")
        await call("Emulation.setDeviceMetricsOverride",
                   {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})
        await call("Page.addScriptToEvaluateOnNewDocument",
                   {"source": "try{localStorage.setItem('pc_nostr_session',JSON.stringify({sk:%s}));}catch(e){}"
                              % json.dumps(sk)})
        await call("Page.navigate", {"url": BASE + "/client"})
        if not await until("!!(window.__PC && window.__PC.switchView)", 60):
            return ["the client never booted"]
        await asyncio.sleep(3)
        await js("window.__PC.switchView('repos')")
        card = """(()=>{const c=[...document.querySelectorAll('.repo-card')].find(c=>
            (c.querySelector('.repo-card-name')||{}).textContent===%s); return c||null;})()""" % json.dumps(REPO)
        if not await until("!!" + card, 40):
            names = await js("[...document.querySelectorAll('.repo-card-name')].map(n=>n.textContent).slice(0,40)")
            screen = await js("(window.__PC&&__PC.S&&__PC.S.VIEW)+' | '+((document.getElementById('feed')||{}).innerText||'').slice(0,300)")
            return [f"listed: no card named {REPO!r} on the Git page (saw {names}; screen: {screen!r})"]
        problems = []
        if not await js(f"!!({card}).querySelector('.repo-private')"):
            problems.append("listed: the card does not say Private")
        await js(f"({card}).click()")
        if not await until("!!document.querySelector('.repo-view .rv-tab[data-tab=\"files\"]')", 30):
            return problems + ["opens: clicking the card did not open the repo view"]
        await js("document.querySelector('.rv-tab[data-tab=\"files\"]').click()")
        rows = await until("document.querySelectorAll('.repo-view .fb-row').length||0", 40)
        if not rows:
            problems.append("files: the Files tab listed nothing -- the read token was refused or never sent")
        else:
            print(f"  files: {rows} entries listed")
        return problems


async def main():
    sk = _sk_hex()
    if not sk:
        print("SKIP  PC_CHECK_GIT_NSEC is not set -- this needs a key that owns a private repo")
        return 2
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    chrome = shutil.which("google-chrome-stable") or shutil.which("chromium")
    if not chrome:
        print("SKIP  no Chrome on this box")
        return 2
    shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                             f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE_DIR}", "about:blank"],
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
        problems = await _run(page["webSocketDebuggerUrl"], sk)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    if problems:
        for p in problems:
            print("FAIL  " + p)
        return 1
    print(f"OK  {REPO} is listed as Private on its owner's Git page, opens, and its files are readable")
    return 0


sys.exit(asyncio.run(main()))
