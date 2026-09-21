#!/usr/bin/env python3
"""System Settings → PosterChan Server, driven in a real headless Chrome against the SHIPPED os.js.

The page is the only way a PosterChanOS owner turns the bundled server on, and every one of its failure
shapes is silent: a button that is enabled when it cannot work, a job whose progress never reaches the
screen, an instance switch that reloads the app at the wrong address. So this walks the whole life of it
against a stateful fake of `window.pcServer` (the preload bridge — Electron cannot be driven here):

  not-set-up     Before anything is installed the page says so (never "stopped"), Enable is the only
                 action, and every AI install is refused with the reason.
  enable         Enable asks first (in-app dialog, the size and the ports named), calls the helper ONCE,
                 shows the installer's own log while the job runs, and ends at Running with the admin
                 panel reachable.
  instance       "Use this server in this app" is off until asked, points the app at 127.0.0.1:<port>,
                 remembers the previous instance, and "Stop using it" restores exactly that one.
  ai             Music/video/voice stay refused until "AI chat + images" is installed, then open up.
  logs/disable   The journal is shown on demand; Disable asks first.
  unavailable    A machine without the server says so and offers nothing.
  no-native      Not one window.confirm/alert/prompt (they wedge the Electron renderer's focus).

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

PORT = int(os.environ.get("PC_CHECK_PORT") or 9477)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-os-server-settings"

STUBS = r"""<script>
window.__native = [];
window.confirm = t => { window.__native.push('confirm'); return true; };
window.alert = t => { window.__native.push('alert'); };
window.prompt = t => { window.__native.push('prompt'); return null; };
window.__asked = [];
window.__PC.uiConfirm = (t) => { window.__asked.push(String(t)); return Promise.resolve(true); };
window.__opened = [];
window.open = (u) => { window.__opened.push(String(u)); return null; };
window.__inst = 'https://poster.place';
window.__setInst = [];
window.pcShell = { getInstance: async () => window.__inst,
                   setInstance: async (u) => { window.__setInst.push(u); window.__inst = u; return true; } };
const S = window.__srv = { available: true, configured: false, venv: false, enabled: 'disabled',
  active: 'inactive', reachable: false, calls: [], jobDelay: 4000,
  job: { running: false, verb: '', rc: '', started: '', finished: '' },
  features: { ai: false, music: false, video: false, voice: false, searxng: false } };
const finish = (then) => setTimeout(() => { S.job.running = false; S.job.rc = '0';
  S.job.finished = String(Date.now()); then(); }, S.jobDelay);
window.pcServer = {
  status: async () => S.available === false ? { available: false, reason: 'not on this machine' } : ({
    available: true, code: true, configured: S.configured, venv: S.venv, enabled: S.enabled,
    active: S.active, port: 3051, relayPort: 3052, nostrOnly: !S.features.ai,
    postgres: { slot: '18', active: S.configured ? 'active' : 'inactive' },
    features: Object.assign({}, S.features), job: Object.assign({}, S.job), reachable: S.reachable,
    localUrl: 'http://127.0.0.1:3051', adminUrl: 'http://127.0.0.1:3051/admin',
    relayUrl: 'ws://127.0.0.1:3052', lanUrls: ['http://192.168.1.5:3051'],
    lanRelayUrls: ['ws://192.168.1.5:3052'] }),
  enable: async () => { S.calls.push('enable');
    S.job = { running: true, verb: 'enable', rc: '', started: '1', finished: '' };
    finish(() => { S.configured = true; S.venv = true; S.enabled = 'enabled'; S.active = 'active'; S.reachable = true; });
    return 'started: enable'; },
  disable: async () => { S.calls.push('disable'); S.enabled = 'disabled'; S.active = 'inactive'; S.reachable = false; return 'ok'; },
  restart: async () => { S.calls.push('restart'); return 'restarted'; },
  logs: async () => { S.calls.push('logs'); return 'Sep 21 posterchanai[1]: journal line A\nSep 21 posterchanai[1]: journal line B'; },
  job: async () => ({ running: S.job.running, verb: S.job.verb, rc: S.job.rc }),
  jobLog: async () => S.job.verb ? ('== pc-server ' + S.job.verb + '\ninstaller output line\n'
                                    + (S.job.running ? '' : '== finished rc=0')) : '',
  installAi: async (f) => { S.calls.push('install-ai ' + f);
    S.job = { running: true, verb: 'install-ai ' + f, rc: '', started: '1', finished: '' };
    finish(() => { S.features[f] = true; });
    return 'started'; },
};
</script>
<script src="/static/js/client/os.js"></script>"""

DRIVE = r"""(async () => {
  const out = {}, sleep = ms => new Promise(r => setTimeout(r, ms));
  const $ = s => document.querySelector(s);
  const until = async (fn, ms) => { const t = Date.now() + (ms || 8000);
    while (Date.now() < t) { try { if (fn()) return true; } catch (_) {} await sleep(100); } return false; };
  const text = s => ($(s) || {}).textContent || '';
  const feat = id => { const b = $('[data-srv-install="' + id + '"]');
    return b ? (b.disabled ? 'disabled' : 'enabled') : (text('[data-srv-features]').includes('Installed') ? 'installed?' : 'missing'); };
  PCOS.enter(); await sleep(150);
  PCOS.openSystemSettings(); await sleep(300);
  const nav = $('.os-set-nav [data-page="server"]');
  out.nav = !!nav;
  out.mobileOption = !!$('[data-settings-mobile] option[value="page:server"]');
  if (!nav) return out;
  nav.click();
  await until(() => !text('[data-srv-summary]').includes('Checking'));
  const pane = $('[data-settings-page="server"]');
  out.paneVisible = !!pane && !pane.hidden && document.querySelectorAll('[data-settings-page]:not([hidden])').length === 1;
  out.fresh = { summary: text('[data-srv-summary]'), enable: $('[data-srv-enable]').disabled,
    enableLabel: text('[data-srv-enable]'), admin: $('[data-srv-admin]').disabled,
    disable: $('[data-srv-disable]').disabled, logs: $('[data-srv-logs]').disabled,
    ai: feat('ai'), music: feat('music'), instanceHidden: $('[data-srv-instance-row]').hidden };

  $('[data-srv-enable]').click();
  await until(() => window.__srv.calls.includes('enable'), 3000);
  out.enableAsked = window.__asked.slice(-1)[0] || '';
  await until(() => !$('[data-srv-job]').hidden && text('[data-srv-job]').includes('installer output'), 4000);
  out.duringJob = { summary: text('[data-srv-summary]'), log: text('[data-srv-job]'),
    enable: $('[data-srv-enable]').disabled, ai: feat('ai') };
  await until(() => text('[data-srv-summary]').startsWith('Running'), 8000);
  out.running = { summary: text('[data-srv-summary]'), admin: $('[data-srv-admin]').disabled,
    enable: $('[data-srv-enable]').disabled, restart: $('[data-srv-restart]').disabled,
    finishedLog: text('[data-srv-job]'), facts: text('[data-srv-facts]'),
    enableCalls: window.__srv.calls.filter(c => c === 'enable').length };
  $('[data-srv-admin]').click(); await sleep(50);
  out.opened = window.__opened.slice();

  await until(() => !$('[data-srv-instance-row]').hidden, 3000);
  out.instance = { note: text('[data-srv-instance-note]'), label: text('[data-srv-instance]'), setBefore: window.__setInst.length };
  $('[data-srv-instance]').click();
  await until(() => window.__setInst.length === 1, 3000);
  let prev = null; try { prev = localStorage.getItem('pc_server_prev_instance'); } catch (_) {}
  out.instanceSet = { set: window.__setInst.slice(), prev };
  $('[data-srv-refresh]').click();
  await until(() => text('[data-srv-instance]') === 'Stop using it', 3000);
  out.instanceLocalLabel = text('[data-srv-instance]');
  $('[data-srv-instance]').click();
  await until(() => window.__setInst.length === 2, 3000);
  out.instanceRestored = window.__setInst.slice();

  out.beforeAi = { ai: feat('ai'), music: feat('music'), searxng: feat('searxng') };
  $('[data-srv-install="ai"]').click();
  await until(() => window.__srv.calls.includes('install-ai ai'), 3000);
  out.aiAsked = window.__asked.slice(-1)[0] || '';
  await until(() => window.__srv.features.ai && !!$('[data-srv-install="music"]') && !$('[data-srv-install="music"]').disabled, 8000);
  out.afterAi = { music: feat('music'), aiRow: !$('[data-srv-install="ai"]'), log: text('[data-srv-job]') };

  $('[data-srv-logs]').click();
  await until(() => !$('[data-srv-journal]').hidden, 3000);
  out.journal = text('[data-srv-journal]');

  const asked = window.__asked.length;
  $('[data-srv-disable]').click();
  await until(() => window.__srv.calls.includes('disable'), 3000);
  out.disableAsked = window.__asked.length > asked;
  await until(() => !$('[data-srv-enable]').disabled, 3000);
  out.afterDisable = { enable: $('[data-srv-enable]').disabled, label: text('[data-srv-enable]'),
    instanceHidden: $('[data-srv-instance-row]').hidden };

  /* Nothing on the page may reach past the card at the narrowest desktop width. */
  const card = $('[data-pcserver]');
  out.overflow = card ? card.scrollWidth - card.clientWidth : -1;

  window.__srv.available = false;
  $('[data-srv-refresh]').click();
  await until(() => text('[data-srv-summary]').includes('not on this machine'), 3000);
  out.unavailable = { summary: text('[data-srv-summary]'),
    anyEnabled: [...document.querySelectorAll('[data-pcserver] button:not([data-srv-refresh])')].some(b => !b.disabled),
    features: document.querySelectorAll('[data-srv-install]').length };
  out.native = window.__native.slice();
  return out;
})()"""


def verdict(o):
    p = []
    if not o or not o.get("nav"):
        return ["the System Settings nav has no PosterChan Server page (window.pcServer is present)"]
    if not o.get("mobileOption"):
        p.append("the narrow-layout category <select> has no PosterChan Server option")
    if not o.get("paneVisible"):
        p.append("clicking the category did not show exactly the server page")
    f = o.get("fresh", {})
    if "Not set up" not in f.get("summary", ""):
        p.append(f"a never-installed server must say 'Not set up', said {f.get('summary')!r}")
    if f.get("enable") or f.get("enableLabel") != "Enable server":
        p.append(f"Enable must be the one live action before setup: {f}")
    if not (f.get("admin") and f.get("disable") and f.get("logs")):
        p.append(f"admin/disable/logs are live on a server that does not exist: {f}")
    if f.get("ai") != "disabled" or f.get("music") != "disabled":
        p.append(f"AI installs must be refused until the server is set up: {f}")
    if not f.get("instanceHidden"):
        p.append("'use this server' is offered before there is a server")
    if "130 MB" not in o.get("enableAsked", "") or "3051" not in o.get("enableAsked", ""):
        p.append(f"Enable did not ask first with the size and ports: {o.get('enableAsked')!r}")
    d = o.get("duringJob", {})
    if "installer output" not in d.get("log", "") or not d.get("enable") or d.get("ai") != "disabled":
        p.append(f"while the setup job runs its log must show and every action wait: {d}")
    r = o.get("running", {})
    if not r.get("summary", "").startswith("Running") or r.get("admin") or not r.get("enable") or r.get("restart"):
        p.append(f"after setup the page must read Running with admin/restart live: {r}")
    if "finished rc=0" not in r.get("finishedLog", ""):
        p.append("the end of the job log (the answer to 'did it work') was never shown")
    if "ws://127.0.0.1:3052" not in r.get("facts", "") or "192.168.1.5:3051/client" not in r.get("facts", ""):
        p.append(f"the facts must name the client and relay addresses: {r.get('facts')!r}")
    if r.get("enableCalls") != 1:
        p.append(f"the helper's enable was called {r.get('enableCalls')} times")
    if o.get("opened") != ["http://127.0.0.1:3051/admin"]:
        p.append(f"Open admin panel opened {o.get('opened')}")
    i = o.get("instance", {})
    if i.get("setBefore") != 0 or i.get("label") != "Use this server in this app":
        p.append(f"the instance switch must be off until asked: {i}")
    s = o.get("instanceSet", {})
    if s.get("set") != ["http://127.0.0.1:3051"] or s.get("prev") != "https://poster.place":
        p.append(f"using the local server must set 127.0.0.1:3051 and remember the old instance: {s}")
    if o.get("instanceLocalLabel") != "Stop using it":
        p.append(f"a client already on the local server is offered {o.get('instanceLocalLabel')!r}")
    if o.get("instanceRestored") != ["http://127.0.0.1:3051", "https://poster.place"]:
        p.append(f"'Stop using it' did not restore the previous instance: {o.get('instanceRestored')}")
    b = o.get("beforeAi", {})
    if b.get("ai") != "enabled" or b.get("music") != "disabled" or b.get("searxng") != "enabled":
        p.append(f"with the server up: AI/search installable, music waits for AI — got {b}")
    if "hour" not in o.get("aiAsked", ""):
        p.append(f"installing AI must say how long it takes before it starts: {o.get('aiAsked')!r}")
    a = o.get("afterAi", {})
    if a.get("music") != "enabled" or not a.get("aiRow"):
        p.append(f"after AI is installed music must open up and AI read Installed: {a}")
    if "journal line B" not in o.get("journal", ""):
        p.append("View logs did not show the journal")
    if not o.get("disableAsked"):
        p.append("Disable did not ask first")
    ad = o.get("afterDisable", {})
    if ad.get("enable") or ad.get("label") != "Start server" or not ad.get("instanceHidden"):
        p.append(f"after Disable: Start must be offered and the instance switch hidden: {ad}")
    if o.get("overflow", 0) > 1:
        p.append(f"the server card overflows horizontally by {o.get('overflow')}px")
    u = o.get("unavailable", {})
    if "not on this machine" not in u.get("summary", "") or u.get("anyEnabled") or u.get("features"):
        p.append(f"a machine without the server must say so and offer nothing: {u}")
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
    print("OK  System Settings → PosterChan Server: setup, run, instance switch, AI installs, logs, off")
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
    tmp = tempfile.mkdtemp(prefix="osserver-")
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
