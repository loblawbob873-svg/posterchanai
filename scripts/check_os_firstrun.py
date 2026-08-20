#!/usr/bin/env python3
"""PosterChanOS — the first-run wizard, driven in a real browser.

    venv-unified/bin/python scripts/check_os_firstrun.py

`tests/client/test_os_firstrun.py` runs the step MACHINE (osfirstrun.js) under node and says which
step is next. This drives the screens: a fresh machine has no network, no instance, no Tor answer
and no key, and somebody who has never seen this computer before has to be able to get from that to
a desktop. Every failure below leaves a machine that boots to something unusable, and none of them
throws.

Assertions:

  no-wizard          A machine with nothing set up boots straight to the desktop, with no way to
                     join a network — which on a fresh laptop is a computer that cannot be used at
                     all, because every later screen needs the radio that nothing offered to turn on.

  wizard-on-windows  The wizard appears where PosterChan is NOT the operating system. The bridges
                     exist in the desktop app on every platform, so "the bridge is there" is not the
                     question — a Windows user opening a chat client must not be asked for their
                     wifi and a Unix account. Same rule, and the same failure, as the OS tray.

  order-wrong        The network is not asked for first. Ask for it fourth and somebody types an
                     instance URL at a machine with no radio and is told the instance is down.

  unreadable-is-online  NetworkManager not answering is treated as "fine, carry on". An empty wifi
                     list means both "no networks here" and "I could not ask", and only one of those
                     is worth waiting in — carrying on produces four screens of failures whose real
                     cause was two screens ago.

  step-not-answered  A step that was answered is asked again on the next boot. "No instance" and
                     "no Tor" are ANSWERS — nothing else in the client records them, so without this
                     the wizard asks the same two questions on every single boot, for ever.

  no-way-past-signin The sign-in step does not reach the client's own gate, so a machine that has
                     everything else set up has no way to get a key onto it.

Exit 0 clean · 1 problems · 2 could not run.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("PC_CHECK_PORT") or 9492)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-osfirstrun-check"

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/css/client.css">
</head><body>
<div class="app"><div id="feed">CLASSIC</div></div>
<script>
/* The client, as the wizard reaches it. `showAuth` is RECORDED rather than performed: the gate is
 * app.js's and the assertion here is that the wizard hands off to it instead of growing a second
 * sign-in form of its own. */
window.__shown = [];
window.__PC = {
  toast: m => (window.__toasts = window.__toasts || []).push(m),
  apiBase: () => window.__instance || '',
  setInstance: (u) => { window.__instance = u; window.__setInstance = u; },
  showAuth: () => { window.__shown.push('auth'); },
  uiPrompt: async () => window.__password === undefined ? 'hunter2' : window.__password,
  uiConfirm: async () => true,
};
window.ME = null; window.GUEST = true;

window.__net = { online: false, readable: true, joined: null,
  list: [{ ssid: 'Tribble', signal: 71, secure: true },
         { ssid: 'Tribble', signal: 40, secure: true },
         { ssid: 'Cafe',    signal: 55, secure: false }] };
window.pcNet = {
  status: async () => { if (!window.__net.readable) throw new Error('no NetworkManager');
                        return { online: window.__net.online, kind: 'wifi', name: 'x', signal: 70 }; },
  wifi: async () => { if (!window.__net.readable) throw new Error('no NetworkManager');
                      return window.__net.list; },
  connect: async (ssid, pw) => { window.__net.joined = [ssid, pw];
                                 window.__net.online = true; return { ok: true }; },
};
/* The compositor. `__compositor = false` is the Windows and macOS case: the bridge is there and
 * nothing answers it. */
window.__compositor = true;
window.pcWM = {
  windows: async () => { if (!window.__compositor) throw new Error('no compositor socket');
                         return []; },
  focus: async () => [], subscribe: async () => true, onEvent: () => () => {},
};
window.pcOS = { provision: async () => { window.__provisioned = true; return { ok: true }; } };
window.pcPower = { status: async () => ({}) };
window.pcAudio = { status: async () => ({}) };
window.pcShell = {};                      // no bundled tor on this build
window.PCOS = { enter: () => { window.__entered = true; } };
</script>
<script src="/static/js/client/osfirstrun.js"></script>
<script src="/static/js/client/osshell.js"></script>
<script src="/static/js/client/osfirstrunui.js"></script>
<script>window.__ready = true;</script>
</body></html>"""


DRIVE = r"""(async () => {
  const out = { problems: [] };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const bad = (k, d) => out.problems.push({ k, d });
  const card = () => document.querySelector('#osfr .osfr-card');
  const title = () => { const c = card(); return c ? c.querySelector('.osfr-h').textContent : ''; };
  const btn = (name) => { const c = card(); return c ? c.querySelector('[data-fr="' + name + '"]') : null; };

  try { localStorage.clear(); } catch (_) {}

  /* ── it appears at all, and the network comes first ─────────────────────────────────────────── */
  const shown = await window.PCFirstRunUI.boot();
  await sleep(400);
  out.first = title();
  if (!shown || !card()) {
    bad('no-wizard', 'a machine with nothing set up showed no first-run screen');
    return out;
  }
  if (!/network|wifi/i.test(out.first))
    bad('order-wrong', 'the first screen is "' + out.first + '" and not the network');

  /* ── a NetworkManager that cannot be asked is not a network that is fine ────────────────────── */
  window.__net.readable = false;
  await window.PCFirstRunUI.run();
  await sleep(400);
  out.blocked = title();
  if (!/cannot see|could not/i.test(out.blocked))
    bad('unreadable-is-online', 'an unreadable NetworkManager showed "' + out.blocked
                              + '" instead of stopping and saying so');
  window.__net.readable = true;

  /* ── joining a network advances ─────────────────────────────────────────────────────────────── */
  await window.PCFirstRunUI.run();
  await sleep(500);
  const rows = [...document.querySelectorAll('#osfr [data-ssid]')];
  out.ssids = rows.map(r => r.dataset.ssid);
  // Deduped by ssid, strongest kept — the same rule the tray applies, or two lists of the same
  // networks disagree about their order.
  if (out.ssids.filter(s => s === 'Tribble').length !== 1)
    bad('order-wrong', 'the wifi list repeats an SSID: ' + JSON.stringify(out.ssids));
  const t = rows.find(r => r.dataset.ssid === 'Tribble');
  if (!t) { bad('no-wizard', 'the wifi list is empty on a machine with networks in range');
            return out; }
  await t.onclick();
  await sleep(600);
  out.joined = window.__net.joined;
  out.afterJoin = title();
  if (!out.joined) bad('no-wizard', 'pressing a network joined nothing');
  if (!/instance/i.test(out.afterJoin))
    bad('order-wrong', 'after the network the wizard showed "' + out.afterJoin + '", not the instance');

  /* ── "no instance" is an ANSWER and is remembered ───────────────────────────────────────────── */
  const skip = btn('skip');
  if (!skip) { bad('step-not-answered', 'the instance step offers no way to run without one');
               return out; }
  skip.click();
  await sleep(500);
  out.afterSkip = title();
  /* No bundled tor on this stub, so that step answers itself and the wizard must land on sign-in.
     A wizard that stops on a switch it cannot wire to anything is a machine that cannot finish. */
  if (!/sign in/i.test(out.afterSkip))
    bad('order-wrong', 'after skipping the instance the wizard showed "' + out.afterSkip + '"');

  const inb = btn('in');
  if (!inb) { bad('no-way-past-signin', 'the sign-in step has no button'); return out; }
  inb.click();
  await sleep(300);
  out.handedOff = window.__shown.slice();
  if (!out.handedOff.includes('auth'))
    bad('no-way-past-signin', 'the sign-in step did not open the client\'s own gate');

  /* ── the answers survive a reboot ───────────────────────────────────────────────────────────── */
  window.ME = { npub: 'npub1exampleexampleexampleexampleexampleexampleexampleexample' };
  window.GUEST = false;
  await window.PCFirstRunUI.run();
  await sleep(700);
  out.entered = !!window.__entered;
  out.provisioned = !!window.__provisioned;
  out.stillUp = !!card();
  if (out.stillUp)
    bad('step-not-answered', 'everything is answered and the wizard is still on screen ("'
                           + title() + '") — the instance or Tor answer was not remembered');
  if (!out.provisioned)
    bad('no-wizard', 'the machine never made an account for the key that signed in');
  if (!out.entered) bad('no-wizard', 'the wizard finished without showing the desktop');

  /* ── and it must never appear where this is not the operating system ────────────────────────── */
  try { localStorage.clear(); } catch (_) {}
  window.__compositor = false;
  window.ME = null; window.GUEST = true; window.__instance = '';
  /* PCOSShell caches its answer, so the Windows case is checked with a FRESH module rather than by
     poking a private — a cached `true` here would make this assertion pass by accident. */
  out.windows = await (async () => {
    const src = await (await fetch('/static/js/client/osshell.js')).text();
    const src2 = await (await fetch('/static/js/client/osfirstrunui.js')).text();
    const g = {};
    const mk = new Function('root', 'document', 'localStorage', 'fetch', src + '\n' + src2
                            + '\nreturn { S: root.PCOSShell, U: root.PCFirstRunUI };');
    const mod = mk(Object.assign(Object.create(window), g), document, localStorage, fetch);
    return { detected: await mod.S.detect(), shown: await mod.U.boot() };
  })();
  if (out.windows.detected || out.windows.shown)
    bad('wizard-on-windows', 'the setup wizard offered itself on a machine with no compositor');

  return out;
})()"""


async def drive(url):
    import websockets  # noqa: F401
    subprocess.run(["rm", "-rf", PROFILE], check=False)
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
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
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": 1400, "height": 900, "deviceScaleFactor": 1, "mobile": False})
            await call("Page.navigate", {"url": url})
            ok = False
            for _ in range(80):
                await asyncio.sleep(0.25)
                r = await call("Runtime.evaluate",
                               {"expression": "window.__ready === true && !!window.PCFirstRunUI",
                                "returnByValue": True})
                if r and r.get("result", {}).get("value") is True:
                    ok = True
                    break
            if not ok:
                print("SKIP  the page never became ready")
                return 2

            r = await call("Runtime.evaluate",
                           {"expression": DRIVE, "returnByValue": True, "awaitPromise": True})
            if r.get("exceptionDetails"):
                print("FAIL  the driver threw:", json.dumps(r["exceptionDetails"])[:900])
                return 1
            out = r["result"].get("value") or {}
            for k in ("first", "blocked", "ssids", "joined", "afterJoin", "afterSkip",
                      "handedOff", "provisioned", "entered", "stillUp", "windows"):
                if k in out:
                    print(f"  {k}: {json.dumps(out[k])}")
            problems = out.get("problems") or []
            if not problems:
                print("PASS  a fresh machine can be set up: wifi → instance → tor → sign in → account")
                return 0
            for p in problems:
                print(f"FAIL  {p['k']}: {p['d']}")
            return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="osfirstrun-")
    with open(os.path.join(tmp, "index.html"), "w") as fh:
        fh.write(PAGE)

    class H(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            path = path.split("?")[0].split("#")[0]
            if path.startswith("/static/"):
                return os.path.join(ROOT, path.lstrip("/"))
            return os.path.join(tmp, path.lstrip("/") or "index.html")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    try:
        return asyncio.run(drive(url))
    finally:
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
