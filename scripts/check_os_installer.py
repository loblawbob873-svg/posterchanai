#!/usr/bin/env python3
"""PosterChanOS — the graphical installer, driven in a real browser.

    venv-unified/bin/python scripts/check_os_installer.py [--shots DIR]

Loads the SHIPPED os.js + osinstall.js + client.css against a stubbed `pcInstaller` bridge (the
shape desktop/preload.js exposes) and walks the wizard the way a person on a live USB does: the icon
on the desktop, Welcome, the disk list, the password, the summary with its typed confirmation, the
progress screen, and both endings. The bridge itself — and gentoo.sh's side of the hand-over — are
covered under node/bash by tests/test_gui_installer_bridge.py and
tests/test_gentoo_noninteractive_install.py; this is the half only a browser can see.

Assertions, each a way the installer fails somebody who has booted a USB stick to use it:

  no-icon            A live session has no "Install PosterChanOS" icon, or it is not the FIRST icon.
                     It is the one thing that desktop exists for.
  icon-when-installed An installed machine (pcInstaller.isLive() false) shows the icon. Clicking it
                     could only ever fail, on the machine somebody is keeping.
  no-window          The icon does not open the installer.
  live-disk-offered  The drive the live system runs from can be chosen.
  next-without-disk  Next is enabled with no disk chosen, or says nothing about why it is not.
  passwords-unequal  The wizard continues with two different passwords.
  erase-unconfirmed  Install is enabled before the disk's NAME has been typed.
  native-dialog      A native confirm()/alert()/prompt() was used. They wedge the Electron renderer's
                     focus; the app's own uiConfirm is the only dialog allowed.
  wrong-answers      start() did not receive the disk, password, mode and volume name chosen.
  no-progress        The install screen does not show the percentage and stage the bridge reports.
  reopen-restarts    Opening the installer while a job runs offers to start another install instead
                     of showing the one in progress.
  no-reboot          A finished install offers no restart, or the button does not reach pcPower.
  failure-silent     A failed install does not show what the installer last said, or no way to retry.
  overflow           Something in the window scrolls sideways or sticks out of it.

Exit 0 clean · 1 problems · 2 could not run.
"""
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("PC_CHECK_PORT") or 9531)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-os-installer-check"
GIB = 1024 ** 3

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/css/client.css">
</head><body>
<div class="app" style="display:flex;height:100dvh">
  <aside class="sidebar glass">
    <div class="brand"><img src="/static/posterchan-relay.png" class="brand-logo" alt="PosterChan"></div>
    <nav class="nav">
      <button class="nav-item" data-view="global"><svg class="ic"><use href="#i-globe"></use></svg><span>Social</span></button>
      <button class="nav-item" data-view="notes"><svg class="ic"><use href="#i-note"></use></svg><span>Notes</span></button>
      <button class="nav-item" data-view="bookmarks"><svg class="ic"><use href="#i-bookmark"></use></svg><span>Bookmarks</span></button>
    </nav>
  </aside>
  <div class="main"><div id="feed" class="feed">CLASSIC</div></div>
</div>
<script src="/static/js/client/sprite.js"></script>
<script>
const Q = new URLSearchParams(location.search);
window.__native = [];
window.confirm = (m) => { window.__native.push('confirm'); return true; };
window.alert = (m) => { window.__native.push('alert'); };
window.prompt = (m) => { window.__native.push('prompt'); return ''; };
window.__confirms = [];
window.__PC = {
  toast: m => (window.__toasts = window.__toasts || []).push(m),
  switchView: (v) => { const f = document.getElementById('feed'); if (f) f.innerHTML = '<div>' + v + '</div>'; },
  me: () => null,
  uiConfirm: (m, o) => { window.__confirms.push({ m, o: { ok: o && o.ok, danger: o && o.danger } });
                         return Promise.resolve(window.__confirmAnswer !== false); },
  uiPrompt: () => Promise.resolve(null),
};
window.ClientSettings = { _v:{}, get(k,d){ return k in this._v ? this._v[k] : d; }, set(k,v){ this._v[k]=v; } };
window.Store = { query: () => [] };
window.Relay = { conns: () => [], watch: () => () => {}, wake: () => {}, query: () => Promise.resolve([]),
                 subscribe: () => 1, close: () => {} };

const GIB = 1024 ** 3;
window.__disks = [
  { name:'sda', path:'/dev/sda', size:32*GIB, model:'SanDisk Ultra', tran:'usb', removable:true, small:false,
    mounted:true, selectable:false, why:'This is the drive PosterChanOS is running from.', posterchanLayout:false,
    parts:[{name:'sda3', size:4*GIB, fstype:'hfsplus', label:''}] },
  { name:'nvme0n1', path:'/dev/nvme0n1', size:512*GIB, model:'WD Black SN770', tran:'nvme', removable:false,
    small:false, mounted:false, selectable:true, why:'', posterchanLayout:false,
    parts:[{name:'nvme0n1p1', size:100*1024*1024, fstype:'vfat', label:'SYSTEM'},
           {name:'nvme0n1p2', size:511*GIB, fstype:'ntfs', label:'Windows'}] },
  { name:'sdb', path:'/dev/sdb', size:2000*GIB, model:'Seagate Barracuda', tran:'sata', removable:false,
    small:false, mounted:false, selectable:true, why:'', posterchanLayout:false, parts:[] },
];
/* The bridge, as preload.js exposes it. `status` walks a script: whatever the check sets in
   __statusQueue is handed out one per poll, the last one repeating. */
window.__started = null;
window.__statusQueue = [];
window.__status = { kind:'install', running:false, finished:0, ok:false, progress:{stage:'',percent:0}, log:'' };
window.__rebooted = 0;
window.pcInstaller = {
  isLive: () => Q.get('live') !== '0',
  info: async () => ({ live: Q.get('live') !== '0', available: Q.get('live') !== '0', installer:'/usr/bin/gentoo.sh', efi:true }),
  disks: async () => JSON.parse(JSON.stringify(window.__disks)),
  status: async () => { if (window.__statusQueue.length > 1) window.__status = window.__statusQueue.shift();
                        else if (window.__statusQueue.length) window.__status = window.__statusQueue[0];
                        return JSON.parse(JSON.stringify(window.__status)); },
  start: async (o) => { window.__started = o;
                        window.__status = { kind:'install', running:true, finished:0, ok:false, started:Date.now(),
                          disk:'/dev/'+o.disk, model:'WD Black SN770', message:'Installing PosterChanOS…',
                          progress:{stage:'medium', label:'Finding the live medium', percent:1}, log:'::pc-install:: medium x\n' };
                        return JSON.parse(JSON.stringify(window.__status)); },
};
window.pcPower = { reboot: async () => { window.__rebooted++; return { ok:true }; }, status: async () => ({}) };
if (Q.get('running') === '1') window.__status = { kind:'install', running:true, finished:0, ok:false, started:Date.now()-65000,
  disk:'/dev/sdb', model:'Seagate Barracuda', message:'Installing PosterChanOS…',
  progress:{stage:'copy', label:'Copying PosterChanOS onto the disk', percent:40, copied:45},
  log:'::pc-install:: copy Copying\nCopying the system — this is the slow part.\n   1,000  45%  10MB/s' };
</script>
<script src="/static/js/client/os.js"></script>
<script>window.__ready = true;</script>
</body></html>"""

DRIVE = r"""(async () => {
  const out = window.__out = { steps: {} };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const until = async (fn, ms) => { for (let i = 0; i < (ms || 3000) / 50; i++) { try { if (fn()) return true; } catch(_){} await sleep(50); } return false; };
  const W = () => document.querySelector('.osw.osw-installer') || document.querySelector('.osw.focused');
  const $ = s => (W() || document).querySelector(s);
  const title = () => ($('.pci-head h2') || {}).textContent || '';
  const next = () => $('[data-next]');
  const why = () => ($('[data-why]') || {}).textContent || '';
  const type = (el, v) => { el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })); };
  const overflow = () => { const b = $('.pci-body'); const w = W();
    if (!b || !w) return 'no window';
    if (b.scrollWidth > b.clientWidth + 1) return 'body scrolls sideways (' + b.scrollWidth + ' > ' + b.clientWidth + ')';
    const wr = w.getBoundingClientRect();
    for (const e of w.querySelectorAll('.pci *')) { const r = e.getBoundingClientRect();
      if (r.width && e.offsetParent !== null && (r.right > wr.right + 1 || r.left < wr.left - 1)) return e.className + ' sticks out'; }
    return ''; };

  PCOS.enter(); await sleep(250);
  const icons = [...document.querySelectorAll('.os-icons .os-icon, .os-icon')].map(b => b.dataset.view);
  out.icons = icons;
  if (Q.get('live') === '0') return out;
  const icon = document.querySelector('.os-icon[data-view="__installer"]');
  out.iconLabel = icon ? icon.textContent.trim() : '';
  if (!icon) return out;
  icon.click();
  out.window = await until(() => $('.pci-main .pci-head h2'), 4000);
  out.steps.first = title();
  if (Q.get('running') === '1') {
    out.reopenTitle = title();
    out.reopenPct = ($('.pci-bar-row b') || {}).textContent || '';
    window.__statusQueue = [
      Object.assign({}, window.__status, { running:false, finished:Date.now(), ok:false, exitCode:1,
        message:'The installer failed (exit 1)', progress:{ stage:'copy', label:'x', percent:40 },
        log:'::pc-install:: copy x\nCopying the system — this is the slow part.\nrsync: write failed on "/tmp/install/usr/lib/x": No space left on device (28)\nThe copy did not complete — nothing was installed.' })];
    out.failed = await until(() => $('.pci-result.bad'), 5000);
    out.failTail = ($('.pci-tail') || {}).textContent || '';
    out.retry = !!$('[data-retry]');
    out.failOverflow = overflow();
    window.__shot = 'failed';
    return out;
  }
  out.welcomeOverflow = overflow();
  return out;
})()"""

HELPERS = r"""
  const out = window.__out;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const until = async (fn, ms) => { for (let i = 0; i < (ms || 3000) / 50; i++) { try { if (fn()) return true; } catch(_){} await sleep(50); } return false; };
  const W = () => document.querySelector('.osw.osw-installer') || document.querySelector('.osw.focused');
  const $ = s => (W() || document).querySelector(s);
  const title = () => ($('.pci-head h2') || {}).textContent || '';
  const next = () => $('[data-next]');
  const why = () => ($('[data-why]') || {}).textContent || '';
  const type = (el, v) => { el.value = v; el.dispatchEvent(new Event('input', { bubbles: true })); };
  const overflow = () => { const b = $('.pci-body'); const w = W();
    if (!b || !w) return 'no window';
    if (b.scrollWidth > b.clientWidth + 1) return 'body scrolls sideways (' + b.scrollWidth + ' > ' + b.clientWidth + ')';
    const wr = w.getBoundingClientRect();
    for (const e of w.querySelectorAll('.pci *')) { const r = e.getBoundingClientRect();
      if (r.width && e.offsetParent !== null && (r.right > wr.right + 1 || r.left < wr.left - 1)) return e.className + ' sticks out'; }
    return ''; };
"""

DISK = "(async () => {" + HELPERS + r"""
  next().click(); await until(() => $('.pci-disk'), 3000);
  out.steps.disk = title();
  out.liveDiskDisabled = !!$('.pci-disk.off input[value="sda"][disabled]');
  out.liveWhy = ($('.pci-disk.off .pci-disk-why') || {}).textContent || '';
  out.nextWithoutDisk = !next().disabled;
  out.whyWithoutDisk = why();
  out.diskOverflow = overflow();
  const nv = $('input[name="pci-disk"][value="nvme0n1"]'); nv.click(); nv.dispatchEvent(new Event('change', { bubbles:true }));
  await sleep(80);
  out.nextWithDisk = !next().disabled;
  return out;
})()"""

SECURITY = "(async () => {" + HELPERS + r"""
  next().click(); await until(() => $('[data-pw]'), 2000);
  out.steps.security = title();
  type($('[data-pw]'), 'correct horse battery'); type($('[data-pw2]'), 'correct horse batterY');
  out.nextUnequal = !next().disabled; out.whyUnequal = why();
  type($('[data-pw2]'), 'correct horse battery');
  out.nextEqual = !next().disabled;
  out.securityOverflow = overflow();
  return out;
})()"""

SUMMARY = "(async () => {" + HELPERS + r"""
  next().click(); await until(() => $('.pci-sum'), 2000);
  out.steps.summary = title();
  out.summaryText = ($('.pci-sum') || {}).textContent || '';
  out.installBeforeTyped = !next().disabled; out.whyBeforeTyped = why();
  type($('[data-typed]'), 'nvme0n');
  out.installPartial = !next().disabled;
  type($('[data-typed]'), 'nvme0n1');
  out.installTyped = !next().disabled;
  out.summaryOverflow = overflow();
  return out;
})()"""

FINISH = r"""(async () => {
  const out = {};
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const until = async (fn, ms) => { for (let i = 0; i < (ms || 3000) / 50; i++) { try { if (fn()) return true; } catch(_){} await sleep(50); } return false; };
  const W = () => document.querySelector('.osw.osw-installer') || document.querySelector('.osw.focused');
  const $ = s => (W() || document).querySelector(s);
  $('[data-next]').click();
  out.progress = await until(() => $('.pci-progress'), 4000);
  out.confirms = window.__confirms;
  out.started = window.__started;
  window.__statusQueue = [
    Object.assign({}, window.__status, { progress:{ stage:'copy', label:'Copying PosterChanOS onto the disk', percent:44, copied:50 },
      log:'::pc-install:: copy Copying\nCopying the system — this is the slow part.\n  2,000,000,000  50%  90MB/s' })];
  await until(() => /44%/.test(($('.pci-bar-row') || {}).textContent || ''), 4000);
  out.pctText = ($('.pci-bar-row') || {}).textContent || '';
  out.phaseActive = ($('.pci-phases li.active') || {}).textContent || '';
  out.phaseDone = [...(W() || document).querySelectorAll('.pci-phases li.done')].map(l => l.textContent.trim());
  const lg = $('[data-log]'); if (lg) lg.click(); await sleep(80);
  out.logShown = /slow part/.test(($('[data-logpre]') || {}).textContent || '');
  return out;
})()"""

DONE = r"""(async () => {
  const out = {};
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const until = async (fn, ms) => { for (let i = 0; i < (ms || 3000) / 50; i++) { try { if (fn()) return true; } catch(_){} await sleep(50); } return false; };
  const W = () => document.querySelector('.osw.osw-installer') || document.querySelector('.osw.focused');
  const $ = s => (W() || document).querySelector(s);
  window.__statusQueue = [Object.assign({}, window.__status, { running:false, finished:Date.now(), ok:true, exitCode:0,
    message:'PosterChanOS is installed', progress:{ stage:'done', label:'PosterChanOS is installed', percent:100 } })];
  out.done = await until(() => $('.pci-result.ok'), 5000);
  out.pct = ($('.pci-bar-row b') || {}).textContent || '';
  const rb = $('[data-reboot]'); out.rebootButton = !!rb;
  if (rb) { rb.click(); await sleep(100); }
  out.rebooted = window.__rebooted;
  out.native = window.__native;
  const b = $('.pci-body'); out.doneOverflow = b && b.scrollWidth > b.clientWidth + 1;
  return out;
})()"""


async def drive(base, shots):
    import websockets
    subprocess.run(["rm", "-rf", PROFILE], check=False)
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", f"--remote-debugging-port={PORT}",
         f"--user-data-dir={PROFILE}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    problems = []
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

            async def js(expr):
                r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
                if r.get("exceptionDetails"):
                    print("  JS:", json.dumps(r["exceptionDetails"])[:800])
                    return None
                return r["result"].get("value")

            async def shot(name):
                if not shots:
                    return
                r = await call("Page.captureScreenshot", {"format": "png"})
                with open(os.path.join(shots, name + ".png"), "wb") as fh:
                    fh.write(base64.b64decode(r["data"]))

            async def load(q):
                await call("Page.navigate", {"url": base + q})
                for _ in range(80):
                    await asyncio.sleep(0.25)
                    if await js("window.__ready === true && !!window.PCOS"):
                        return True
                return False

            await call("Runtime.enable")
            await call("Page.enable")
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})

            def bad(code, msg):
                problems.append((code, msg))

            # ---- an installed machine: no icon
            if not await load("?live=0"):
                print("SKIP  the page never loaded")
                return 2
            out = await js(DRIVE) or {}
            if "__installer" in (out.get("icons") or []):
                bad("icon-when-installed", "an installed machine shows the Install PosterChanOS icon")

            # ---- a live session, start to finish
            await load("?live=1")
            out = await js(DRIVE) or {}
            if out.get("window"):
                await shot("1-welcome")
                for name, step in (("2-disk", DISK), ("3-security", SECURITY), ("4-summary", SUMMARY)):
                    out = await js(step) or out
                    if name != "4-summary":
                        await shot(name)
            icons = out.get("icons") or []
            if not icons or icons[0] != "__installer":
                bad("no-icon", f"the first desktop icon is {icons[:1]}, not the installer")
            elif "Install PosterChanOS" not in (out.get("iconLabel") or ""):
                bad("no-icon", f"the installer icon is labelled {out.get('iconLabel')!r}")
            if not out.get("window"):
                bad("no-window", "clicking the icon opened no installer")
            else:
                if out["steps"].get("first") != "Install PosterChanOS":
                    bad("no-window", f"the installer opened on {out['steps'].get('first')!r}")
                if not out.get("liveDiskDisabled") or "running from" not in (out.get("liveWhy") or ""):
                    bad("live-disk-offered", "the live USB can be chosen, or its row does not say why not")
                if out.get("nextWithoutDisk") or not out.get("whyWithoutDisk"):
                    bad("next-without-disk", f"Next={out.get('nextWithoutDisk')} why={out.get('whyWithoutDisk')!r}")
                if not out.get("nextWithDisk"):
                    bad("next-without-disk", "Next stayed disabled after choosing a disk")
                if out.get("nextUnequal") or "not the same" not in (out.get("whyUnequal") or ""):
                    bad("passwords-unequal", "Next was enabled with two different passwords")
                if not out.get("nextEqual"):
                    bad("passwords-unequal", "Next stayed disabled with matching passwords")
                if out.get("installBeforeTyped") or out.get("installPartial") or not out.get("installTyped"):
                    bad("erase-unconfirmed", f"before={out.get('installBeforeTyped')} partial={out.get('installPartial')} "
                                             f"typed={out.get('installTyped')}")
                if "WD Black SN770" not in (out.get("summaryText") or "") or "nvme0n1" not in (out.get("summaryText") or ""):
                    bad("wrong-answers", "the summary does not name the chosen disk")
                for k in ("welcomeOverflow", "diskOverflow", "securityOverflow", "summaryOverflow"):
                    if out.get(k):
                        bad("overflow", f"{k}: {out[k]}")
                await shot("4-summary")
                fin = await js(FINISH) or {}
                st = fin.get("started") or {}
                if not fin.get("confirms") or not (fin["confirms"][0]["o"] or {}).get("danger"):
                    bad("native-dialog", "Install did not ask through the app's own (danger) uiConfirm")
                if st != {"disk": "nvme0n1", "size": 512 * GIB, "rootName": "gentoo",
                          "password": "correct horse battery", "mode": "fresh"}:
                    bad("wrong-answers", f"start() received {st}")
                if "44%" not in (fin.get("pctText") or "") or "50% of the files" not in (fin.get("pctText") or ""):
                    bad("no-progress", f"the bar reads {fin.get('pctText')!r}")
                if "Copying" not in (fin.get("phaseActive") or "") or fin.get("phaseDone") != ["Preparing the disk"]:
                    bad("no-progress", f"phases active={fin.get('phaseActive')!r} done={fin.get('phaseDone')}")
                if not fin.get("logShown"):
                    bad("no-progress", "Show details does not show the installer's own output")
                await shot("5-installing")
                done = await js(DONE) or {}
                if not done.get("done") or done.get("pct") != "100%":
                    bad("no-reboot", f"the finished screen did not appear (pct={done.get('pct')!r})")
                await shot("6-done")
                if not done.get("rebootButton") or done.get("rebooted") != 1:
                    bad("no-reboot", "Restart now did not reach pcPower.reboot")
                if done.get("native"):
                    bad("native-dialog", f"native dialogs were used: {done['native']}")
                if done.get("doneOverflow"):
                    bad("overflow", "the finished screen scrolls sideways")

            # ---- reopening while a job runs, which then fails
            await load("?live=1&running=1")
            out = await js(DRIVE) or {}
            if out.get("reopenTitle") != "Installing PosterChanOS" or "40%" not in (out.get("reopenPct") or ""):
                bad("reopen-restarts", f"a running install reopened on {out.get('reopenTitle')!r} "
                                       f"({out.get('reopenPct')!r}) instead of its progress")
            if not out.get("failed") or "No space left on device" not in (out.get("failTail") or "") or not out.get("retry"):
                bad("failure-silent", f"failed={out.get('failed')} tail={out.get('failTail')!r} retry={out.get('retry')}")
            if out.get("failOverflow"):
                bad("overflow", f"failed screen: {out['failOverflow']}")
            await shot("7-failed")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        subprocess.run(["rm", "-rf", PROFILE], check=False)

    if problems:
        for code, msg in problems:
            print(f"FAIL  {code}: {msg}")
        return 1
    print("OK  installer: icon (live only, first) → welcome → disk → password → typed confirmation → "
          "progress → restart; reopen shows the running job; a failure shows its log")
    return 0


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    shots = ""
    if "--shots" in sys.argv:
        shots = sys.argv[sys.argv.index("--shots") + 1]
        os.makedirs(shots, exist_ok=True)
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="osinstcheck-")
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
    base = f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    try:
        return asyncio.run(drive(base, shots))
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
