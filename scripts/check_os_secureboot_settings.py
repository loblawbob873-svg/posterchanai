#!/usr/bin/env python3
"""System Settings → Secure Boot, driven in a real headless Chrome against the SHIPPED os.js.

Asked 2026-10-27 with the installer's checkbox ("i hope there is some secure boot thing in the gui installer").
The page is how a PosterChanOS owner sets up anti-cheat mode without a terminal, and how they learn the ONE-TIME
CODE MokManager asks for at the next boot -- miss that and the set-up was for nothing. Walked against a stateful
fake of `window.pcAnticheat` (the preload bridge -- Electron cannot be driven here):

  off            The costs are stated (disk password at startup, no hibernation, MokManager) before the button.
  setup          Set up calls the helper once, the page follows the running service BY ITSELF (no click).
  enrol          The one-time code is shown, with the MokManager steps; Undo is offered.
  enabled        Once Secure Boot is on the code is gone and pc-eac-check is named.
  undo           Asks first (in-app dialog), then calls the helper.
  failed         The helper's log is shown with Try again.
  refused        An account that may not change it is told so (the bridge's message), not left on "Starting…".
  nvidia         An NVIDIA machine is told why, with no button.
  absent         No bridge (Windows, another Linux) = no Secure Boot page at all.
  no-native      Not one window.confirm/alert/prompt.

Exit 0 = clean, 1 = problems (printed), 2 = could not run (no Chrome / websockets).
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check_os_desktop import PAGE, ROOT  # noqa: E402  (the shipped desktop, one definition)

PORT = int(os.environ.get("PC_CHECK_PORT") or 9483)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-os-secureboot-settings"

STUBS = r"""<script>
window.__native = [];
window.confirm = t => { window.__native.push('confirm'); return true; };
window.alert = t => { window.__native.push('alert'); };
window.prompt = t => { window.__native.push('prompt'); return null; };
window.__asked = []; window.__toasts = [];
window.__PC.uiConfirm = (t) => { window.__asked.push(String(t)); return Promise.resolve(true); };
window.__PC.toast = (t) => { window.__toasts.push(String(t)); };
const A = window.__ac = { state: 'off', secureBoot: 'disabled', nvidia: false, installed: false, password: '',
                          log: '', calls: [], refuse: false };
window.pcAnticheat = {
  available: () => A.here !== false,
  status: async () => { const o = { state: A.state, requested: A.state === 'requested', installed: A.installed,
                                    secureBoot: A.secureBoot, nvidia: A.nvidia };
                        if (A.state === 'enrol') o.password = A.password;
                        if (A.state === 'failed') o.log = A.log;
                        return o; },
  setup: async () => { A.calls.push('setup');
    if (A.refuse) throw new Error("Error invoking remote method 'pc:anticheat:setup': Error: Only the administrator of this computer can change Secure Boot.");
    A.state = 'requested'; return window.pcAnticheat.status(); },
  undo: async () => { A.calls.push('undo'); A.state = 'undoing'; return window.pcAnticheat.status(); },
};
</script>
<script src="/static/js/client/os.js"></script>"""

DRIVE = r"""(async () => {
  const out = {}, sleep = ms => new Promise(r => setTimeout(r, ms)), A = window.__ac;
  const $ = s => document.querySelector(s);
  const until = async (fn, ms) => { const t = Date.now() + (ms || 8000);
    while (Date.now() < t) { try { if (fn()) return true; } catch (_) {} await sleep(100); } return false; };
  const pane = () => $('[data-settings-page="secureboot"]');
  const text = () => (pane() || {}).textContent || '';
  const state = () => (($('[data-sb-state]') || {}).dataset || {}).sbState || '';
  const open = async () => { const nav = $('.os-set-nav [data-page="secureboot"]'); if (!nav) return false;
    nav.click(); await until(() => state() || $('[data-sb-error]'), 4000); return true; };
  PCOS.enter(); await sleep(150);
  PCOS.openSystemSettings(); await sleep(300);
  out.nav = await open();
  out.mobileOption = !!$('[data-settings-mobile] option[value="page:secureboot"]');
  if (!out.nav) return out;
  out.off = { state: state(), text: text(), button: !!$('[data-sb-setup]'),
              alone: document.querySelectorAll('[data-settings-page]:not([hidden])').length === 1 };
  out.overflowOff = Math.max(0, pane().scrollWidth - pane().clientWidth);
  $('[data-sb-setup]').click();
  await until(() => state() === 'requested', 3000);
  out.requested = { state: state(), calls: A.calls.slice(), text: text() };
  A.state = 'running'; await until(() => state() === 'running', 8000);
  out.followedRunning = state();
  A.state = 'enrol'; A.password = '48151623';
  await until(() => state() === 'enrol', 8000);
  out.enrol = { state: state(), code: (($('[data-sb-code]') || {}).textContent || '').trim(), text: text(),
                undo: !!$('[data-sb-undo]') };
  A.state = 'enabled'; A.installed = true; A.secureBoot = 'enabled'; A.password = '';
  $('.os-set-nav [data-page="secureboot"]').click(); await until(() => state() === 'enabled', 4000);
  out.enabled = { state: state(), text: text(), code: !!$('[data-sb-code]') };
  $('[data-sb-undo]').click();
  await until(() => A.calls.includes('undo'), 3000);
  out.undo = { asked: window.__asked.slice(-1)[0] || '', calls: A.calls.slice() };
  await until(() => state() === 'undoing', 3000);
  out.undoing = state();
  A.state = 'failed'; A.installed = false; A.log = 'emerge: there are no ebuilds to satisfy "sys-boot/shim"';
  await until(() => state() === 'failed', 8000);
  out.failed = { log: (($('[data-sb-log]') || {}).textContent || ''), retry: !!$('[data-sb-setup]') };
  A.state = 'off'; A.refuse = true;
  $('.os-set-nav [data-page="secureboot"]').click(); await until(() => state() === 'off', 4000);
  $('[data-sb-setup]').click();
  await until(() => window.__toasts.length, 3000);
  await until(() => state() === 'off' && $('[data-sb-setup]') && !$('[data-sb-setup]').disabled, 3000);
  out.refused = { toast: window.__toasts.slice(-1)[0] || '', state: state(),
                  button: (($('[data-sb-setup]') || {}).textContent || '') };
  A.nvidia = true; A.refuse = false;
  $('.os-set-nav [data-page="secureboot"]').click(); await sleep(400);
  out.nvidia = { text: text(), button: !!$('[data-sb-setup]') };
  A.here = false;
  $('.os-set-nav [data-page="displays"]').click(); await sleep(400);
  out.notHereNav = !!$('.os-set-nav [data-page="secureboot"]');
  delete window.pcAnticheat;
  $('.os-set-nav [data-page="displays"]').click(); await sleep(400);
  out.absentNav = !!$('.os-set-nav [data-page="secureboot"]');
  out.native = window.__native.slice();
  return out;
})()"""


def verdict(o):
    p = []
    if not o.get("nav") or not o.get("mobileOption"):
        return [f"no Secure Boot page in System Settings (nav={o.get('nav')} mobile={o.get('mobileOption')})"]
    f = o.get("off", {})
    t = f.get("text", "")
    if f.get("state") != "off" or not f.get("button") or not f.get("alone"):
        p.append(f"off: state={f.get('state')!r} button={f.get('button')} onlyPage={f.get('alone')}")
    for need in ("disk password", "hibernation", "MokManager", "600 MB"):
        if need not in t:
            p.append(f"off: the costs are not stated before the button (missing {need!r})")
    if o.get("overflowOff", 0) > 1:
        p.append(f"the page scrolls sideways by {o['overflowOff']}px")
    r = o.get("requested", {})
    if r.get("state") != "requested" or r.get("calls") != ["setup"]:
        p.append(f"setup: {r.get('state')!r} calls={r.get('calls')}")
    if o.get("followedRunning") != "running":
        p.append(f"the page did not follow the running set-up by itself (shows {o.get('followedRunning')!r})")
    e = o.get("enrol", {})
    if e.get("state") != "enrol" or e.get("code") != "48151623" or "Enroll MOK" not in e.get("text", "") \
            or not e.get("undo"):
        p.append(f"enrol: the one-time code and the MokManager steps are not shown: {e}")
    en = o.get("enabled", {})
    if en.get("state") != "enabled" or en.get("code") or "pc-eac-check" not in en.get("text", ""):
        p.append(f"enabled: code still shown or pc-eac-check not named: {en}")
    u = o.get("undo", {})
    if "Turn anti-cheat mode off" not in u.get("asked", "") or u.get("calls", [])[-1:] != ["undo"]:
        p.append(f"undo did not ask first through the app's dialog: {u}")
    if o.get("undoing") != "undoing":
        p.append(f"after undo the page reads {o.get('undoing')!r}")
    fl = o.get("failed", {})
    if "sys-boot/shim" not in fl.get("log", "") or not fl.get("retry"):
        p.append(f"failed: the helper's log or Try again is missing: {fl}")
    rf = o.get("refused", {})
    if rf.get("toast") != "Only the administrator of this computer can change Secure Boot." or rf.get("state") != "off" \
            or "Starting" in rf.get("button", ""):
        p.append(f"refused: {rf}")
    nv = o.get("nvidia", {})
    if nv.get("button") or "NVIDIA" not in nv.get("text", ""):
        p.append(f"nvidia: {nv}")
    if o.get("notHereNav"):
        p.append("a desktop whose bridge says the helper is not installed still shows a Secure Boot page")
    if o.get("absentNav"):
        p.append("a desktop without the bridge still shows a Secure Boot page")
    if o.get("native"):
        p.append(f"native dialogs were used: {o.get('native')} (they wedge the desktop's focus)")
    return p


async def drive(url):
    import websockets
    shutil.rmtree(PROFILE, ignore_errors=True)
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--window-size=1024,768",
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
                await asyncio.sleep(.5)
        if not page:
            print("SKIP  could not start Chrome")
            return 2
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            ident = [0]

            async def call(method, params=None):
                ident[0] += 1
                mine = ident[0]
                await ws.send(json.dumps({"id": mine, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == mine:
                        return msg.get("result", {})

            await call("Runtime.enable")
            await call("Page.enable")
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": 1024, "height": 768, "deviceScaleFactor": 1, "mobile": False})
            await call("Page.navigate", {"url": url})
            for _ in range(120):
                r = await call("Runtime.evaluate", {"expression": "window.__ready === true && !!window.PCOS",
                                                    "returnByValue": True})
                if r.get("result", {}).get("value"):
                    break
                await asyncio.sleep(.1)
            else:
                print("SKIP  the desktop module never loaded")
                return 2
            r = await call("Runtime.evaluate", {"expression": DRIVE, "awaitPromise": True, "returnByValue": True})
            if r.get("exceptionDetails"):
                print("FAIL  the drive script threw: " + json.dumps(r["exceptionDetails"])[:800])
                return 1
            out = r.get("result", {}).get("value") or {}
            shots = os.environ.get("PC_CHECK_SHOTS")
            if shots:
                import base64
                r = await call("Page.captureScreenshot", {"format": "png"})
                with open(os.path.join(shots, "secureboot-last.png"), "wb") as fh:
                    fh.write(base64.b64decode(r.get("data", "")))
    finally:
        proc.terminate()
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(PROFILE, ignore_errors=True)

    problems = verdict(out)
    if os.environ.get("PC_DEBUG"):
        print(json.dumps(out, indent=1)[:6000])
    if problems:
        print("FAIL " + "\nFAIL ".join(problems))
        return 1
    print("OK  System Settings → Secure Boot: costs first, set-up followed, one-time code, enabled, undo, "
          "failure log, refusal, NVIDIA, absent bridge")
    return 0


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    marker = '<script src="/static/js/client/os.js"></script>'
    if marker not in PAGE:
        print("SKIP  check_os_desktop's page no longer loads os.js the way this check injects into")
        return 2
    tmp = tempfile.mkdtemp(prefix="ossecureboot-")
    with open(os.path.join(tmp, "index.html"), "w") as fh:
        fh.write(PAGE.replace(marker, STUBS, 1))

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
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
