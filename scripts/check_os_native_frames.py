#!/usr/bin/env python3
"""PosterChanOS — a Linux app inside a PosterChan window, driven against a stub compositor.

    venv-unified/bin/python scripts/check_os_native_frames.py

`tests/test_os_native_windows.py` runs the ARITHMETIC (osnative.js) and it passes on every one of
the failures below, because none of them is arithmetic — they are in the driver in os.js, which is
the half that talks to the compositor. All three were found on a real Gentoo box with sway and all
three are silent: nothing throws, nothing logs, and the desktop keeps drawing perfectly.

The stub compositor here reproduces the ONE behaviour that makes them possible, measured on that
machine rather than assumed: **focusing a native window takes the keyboard away from the shell.**
`pcWM.focus(id)` → `document.hasFocus()` goes true → false and a `blur` event arrives ~1ms later.
Every desktop does this; it is what focus means. What it costs is below.

Assertions, each a way a framed app breaks:

  native-drag-dead    A native window cannot be dragged. Pressing its title bar focuses the app,
                      which BLURS this window, and `blur` is one of the ways a drag ends (alt-tabbed
                      away mid-drag must not leave a window glued to the cursor). So the gesture
                      ended on its first pointermove. Measured on the real machine before the fix: a
                      200px drag moved firefox 25px and stopped. Reported as "firefox can't be
                      moved" — with the frame, the title bar and the buttons all drawing correctly.

  native-resize-dead  The same thing through the resize grip, which arms the same blur guard.

  overlay-buried      An overlay of ours opens BEHIND a native app. The start menu, the notification
                      panel, the network panel, a tray popover and every modal are painted by this
                      page — and this page is a tiled compositor window that firefox floats above,
                      so no z-index here can reach across. `stashPlan` was fed the WINDOW list only,
                      and a menu is not a window, so nothing put the app away. Reported as "start
                      menu does not go over firefox": it opened, drew, took the keyboard, and was
                      invisible. Also fails if the app is not brought BACK when the overlay closes.

  native-app-killed   An app is closed because its window momentarily has no title. `taskbarRows`
                      drops an untitled window on purpose — a nameless button that renames itself a
                      second later is worse than one that arrives late — but the same list was used
                      to decide what still EXISTS, and closing a native frame closes the app. A
                      browser that clears its title for an instant was killed for it.

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
# Its OWN port and profile, never shared: the checks run concurrently and four scripts once shared
# 9473, which reads as a flaky check rather than as two browsers fighting over one debugger.
PORT = int(os.environ.get("PC_CHECK_PORT") or 9491)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-osnative-check"

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/css/client.css">
</head><body>
<div class="app" style="display:flex;height:100dvh">
  <aside class="sidebar glass">
    <div class="brand"><img src="/static/posterchan-relay.png" class="brand-logo" alt="PosterChan"></div>
    <nav class="nav">
      <button class="nav-item" data-view="global"><svg class="ic"><use href="#i-globe"></use></svg><span>Social</span></button>
      <button class="nav-item" data-view="notes"><svg class="ic"><use href="#i-note"></use></svg><span>Notes</span></button>
    </nav>
  </aside>
  <div class="main"><div id="feed" class="feed">CLASSIC</div></div>
</div>
<script src="/static/js/client/sprite.js"></script>
<script>
window.__PC = {
  toast: m => (window.__toasts = window.__toasts || []).push(m),
  get VIEW(){ return window.__view || 'global'; },
  switchView: (v, quiet) => { window.__view = v;
    if (quiet) return;
    const f = document.getElementById('feed');
    if (f) f.innerHTML = '<div class="stub-view">' + v + '</div>';
  },
};
window.Store = { query: () => [] };
window.Relay = { conns: () => [], watch: () => () => {}, wake: () => {},
                 query: () => Promise.resolve([]), subscribe: () => 1, close: () => {} };

/* ── THE STUB COMPOSITOR ────────────────────────────────────────────────────────────────────────
 *
 * sway, as os.js reaches it, with the one behaviour that matters reproduced rather than assumed:
 * focusing a window hands it the keyboard, which means THIS window loses it. Measured on the real
 * machine (a Ryzen laptop running sway): pcWM.focus(41) → hasFocus() true → false, one `blur`
 * event, +1ms. Without that in the stub, every assertion below passes against broken code.
 */
window.__wm = {
  wins: [
    { id: 1, pid: 100, app: 'posterchan-desktop', title: 'PosterChan', workspace: '1',
      focused: false, floating: false, xwayland: false,
      rect: { x: 0, y: 0, width: 1600, height: 900 } },
    { id: 2, pid: 200, app: 'firefox', title: 'Mozilla Firefox', workspace: '1',
      focused: false, floating: true, xwayland: false,
      rect: { x: 300, y: 100, width: 700, height: 500 } },
  ],
  calls: [],
  hidden: {},
};
const wmLog = (what, id, extra) => window.__wm.calls.push(
  Object.assign({ what, id, at: Date.now() }, extra || {}));

window.pcWM = {
  windows: () => Promise.resolve(window.__wm.wins.map(w => Object.assign({}, w,
                                   { rect: Object.assign({}, w.rect) }))),
  focus: (id) => {
    wmLog('focus', id);
    /* THE MEASURED BEHAVIOUR. A native window taking the keyboard blurs this one — and a drag
     * listens for exactly that. Only for a window that is not ours, the same as the real thing.
     *
     * ASYNCHRONOUS, and that is the whole faithfulness of this stub. On the real machine the blur
     * arrives ~1ms after the focus call, which is AFTER `startDrag` has registered its listener for
     * it. Dispatched synchronously it lands before that listener exists, the drag survives, and
     * this check passes against the very code it was written to fail on — which it did, once. */
    if (Number(id) !== 1) setTimeout(() => { try { window.dispatchEvent(new Event('blur')); } catch (_) {} }, 1);
    return Promise.resolve([]);
  },
  hide: (id) => { wmLog('hide', id); window.__wm.hidden[id] = true; return Promise.resolve([]); },
  show: (id) => { wmLog('show', id); window.__wm.hidden[id] = false; return Promise.resolve([]); },
  place: (id, x, y, w, h) => {
    wmLog('place', id, { x, y, w, h });
    const win = window.__wm.wins.find(v => Number(v.id) === Number(id));
    if (win) win.rect = { x, y, width: w, height: h };
    return Promise.resolve([]);
  },
  close: (id) => {
    wmLog('close', id);
    window.__wm.wins = window.__wm.wins.filter(v => Number(v.id) !== Number(id));
    return Promise.resolve([]);
  },
  launch: (argv, opts) => { window.__lastArgv = argv; window.__lastOpts = opts;
                            return Promise.resolve({ pid: 0, window: null }); },
  subscribe: () => Promise.resolve(),
  /* A REAL SUBSCRIPTION, so the reconcile is reached the way it is reached in the app — off a
   * compositor event — rather than through a hook that only a test calls. A window changing its
   * title IS a `window` event, and what the shell does with one is the thing being checked. */
  onEvent: (fn) => { window.__wmSubs.add(fn); return () => window.__wmSubs.delete(fn); },
};
window.__wmSubs = new Set();
window.__wmFire = () => { for (const fn of window.__wmSubs) { try { fn({}); } catch (_) {} } };
window.pcNet   = { status: () => Promise.resolve({ online: true, kind: 'wifi', name: 'Net', signal: 70 }),
                   wifi: () => Promise.resolve([]), connect: () => Promise.resolve({ ok: true }) };
window.pcPower = { status: () => Promise.resolve({ battery: { present: true, percent: 80, charging: false },
                                                   brightness: { available: true, percent: 50 },
                                                   profiles: ['balanced'], profile: 'balanced',
                                                   canHibernate: false }),
                   setBrightness: () => Promise.resolve({ ok: true }) };
window.pcAudio = { status: () => Promise.resolve({ output: { percent: 40, muted: false } }),
                   setVolume: () => Promise.resolve({ ok: true }) };
window.pcOS    = { provision: () => Promise.resolve({ ok: true }) };
/* The machine's installed programs, as the scan answers. Two entries the built-in list does NOT
 * already name, plus one it does — the launcher must show the first two and not offer firefox
 * twice under two names. */
window.pcApps  = { list: () => Promise.resolve({ apps: [
  { id: 'btop', name: 'btop', match: 'btop', argv: ['/usr/bin/foot', '-e', '/usr/bin/btop'], group: 'System' },
  { id: 'org.qbittorrent.qBittorrent', name: 'qBittorrent', match: 'qbittorrent',
    argv: ['/usr/bin/qbittorrent'], group: 'Internet' },
  { id: 'firefox-bin', name: 'Firefox', match: 'firefox', argv: ['/usr/bin/firefox-bin'] } ] }) };
</script>
<script src="/static/js/client/osfirstrun.js"></script>
<script src="/static/js/client/osshell.js"></script>
<script src="/static/js/client/osnative.js"></script>
<script src="/static/js/client/os.js"></script>
<script>window.__ready = true;</script>
</body></html>"""


DRIVE = r"""(async () => {
  const out = { problems: [] };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const bad = (k, d) => out.problems.push({ k, d });

  await window.PCOSShell.detect();
  PCOS.enter();
  await sleep(400);
  // adoptAll runs off the compositor watcher; give it a nudge so the check does not depend on when
  // the stub's first event happens to land.
  await sleep(600);

  const frame = () => document.querySelector('.osw.osw-native');
  const bodyRect = () => { const f = frame(); if (!f) return null;
    const r = f.querySelector('.osw-body').getBoundingClientRect();
    return { l: Math.round(r.left), t: Math.round(r.top),
             w: Math.round(r.width), h: Math.round(r.height) }; };

  if (!frame()) { bad('native-not-framed', 'the compositor window was never given a window here');
                  PCOS.exit(); return out; }
  out.framed = true;

  /* ── the drag ──────────────────────────────────────────────────────────────────────────────── */
  const mk = (t, x, y, btn) => new PointerEvent(t, { bubbles: true, cancelable: true, composed: true,
    clientX: x, clientY: y, pointerId: 1, pointerType: 'mouse', isPrimary: true,
    buttons: btn, button: 0 });

  async function gesture(handleSel, dx, dy) {
    const f = frame();
    const h = f.querySelector(handleSel);
    const hr = h.getBoundingClientRect();
    const sx = Math.round(hr.left + hr.width / 2), sy = Math.round(hr.top + hr.height / 2);
    const before = bodyRect();
    h.dispatchEvent(mk('pointerdown', sx, sy, 1));
    const steps = 8;
    for (let i = 1; i <= steps; i++) {
      document.dispatchEvent(mk('pointermove', sx + Math.round(dx * i / steps),
                                               sy + Math.round(dy * i / steps), 1));
      await sleep(25);
    }
    document.dispatchEvent(mk('pointerup', sx + dx, sy + dy, 0));
    await sleep(500);
    return { before, after: bodyRect() };
  }

  {
    window.__blurs = 0;
    window.addEventListener('blur', () => { window.__blurs++; });
    const g = await gesture('.osw-bar', 200, 96);
    out.blurs = window.__blurs;
    out.calls = window.__wm.calls.map(c => c.what + ':' + c.id).slice(0, 12);
    out.drag = { moved: [g.after.l - g.before.l, g.after.t - g.before.t], want: [200, 96] };
    // Generous: the drag is clamped to the desktop and the last step rounds. Anything under half the
    // gesture is the blur ending it, which is the failure this exists to catch.
    if (Math.abs(g.after.l - g.before.l) < 150 || Math.abs(g.after.t - g.before.t) < 70)
      bad('native-drag-dead', 'a ' + [200, 96] + ' drag of a native window moved it ' + out.drag.moved
                            + ' — focusing the app blurred this window and ended the gesture');
  }

  {
    const g = await gesture('.osw-grip', 120, 90);
    out.resize = { grew: [g.after.w - g.before.w, g.after.h - g.before.h], want: [120, 90] };
    if (Math.abs(g.after.w - g.before.w) < 90 || Math.abs(g.after.h - g.before.h) < 65)
      bad('native-resize-dead', 'a ' + [120, 90] + ' resize of a native window grew it '
                              + out.resize.grew + ' — the same blur, through the grip');
  }

  /* The surface must have followed the frame — the whole point of the arithmetic above it. */
  {
    await sleep(400);
    const b = bodyRect();
    const w = window.__wm.wins.find(v => v.app === 'firefox');
    out.placed = w && w.rect;
    if (!w) bad('native-app-killed', 'the app is gone from the compositor after a gesture');
    else if (Math.abs(w.rect.x - b.l) > 2 || Math.abs(w.rect.y - b.t) > 2
          || Math.abs(w.rect.width - b.w) > 2 || Math.abs(w.rect.height - b.h) > 2)
      bad('native-not-followed', 'the frame is at ' + JSON.stringify(b)
                               + ' and the app at ' + JSON.stringify(w.rect));
  }

  /* ── an overlay of ours must put the app away ──────────────────────────────────────────────── */
  {
    // Right over the frame, so this is about the RULE and not about where a menu happens to open.
    const b = bodyRect();
    const ov = document.createElement('div');
    ov.className = 'os-startmenu';
    ov.id = 'os-startmenu';
    ov.style.cssText = 'position:absolute;left:' + b.l + 'px;top:' + b.t + 'px;width:'
                     + Math.max(120, b.w - 20) + 'px;height:' + Math.max(120, b.h - 20) + 'px';
    document.getElementById('os-root').appendChild(ov);
    await sleep(500);
    out.hiddenForOverlay = !!window.__wm.hidden[2];
    if (!window.__wm.hidden[2])
      bad('overlay-buried', 'an overlay covering the app did not put it away — it is painted '
                          + 'underneath a compositor surface and cannot be seen');
    ov.remove();
    await sleep(500);
    out.shownAfterOverlay = !window.__wm.hidden[2];
    if (window.__wm.hidden[2])
      bad('overlay-buried', 'the app was not brought back when the overlay closed');
  }

  /* ── the start menu lists what is installed on this machine ────────────────────────────────── */
  {
    const list = await window.PCOSShell.allApps();
    const names = list.map(a => a.name);
    out.launcher = names;
    if (!names.includes('qBittorrent') || !names.includes('btop'))
      bad('installed-apps-missing', 'the launcher does not offer programs installed on this '
                                  + 'machine: ' + JSON.stringify(names));
    const fx = names.filter(n => /^(browser|firefox)$/i.test(n));
    if (fx.length !== 1)
      bad('app-listed-twice', 'firefox is in the launcher as ' + JSON.stringify(fx)
                            + ' — the built-in and the scanned entry are the same program');
    // …and it is startable with the argv its own .desktop file named.
    window.__wm.calls.length = 0;
    const before = window.__wm.wins.length;
    await window.PCOSShell.launch('app:btop').catch(() => {});
    out.launchedArgv = window.__lastArgv || null;
    if (!out.launchedArgv || out.launchedArgv[0] !== '/usr/bin/foot')
      bad('installed-apps-missing', 'starting an installed program did not use its own argv: '
                                  + JSON.stringify(out.launchedArgv));
    void before;
  }

  /* ── a window that clears its title is not a window that has closed ────────────────────────── */
  {
    const w = window.__wm.wins.find(v => v.app === 'firefox');
    w.title = '';
    window.__wmFire();
    await sleep(900);
    const killed = window.__wm.calls.some(c => c.what === 'close' && Number(c.id) === 2);
    out.titleClearedKilled = killed;
    if (killed)
      bad('native-app-killed', 'the app was closed because its window momentarily had no title');
    w.title = 'Mozilla Firefox';
  }

  PCOS.exit();
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
                       {"width": 1600, "height": 900, "deviceScaleFactor": 1, "mobile": False})
            await call("Page.navigate", {"url": url})
            ok = False
            for _ in range(80):
                await asyncio.sleep(0.25)
                r = await call("Runtime.evaluate",
                               {"expression": "window.__ready === true && !!window.PCOS",
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
                print("FAIL  the driver threw:",
                      json.dumps(r["exceptionDetails"])[:900])
                return 1
            out = r["result"].get("value") or {}

            for k in ("drag", "blurs", "calls", "resize", "placed", "hiddenForOverlay",
                      "shownAfterOverlay", "launcher", "launchedArgv", "titleClearedKilled"):
                if k in out:
                    print(f"  {k}: {json.dumps(out[k])}")
            problems = out.get("problems") or []
            if not problems:
                print("PASS  a Linux app is framed, dragged, resized, covered, listed and not killed")
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
    tmp = tempfile.mkdtemp(prefix="osnative-")
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
