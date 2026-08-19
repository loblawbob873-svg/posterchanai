#!/usr/bin/env python3
"""User Settings, RENDERED — every tab opens and its controls are actually in it.

Settings is built as one big template string with a hand-written tab list beside it, so a pane and
its tab are two separate edits that nothing checks against each other: a tab with no pane opens onto
nothing, a pane with no tab is unreachable, and a control moved between panes can simply vanish.
Reported exactly that way — "you said you made the read aloud fix to settings appearance which
don't exist!" and then "it's not there!".

So this opens the real screen in a real browser and, for each tab, clicks it and looks. Named
controls are asserted to be in the pane they are supposed to be in, not merely somewhere in the
document.

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
PORT = int(os.environ.get("PC_CHECK_PORT") or 9559)
PROF = (os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-ustabs")

# control id -> the pane it must live in
WHERE = {
    "set-read-aloud":    "timeline",
    "set-auto-new-posts":"timeline",
    "set-new-posts-pill":"timeline",
    "set-start-timeline":"timeline",
    "set-hide-replies":  "timeline",
    "set-post-effects":  "timeline",
    "set-clean-links":   "privacy",
    "set-hide-dm-prev":  "privacy",
    "set-media-cache":   "cache",
    "set-music-offline": "cache",
    "set-hide-fedi":     "social",
    "set-no-images":     "profile",
    "set-blur-nsfw":     "muted",
}

# Panes whose TAB is conditional on the build: the Tor pane is always in the markup and only gets a
# tab on a native shell (desktop/Capacitor), so on the web it is legitimately unreachable. Every
# other pane must have a tab, which is the rule this check exists to hold.
CONDITIONAL_PANES = {"tor"}

LOGIN = r"""(async (nsec) => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const $ = s => document.querySelector(s);
  try{ sessionStorage.clear(); }catch(_){}
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

OPEN = r"""(async () => {
  /* PATIENT, AND WILLING TO PRESS RETRY. renderUserSettings fetches /api/auth/settings first and
     dead-ends on "Couldn't load your settings" — a pane with no tabs in it — whenever that request
     is slow or refused. Under the full suite, 33 browser checks share one node and one relay, so
     ten seconds is simply not long enough: this check SKIPPED in a real run while the screen it was
     waiting for had in fact drawn, in its error state. A check that reports "could not run" when the
     app is fine is noise, and noise is what gets ignored on the run that matters. */
  window.__PC.switchView('settings');
  for(let i=0;i<120;i++){
    await new Promise(r=>setTimeout(r,500));
    if(document.querySelector('.us-tabs')) return true;
    const retry = document.querySelector('#us-retry');
    if(retry && (i % 8) === 0) retry.click();
  }
  return false;
})"""

LOOK = r"""(async (where, COND) => {
  const tabs = [...document.querySelectorAll('.us-tab')].map(b => [b.dataset.tab, b.textContent.trim()]);
  const panes = [...document.querySelectorAll('.us-pane')].map(p => p.dataset.pane);
  const out = { tabs, panes, misplaced: [], missing: [], dead: [], unreachable: [] };
  for(const [id, pane] of Object.entries(where)){
    const el = document.getElementById(id);
    if(!el){ out.missing.push(id); continue; }
    const host = el.closest('.us-pane');
    const got = host ? host.dataset.pane : '(none)';
    if(got !== pane) out.misplaced.push(id + ': in ' + got + ', wanted ' + pane);
  }
  // A tab with no pane opens onto nothing; a pane with no tab cannot be reached.
  for(const [t] of tabs) if(!panes.includes(t)) out.dead.push(t);
  for(const p of panes) if(!tabs.some(x => x[0] === p) && !COND.includes(p)) out.unreachable.push(p);
  // And every tab must actually switch: click it, then check its pane became the active one.
  out.switching = [];
  for(const [t] of tabs){
    const b = document.querySelector('.us-tab[data-tab="' + t + '"]');
    if(!b) continue;
    b.click();
    await new Promise(r => setTimeout(r, 60));
    const act = document.querySelector('.us-pane.active');
    if(!act || act.dataset.pane !== t) out.switching.push(t);
  }
  return out;
})"""


class Tab:
    def __init__(self, port, profile):
        self.port, self.profile, self.proc, self.ws, self.n = port, profile, None, None, 0

    async def start(self, chrome, url):
        import websockets
        subprocess.run(["rm", "-rf", self.profile], check=False)
        self.proc = subprocess.Popen(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             f"--remote-debugging-port={self.port}", f"--user-data-dir={self.profile}",
             "--window-size=1280,900", "about:blank"],
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
        self.ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024)
        await self.call("Runtime.enable")
        await self.call("Page.enable")
        await self.call("Page.addScriptToEvaluateOnNewDocument",
                        {"source": "window.addEventListener('error',e=>{window.__lastErr=String(e.message)+' @'+e.filename+':'+e.lineno;});"
                                   "window.addEventListener('unhandledrejection',e=>{window.__lastErr='rejection: '+String(e.reason&&e.reason.message||e.reason);});"})
        await self.call("Page.navigate", {"url": url})
        for _ in range(80):
            await asyncio.sleep(0.25)
            if await self.js("!!(window.__PC)"):
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
                            {"expression": expr, "returnByValue": True, "awaitPromise": aw,
                             "timeout": 60000})
        if r.get("exceptionDetails"):
            if os.environ.get("PC_DEBUG"):
                print("  DEBUG:", json.dumps(r["exceptionDetails"])[:800])
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
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.services.nostr import bech32 as _b32
    nsec = _b32.encode("nsec", bytes.fromhex(secrets.token_hex(32)))
    t = Tab(PORT, PROF)
    problems = []
    try:
        if not await t.start(chrome, url):
            print("SKIP  the client never finished loading")
            return 2
        if not await t.js(f"({LOGIN})({json.dumps(nsec)})", aw=True):
            print("SKIP  login failed")
            return 2
        # THE SCREEN NEEDS AN ACCOUNT THE NODE KNOWS. renderUserSettings fetches
        # /api/auth/settings first and, for an unregistered key, dead-ends on "Couldn't load your
        # settings" — which has no tabs in it, so this check sat waiting for markup that was never
        # going to appear and reported a bare SKIP. Registering the throwaway is one request and it
        # is what makes the wait deterministic instead of a race with a 2.4s failure path.
        reg = await t.js("""(async () => {
          const auth = await window.__PC.signAuth('login');
          const r = await fetch('/api/auth/nostr-login', { method:'POST',
            headers:{'Content-Type':'application/json'},
            body: JSON.stringify({ pubkey: window.__PC.me().pubkey, auth: btoa(JSON.stringify(auth)) }) });
          return r.ok; })()""", aw=True)
        if not reg:
            print("SKIP  could not register the throwaway account")
            return 2
        if not await t.js(f"({OPEN})()", aw=True):
            err = await t.js("(window.__lastErr||'') + ' || feed=' + String((document.getElementById('feed')||{}).innerHTML||'').slice(0,300)")
            print("SKIP  the settings screen never drew" + (" — page error: " + err if err else ""))
            return 2
        r = await t.js(f"({LOOK})({json.dumps(WHERE)}, {json.dumps(sorted(CONDITIONAL_PANES))})", aw=True) or {}
        if not r:
            print("SKIP  could not read the settings screen")
            return 2
        print("  tabs:", ", ".join(x[1] for x in r["tabs"]))
        for key, why in (("missing", "control is not on the settings screen at all"),
                         ("misplaced", "control is in the wrong pane"),
                         ("dead", "tab opens onto no pane"),
                         ("unreachable", "pane has no tab, so nothing can open it"),
                         ("switching", "tab does not activate its own pane")):
            for x in r.get(key) or []:
                problems.append(f"{why}: {x}")
        if not problems:
            print(f"  {len(r['tabs'])} tabs, each opens its own pane")
            print("  " + ", ".join(f"{k} in {v}" for k, v in WHERE.items()))
    finally:
        t.stop()
    if problems:
        for p in problems:
            print("FAIL ", p)
        return 1
    print("PASS  user settings: every tab opens its pane, every named control is where it belongs")
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
