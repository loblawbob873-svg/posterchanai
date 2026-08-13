#!/usr/bin/env python3
"""Every sign-in method on /client must be REACHABLE — run this before shipping auth/desktop changes.

Each assertion corresponds to a way a login can be broken while looking perfectly fine:

  covered      The control is rendered, visible and correctly sized, and a click on its centre lands
               on something ELSE. This is what shipped: `.auth-gate` is a fixed layer at z-index 50
               and PosterChan OS's `#os-root` is 300, so opening the gate from inside the desktop
               painted a complete, correct login form UNDERNEATH the desktop icons. Every button on
               it, including "Browser extension (NIP-07)", belonged to whatever was drawn on top.
               Reported by a user as "desktop mode login doesn't detect I'm using a browser
               extension" — because the click never reached the button that would have looked.
               elementFromPoint is the only test that catches this; visibility checks all pass.

  unbound      The control exists and nothing is listening. A button that does nothing is
               indistinguishable from a backend that is down.

  pane-dead    A sub-pane (Amber / remote signer, Create a new identity) opens but its own controls
               are covered or unbound — the outer button works, so the break is one click deeper
               than anyone looks.

Run against a live instance, in BOTH modes (the desktop-mode pass is the one that catches `covered`):

    venv-unified/bin/python scripts/check_auth_gate.py [base_url]

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome / site unreachable).
"""
import os
import asyncio
import json
import shutil
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3051"
PORT = int(os.environ.get("PC_CHECK_PORT") or 9479)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-auth-gate-check"

# id -> (label, may_be_hidden). may_be_hidden covers the controls a deployment legitimately does not
# offer: NIP-55 is Android-only, Google/Pleroma only appear when the node has them configured.
CONTROLS = [
    ("btn-nip07",     "Browser extension (NIP-07)", False),
    ("btn-amber",     "Amber / remote signer",      False),
    ("btn-nsec-login", "Login with key",            False),
    ("btn-show-signup", "Create a new identity",    False),
    ("btn-nip55",     "Amber on this device",       True),
    ("btn-google",    "Continue with Google",       True),
    ("btn-pleroma",   "Continue with Pleroma",      True),
]

# Sub-panes: (button that opens it, a control inside it that must then be reachable)
PANES = [
    ("btn-amber", "btn-amber-connect", "Amber / remote signer pane"),
    ("btn-amber", "btn-amber-nc",      "Amber / remote signer pane"),
    ("btn-show-signup", "btn-gen-key", "Create a new identity pane"),
]

PROBE = r"""((ids) => {
  const out = {};
  for (const id of ids) {
    const el = document.getElementById(id);
    if (!el) { out[id] = {missing: true}; continue; }
    const vis = !el.checkVisibility || el.checkVisibility();
    const r = el.getBoundingClientRect();
    let hit = null, covered = null;
    if (vis && r.width > 0 && r.height > 0) {
      const t = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      hit = t ? (t.id || t.tagName + '.' + String(t.className).slice(0, 40)) : null;
      covered = !(t && el.contains(t));
    }
    out[id] = {
      visible: vis, w: Math.round(r.width), h: Math.round(r.height),
      covered, hit,
      // An inline handler OR a listener attached with addEventListener. getEventListeners is a
      // devtools-only API, so onclick is what we can see from here — every auth control uses it.
      bound: typeof el.onclick === 'function',
    };
  }
  return out;
})(%s)"""


async def drive(ws_url, desktop_mode):
    import websockets
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        n = [0]

        async def call(m, p=None):
            n[0] += 1
            await ws.send(json.dumps({"id": n[0], "method": m, "params": p or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == n[0]:
                    return msg.get("result")

        async def js(expr):
            r = await call("Runtime.evaluate",
                           {"expression": expr, "returnByValue": True, "awaitPromise": True})
            if r.get("exceptionDetails"):
                return None
            return r["result"].get("value")

        await call("Runtime.enable")
        await call("Page.enable")
        await call("Emulation.setDeviceMetricsOverride",
                   {"width": 1600, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await call("Page.navigate", {"url": BASE + "/client"})
        await asyncio.sleep(13)
        await js("(()=>{try{ClientSettings.set('osMode',%s)}catch(e){}})()"
                 % ("true" if desktop_mode else "false"))
        await call("Page.navigate", {"url": BASE + "/client"})
        await asyncio.sleep(14)

        # Open the gate the way a guest does. In desktop mode this is the guest card INSIDE a window,
        # which is the exact path that was broken.
        opened = await js("(()=>{const b=document.getElementById('guest-login2');"
                          " if(b){b.click();return 'guest-card';}"
                          " if(window.__PC&&__PC.showAuth){__PC.showAuth('login');return 'api';}"
                          " return 'none';})()")
        await asyncio.sleep(2)
        if not await js("(()=>{const g=document.querySelector('.auth-gate');"
                        " return !!g && !g.classList.contains('hidden');})()"):
            return None, f"could not open the auth gate (entry point: {opened})"

        problems = []
        ids = [c[0] for c in CONTROLS]
        res = await js(PROBE % json.dumps(ids))
        if res is None:
            return None, "page did not evaluate (site unreachable?)"
        for cid, label, may_hide in CONTROLS:
            r = res.get(cid) or {}
            if r.get("missing"):
                if not may_hide:
                    problems.append(("missing", f"{label} (#{cid}) is not in the document"))
                continue
            if not r.get("visible"):
                continue                      # legitimately not offered by this deployment
            if r.get("covered"):
                problems.append(("covered",
                                 f"{label} (#{cid}) is covered by {r.get('hit')} — a click never reaches it"))
            if not r.get("bound"):
                problems.append(("unbound", f"{label} (#{cid}) has no click handler"))

        # Sub-panes, one click deeper.
        for opener, inner, pane in PANES:
            back = await js("(()=>{const b=document.getElementById('%s');"
                            " if(!b) return false; b.click(); return true;})()" % opener)
            if not back:
                continue
            await asyncio.sleep(0.6)
            pr = (await js(PROBE % json.dumps([inner]))) or {}
            r = pr.get(inner) or {}
            if r.get("missing") or not r.get("visible"):
                problems.append(("pane-dead", f"{pane}: #{inner} did not appear"))
            else:
                if r.get("covered"):
                    problems.append(("pane-dead",
                                     f"{pane}: #{inner} is covered by {r.get('hit')}"))
                if not r.get("bound"):
                    problems.append(("pane-dead", f"{pane}: #{inner} has no click handler"))
            # back out to the login pane for the next probe
            for b in ("btn-amber-back", "btn-back-login"):
                await js("(()=>{const b=document.getElementById('%s'); if(b) b.click();})()" % b)
            await asyncio.sleep(0.4)

        return problems, None


async def run():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    shutil.rmtree(PROFILE, ignore_errors=True)
    proc = subprocess.Popen(
        ["google-chrome-stable", "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
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

        all_problems = []
        for desktop_mode in (False, True):
            mode = "desktop mode" if desktop_mode else "classic"
            problems, err = await drive(page["webSocketDebuggerUrl"], desktop_mode)
            if err:
                print(f"SKIP  {mode}: {err}")
                return 2
            print(f"{mode}: {len(problems)} problem(s)")
            all_problems += [(mode, k, d) for k, d in problems]

        if not all_problems:
            print("OK  every sign-in method is reachable in both modes")
            return 0
        print()
        for mode, kind, detail in all_problems:
            print(f"FAIL  [{mode}] {kind}: {detail}")
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(PROFILE, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
