#!/usr/bin/env python3
"""The Terminal is a TERMINAL: most of the window is shell, and opening it gives you a prompt here.

    venv-unified/bin/python scripts/check_terminal_ui.py

Reported as "The Terminal needs a UI redo. waste of space, Should always start in a new local
terminal so it's usable." Measured on the laptop before the redesign: a page heading, a host
<select> row with six text buttons, a full-width red "Could not refresh hosts; retrying…" line, a
separate TABS row and a desktop key bar — the shell got what was left. And opening it reattached
whatever this browser tab last had, which was as often as not an SSH session on another machine.

This drives the SHIPPED term.js + client.css in a real Chrome, with the desktop's local PTY bridge
(`window.pcTerm`, desktop/preload.js) STUBBED the way preload injects it and a fake SSH server, and
asserts, at 1280x800 and in a 900x600 popped-out desktop window (html.pc-oswin + its 38px title
bar):

  space            the terminal area (.tty-screen) is >= 85% of the window height.
  one-bar          exactly ONE visible bar sits above it (the page heading is hidden too).
  new-local        opening Terminal calls the local bridge's start() — a NEW local shell — even
                   with a remembered remote session and an existing local one to "come back" to,
                   and opens NO remote connection.
  plus-menu        the + button opens a menu offering Local and every saved SSH host, and choosing
                   an SSH host opens a remote session on that host.
  keys-desktop     the esc/tab/ctrl key bar is hidden on a desktop with a physical keyboard,
  keys-phone       and shown at phone width, where it is the only Ctrl-C there is.
  status-inline    a host-list failure is a short status ON the bar, not a line of its own.

Reads PC_CHECK_PORT / PC_CHECK_PROFILE (checks run concurrently). Exit 0 pass, 1 fail, 2 could-not-run.
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
PORT = int(os.environ.get("PC_CHECK_PORT") or 9531)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-terminal-ui-check"

PAGE = r"""<!doctype html><html class="__HTMLCLS__"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/vendor/xterm/xterm.css">
<link rel="stylesheet" href="/static/css/client.css">
</head><body class="term-view">
__CHROME__
<div class="app" id="app"><aside class="sidebar glass"></aside><main class="main">
  <header class="topbar glass"><h2 id="view-title">Terminal</h2></header>
  <div id="feed" class="feed feed-term"></div>
</main></div>
<script src="/static/vendor/xterm/xterm.js"></script>
<script src="/static/vendor/xterm/fit.js"></script>
<script>
window.__PC_API_BASE__ = 'https://instance.test';
window.__PC_TOKEN__ = 'tok';
const Q = new URLSearchParams(location.search);
/* WHAT THIS BROWSER TAB "LEFT": a remote SSH session. The old code reattached it on open. */
try{ sessionStorage.setItem('pc_tty_sid:https://instance.test', 'r-remembered'); }catch(_){}
window.__log = { wsOpened: [], start: 0, attach: [], localWrites: 0 };
class FakeWS {
  constructor(url){ this.readyState = 0;
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 5); }
  send(raw){
    const m = JSON.parse(raw);
    if(m.t !== 'open') return;
    __log.wsOpened.push({ resume: m.resume || '', host: m.host || '', label: m.label || '' });
    const sid = m.resume || ('r' + __log.wsOpened.length);
    setTimeout(() => this.onmessage && this.onmessage({ data: JSON.stringify(
      { t:'ready', sid, host: m.host || 'server1', label: m.label || 'main', resumed: !!m.resume }) }), 2);
  }
  close(){ this.readyState = 3; }
}
window.WebSocket = FakeWS;
/* THE DESKTOP'S LOCAL PTY, stubbed with the shape desktop/preload.js exposes as window.pcTerm. One
   shell is already running — the one the old code "came back to". */
(function(){
  const shells = new Map([['1', { seq: 0 }]]); let next = 2; const subs = new Set();
  window.pcTerm = {
    start: async (o) => { __log.start++; const id = String(next++); shells.set(id, { seq: 0 });
      setTimeout(() => { const s = shells.get(id); const d = 'user@box:~$ '; s.seq += d.length;
        subs.forEach(f => f({ id, t:'out', d, seq: s.seq })); }, 20);
      return { id }; },
    write: async (id, d) => { __log.localWrites++; },
    resize: async () => true,
    backlog: async (id, since) => shells.has(String(id)) ? { d:'', seq: shells.get(String(id)).seq, alive:true } : null,
    close: async (id) => { shells.delete(String(id)); },
    list: async () => [...shells.keys()].map(id => ({ id, alive:true, idle:5 })),
    attach: async (id) => { __log.attach.push(String(id)); return true; },
    detach: async () => true,
    onData: (fn) => { subs.add(fn); return () => subs.delete(fn); },
  };
  if(Q.get('nolocal')) delete window.pcTerm;
})();
const $ = (s, r) => (r||document).querySelector(s);
const $$ = (s, r) => Array.from((r||document).querySelectorAll(s));
window.__PC = {
  $, $$,
  enc: (s) => String(s==null?'':s).replace(/[&<>"']/g,
        c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  toast: () => {}, publish: async () => {}, ensureAiSession: async () => {},
  uiPrompt: async () => '', uiConfirm: async () => true, isView: (v) => v === 'terminal',
  viewer: () => ({ pubkey: 'a'.repeat(64) }),
  authFetch: async (u) => {
    if(u.indexOf('/api/ssh/hosts') === 0){
      if(Q.get('hostfail')) return { ok:false, status:503, json: async () => ({}) };
      return { ok:true, status:200, json: async () => ({ ok:true, available:true, hosts:[
        { name:'server1', label:'me@server1.lan', keyed:true },
        { name:'nas.lan', label:'me@nas.lan', keyed:true }]}) };
    }
    if(u.indexOf('/api/ssh/sessions') === 0)
      return { ok:true, status:200, json: async () => ({ ok:true, sessions:[
        { sid:'r-remembered', host:'server1', label:'main', age:60, idle:0 }]}) };
    return { ok:true, status:200, json: async () => ({}) };
  },
};
</script>
<script src="/static/js/client/term.js"></script>
<script>
(async () => {
  for(let i = 0; i < 100 && !window.PCTerm; i++) await new Promise(r => setTimeout(r, 20));
  await PCTerm.render();
  await new Promise(r => setTimeout(r, 700));
  window.__ready = true;
})();
</script></body></html>"""

CHROME = ('<div id="pc-oswin-chrome"><span class="pc-oswin-title">PosterChan Window — terminal</span>'
          '<span class="pc-oswin-buttons"><button>–</button><button>□</button><button>×</button></span></div>')

MEASURE = r"""(() => {
  const vis = el => { if(!el) return false; const cs = getComputedStyle(el), r = el.getBoundingClientRect();
    return cs.display !== 'none' && cs.visibility !== 'hidden' && r.height > 0 && r.width > 0; };
  const scr = document.querySelector('.tty-screen'), wrap = document.querySelector('.tty-wrap');
  const sr = scr ? scr.getBoundingClientRect() : {top:0,height:0};
  const H = window.innerHeight;
  // Every visible bar between the top of the window content and the terminal area.
  const above = [];
  const top = document.querySelector('.topbar');
  if(vis(top)) above.push('page heading (.topbar)');
  if(wrap) for(const el of wrap.children){
    if(el === scr) break;
    if(vis(el) && el.getBoundingClientRect().height > 2) above.push(el.className || el.id || el.tagName);
  }
  const keys = document.querySelector('#tty-keys');
  const st = document.querySelector('#tty-state');
  const bar = document.querySelector('.tty-bar');
  return { H, screenH: Math.round(sr.height), share: sr.height / H, above,
           xterm: !!document.querySelector('#tty-screen .xterm'),
           keysVisible: vis(keys), keysHidden: keys ? keys.hidden : null,
           state: st ? { text: st.textContent, h: Math.round(st.getBoundingClientRect().height),
                         inBar: !!(bar && bar.contains(st)), vis: vis(st) } : null,
           barH: bar ? Math.round(bar.getBoundingClientRect().height) : 0,
           log: window.__log, tabs: [...document.querySelectorAll('#tty-sessions [data-tab]')]
             .map(t => ({ sid: t.dataset.tab, active: t.classList.contains('active'),
                          name: (t.querySelector('b')||{}).textContent || '' })),
           sid: window.PCTerm && PCTerm.sessionId() };
})()"""

PLUS = r"""(async () => {
  const plus = document.querySelector('#tty-tab-new');
  if(!plus) return { err: 'no + button' };
  plus.click();
  await new Promise(r => setTimeout(r, 150));
  const m = document.querySelector('#tty-new-menu');
  const shown = !!m && !m.hidden && m.getBoundingClientRect().height > 0;
  const items = m ? [...m.querySelectorAll('[data-new]')].map(b => b.dataset.new) : [];
  const before = window.__log.wsOpened.length;
  const ssh = m && m.querySelector('[data-new="server1"]');
  if(ssh) ssh.click();
  await new Promise(r => setTimeout(r, 700));
  const opened = window.__log.wsOpened.slice(before);
  return { shown, items, opened, closed: !m || m.hidden, sid: PCTerm.sessionId() };
})()"""


async def drive(base):
    import websockets
    shutil.rmtree(PROFILE, ignore_errors=True)
    chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
              or shutil.which("chromium") or shutil.which("chromium-browser"))
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    page = None
    try:
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
        problems = []
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        return msg.get("result") or {}

            async def js(expr, awaited=False):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True, "awaitPromise": awaited})
                if r.get("exceptionDetails"):
                    print("  js error: " + json.dumps(r["exceptionDetails"])[:600])
                    return None
                return (r.get("result") or {}).get("value")

            async def load(path, w, h, mobile=False):
                await call("Emulation.setDeviceMetricsOverride",
                           {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": mobile})
                await call("Page.navigate", {"url": base + path})
                for _ in range(80):
                    if await js("!!window.__ready"):
                        return True
                    await asyncio.sleep(0.25)
                return False

            await call("Page.enable")
            await call("Runtime.enable")

            cases = [("1280x800 page", "/page.html", 1280, 800, False),
                     ("900x600 desktop window", "/window.html", 900, 600, False)]
            for tag, path, w, h, mob in cases:
                if not await load(path, w, h, mob):
                    print(f"SKIP  [{tag}] the terminal never mounted")
                    return 2
                q = await js(MEASURE)
                if not q:
                    print(f"SKIP  [{tag}] no measurement")
                    return 2
                shots = os.environ.get("PC_TERM_UI_SHOTS")   # a directory: save what was measured
                if shots:
                    import base64
                    img = await call("Page.captureScreenshot", {"format": "png"})
                    with open(os.path.join(shots, f"terminal-{w}x{h}.png"), "wb") as fh:
                        fh.write(base64.b64decode(img.get("data", "")))
                log = q["log"]
                print(f"[{tag}] terminal {q['screenH']}/{q['H']}px = {q['share']*100:.1f}% · "
                      f"bars above: {q['above']} · bar {q['barH']}px · keys visible={q['keysVisible']} · "
                      f"start()={log['start']} ws={len(log['wsOpened'])} attach={log['attach']} · tabs="
                      + ", ".join(t['name'] + ('*' if t['active'] else '') for t in q["tabs"]))
                if not q["xterm"]:
                    problems.append((tag, "no-xterm", "the emulator never mounted"))
                if q["share"] < 0.85:
                    problems.append((tag, "space", f"the terminal is {q['share']*100:.1f}% of the "
                                                   "window height; it must be at least 85%"))
                if len(q["above"]) != 1:
                    problems.append((tag, "one-bar", f"{len(q['above'])} bars above the terminal: "
                                                     f"{q['above']}"))
                if log["start"] != 1:
                    problems.append((tag, "new-local", f"the local bridge's start() ran {log['start']} "
                                                       "times on open; opening Terminal must start "
                                                       "exactly one NEW local shell"))
                if log["wsOpened"]:
                    problems.append((tag, "new-local", "opening Terminal opened a remote connection: "
                                                       + json.dumps(log["wsOpened"])))
                if not (q["sid"] or "").startswith("local:") or q["sid"] == "local:1":
                    problems.append((tag, "new-local", f"the shell on screen is {q['sid']!r}, not a "
                                                       "new local one"))
                if q["keysVisible"]:
                    problems.append((tag, "keys-desktop", "the phone key bar is showing on a desktop "
                                                          "with a physical keyboard"))

            # + menu: Local and every SSH host; choosing SSH opens that host.
            await load("/page.html", 1280, 800)
            p = await js(PLUS, awaited=True)
            print(f"[+ menu] shown={p and p.get('shown')} items={p and p.get('items')} "
                  f"opened={p and p.get('opened')}")
            if not p or p.get("err") or not p.get("shown"):
                problems.append(("+ menu", "plus-menu", "the + button opens no menu"))
            else:
                if "local" not in p["items"] or "server1" not in p["items"] or "nas.lan" not in p["items"]:
                    problems.append(("+ menu", "plus-menu", f"the menu offers {p['items']}, not Local "
                                                            "plus every SSH host"))
                if len(p["opened"]) != 1 or p["opened"][0]["host"] != "server1" or p["opened"][0]["resume"]:
                    problems.append(("+ menu", "plus-menu", "choosing server1 did not open a NEW "
                                                            f"session there: {p['opened']}"))
                elif p["opened"][0]["label"] == "main":
                    # `main` on server1 is already running (it is a tab): a new tab named `main`
                    # is `tmux new-session -A` onto THAT shell — see check_terminal_tabs.py.
                    problems.append(("+ menu", "plus-menu", "the new SSH tab took the label of a "
                                                            "session already running there"))
                if not p.get("closed"):
                    problems.append(("+ menu", "plus-menu", "the menu stayed open after a choice"))

            # Phone width: the key bar is the only Ctrl-C there is.
            await load("/page.html", 390, 800, True)
            q = await js(MEASURE)
            print(f"[390 phone] keys visible={q and q['keysVisible']} bar {q and q['barH']}px")
            if not q or not q["keysVisible"]:
                problems.append(("390 phone", "keys-phone", "the key bar is hidden at phone width"))

            # A failing host list is a word on the bar, not a line of its own.
            await load("/page.html?hostfail=1&nolocal=1", 1280, 800)
            await asyncio.sleep(0.3)
            q = await js(MEASURE)
            st = (q or {}).get("state") or {}
            print(f"[host list failing] status={st.get('text')!r} inBar={st.get('inBar')} "
                  f"h={st.get('h')} bars above={q and q['above']}")
            if not st.get("inBar") or (st.get("vis") and st.get("h", 0) > 22) or len(q["above"]) != 1:
                problems.append(("host fail", "status-inline", "a host-list failure takes its own "
                                                               "row instead of sitting on the bar"))

            # A plain browser: no local shell, and it SAYS so rather than silently showing SSH.
            await load("/page.html?nolocal=1", 1280, 800)
            said = await js("(document.querySelector('#tty-state')||{}).title + ' ' + "
                            "(document.querySelector('#tty-new-menu')||{}).textContent")
            print(f"[browser, no local PTY] says: {(said or '')[:110]!r}")
            if not said or "local shell" not in said.lower():
                problems.append(("browser", "no-local-why", "a browser with no local shell does not "
                                                            "say why there is none"))
        if problems:
            print()
            for tag, kind, why in problems:
                print(f"FAIL  [{tag}] {kind}: {why}")
            return 1
        print("\nOK  the terminal fills its window under one bar, opens a new local shell, offers "
              "every host from +, and keeps the key bar for phones")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(PROFILE, ignore_errors=True)


def main():
    import importlib.util
    if importlib.util.find_spec("websockets") is None:
        print("SKIP  websockets not installed")
        return 2
    for p in ("static/js/client/term.js", "static/css/client.css",
              "static/vendor/xterm/xterm.js", "static/vendor/xterm/fit.js"):
        if not os.path.exists(os.path.join(ROOT, p)):
            print(f"SKIP  {p} is missing — re-point this check")
            return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="termui-")
    with open(os.path.join(tmp, "page.html"), "w") as fh:
        fh.write(PAGE.replace("__HTMLCLS__", "").replace("__CHROME__", ""))
    with open(os.path.join(tmp, "window.html"), "w") as fh:
        fh.write(PAGE.replace("__HTMLCLS__", "pc-oswin").replace("__CHROME__", CHROME))

    class H(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            path = path.split("?")[0].split("#")[0]
            # Point at another build (an installed asar's copy, or the pre-fix file to prove this
            # check can fail) without touching the tree.
            if path == "/static/js/client/term.js" and os.environ.get("PC_TERM_JS"):
                return os.environ["PC_TERM_JS"]
            if path == "/static/css/client.css" and os.environ.get("PC_CLIENT_CSS"):
                return os.environ["PC_CLIENT_CSS"]
            if path.startswith("/static/"):
                return os.path.join(ROOT, path.lstrip("/"))
            return os.path.join(tmp, path.lstrip("/") or "page.html")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        return asyncio.run(drive(f"http://127.0.0.1:{srv.server_port}"))
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
