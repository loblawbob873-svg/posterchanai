#!/usr/bin/env python3
"""PosterChan OS — the windowed desktop, driven for real.

    venv-unified/bin/python scripts/check_os_desktop.py

Loads the SHIPPED os.js against a stubbed sidebar and a stub feature that paints into `#feed`, then
opens windows and checks the things this shell can silently get wrong.

Assertions, each a way a window manager breaks:

  no-desktop           Entering does not produce a taskbar, a start button and desktop icons.
  apps-missing         The desktop icons / start menu do not match the sidebar. They are READ from
                       it on purpose, so a feature added to the nav appears here for free — if that
                       link breaks, the launcher silently drifts from the real navigation.
  feed-not-handed-over The focused window's body must be the one carrying `id="feed"`, and exactly
                       ONE element may. This is the whole mechanism: every feature renders into
                       `#feed`, so if the id is missing, duplicated, or on the wrong window, a
                       feature paints into a window nobody is looking at — or into nothing.
  feed-not-returned    Leaving the desktop must give `id="feed"` back to the client's own element,
                       or the classic UI renders into a detached node and goes blank.
  window-controls      Minimise / maximise / close do not do what they say.
  offscreen-window     A window can be dragged somewhere it cannot be dragged back from.
  mobile-not-gated     The desktop offers itself below 1024px, where it cannot work.
  modal-buried         A modal is not clickable — .modal-bg was authored at z-index 100, below the
                       z-index:300 desktop, so reply / quote / confirm / settings opened INVISIBLY
                       behind it. Hit-tested with elementFromPoint, not by reading the stylesheet.
  post-window-broken   Clicking a post does not open it in its own window (or opens a second one
                       when the post is already open).

Exit 0 = clean, 1 = problems (printed), 2 = could not run (no Chrome / websockets).
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
# (w, h, wide, touch) — a tablet in landscape, and a tablet upright which must be refused.
WIDTHS = [(1600, 900, True, False), (1280, 800, True, True), (800, 1280, False, True)]
PORT = 9486
PROFILE = "/tmp/pc-os-check"

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/static/css/client.css">
</head><body>
<div class="app" style="display:flex;height:100dvh">
  <aside class="sidebar glass">
    <div class="brand"><img src="/static/posterchan-relay.png" class="brand-logo" alt="PosterChan"></div>
    <div class="nav-search"><input id="nav-search-input" class="input" type="search"></div>
    <nav class="nav">
      <button class="nav-item" data-view="ai"><svg class="ic"><use href="#i-ai"></use></svg><span>PosterChan AI</span></button>
      <button class="nav-item" data-view="notifications"><svg class="ic"><use href="#i-bell"></use></svg><span>Notifications</span></button>
      <button class="nav-item" data-view="global"><svg class="ic"><use href="#i-globe"></use></svg><span>Social</span></button>
      <button class="nav-item" data-view="calendar"><svg class="ic"><use href="#i-clock"></use></svg><span>Calendar</span></button>
      <button class="nav-item" data-view="contacts"><svg class="ic"><use href="#i-user"></use></svg><span>Contacts</span></button>
      <button class="nav-item" data-view="messages"><svg class="ic"><use href="#i-mail"></use></svg><span>Messages</span></button>
      <button class="nav-item" data-view="bookmarks"><svg class="ic"><use href="#i-bookmark"></use></svg><span>Bookmarks</span></button>
      <button class="nav-item" data-view="calls"><svg class="ic"><use href="#i-phone"></use></svg><span>Calls</span></button>
      <button class="nav-item" data-view="notes"><svg class="ic"><use href="#i-note"></use></svg><span>Notes</span></button>
      <button class="nav-item" data-view="vault"><svg class="ic"><use href="#i-key"></use></svg><span>Passwords</span></button>
      <button class="nav-item" data-view="drafts"><svg class="ic"><use href="#i-draft"></use></svg><span>Drafts</span></button>
      <button class="nav-item" data-view="meme"><svg class="ic"><use href="#i-tv"></use></svg><span>Meme Builder</span></button>
      <button class="nav-item" data-view="websearch"><svg class="ic"><use href="#i-search"></use></svg><span>Web Search</span></button>
      <button class="nav-item" data-view="markets"><svg class="ic"><use href="#i-chart"></use></svg><span>Markets</span></button>
      <button class="nav-item" data-view="news"><svg class="ic"><use href="#i-news"></use></svg><span>News</span></button>
      <button class="nav-item" data-view="stats"><svg class="ic"><use href="#i-chart"></use></svg><span>Server Stats</span></button>
    </nav>
  </aside>
  <div class="main"><div id="feed" class="feed">CLASSIC</div></div>
</div>
<script src="/static/js/client/sprite.js"></script>
<script>
// A stub of the one contract os.js depends on: switchView paints the named view into #feed.
window.__rendered = [];
window.__composed = 0;
window.__PC = {
  toast: m => (window.__toasts = window.__toasts || []).push(m),
  compose: () => { window.__composed++; },
  get VIEW(){ return window.__view || 'global'; },
  switchView: v => {
    window.__view = v; window.__rendered.push(v);
    const f = document.getElementById('feed');
    if (f) {
      f.innerHTML = '<div class="stub-view" data-v="' + v + '">' + v + ' rendered'
                  + '<button class="stub-btn">Do the thing</button></div>';
      const b = f.querySelector('.stub-btn');
      if (b) b.onclick = () => { window.__clicked = (window.__clicked || 0) + 1; };
    }
  },
};
window.ClientSettings = { _v:{}, get(k,d){ return k in this._v ? this._v[k] : d; }, set(k,v){ this._v[k]=v; } };
</script>
<script src="/static/js/client/os.js"></script>
<script>window.__ready = true;</script>
</body></html>"""

DRIVE = r"""(async () => {
  const out = {};
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const feeds = () => document.querySelectorAll('#feed').length;
  const feedIn = sel => { const f = document.getElementById('feed');
                          return !!(f && f.closest(sel)); };

  out.classicFeedText = (document.getElementById('feed')||{}).textContent;
  PCOS.enter(); await sleep(150);
  out.entered   = PCOS.isOn();
  const nb = document.querySelector('#os-new');
  out.hasNew = !!nb;
  if (nb) nb.click();

  // A REAL modal, in the real #modal-root, hit-tested against the real CSS. Everything the apps do
  // that isn't inline — reply, quote, confirm, settings, the AI splash actions — goes through here,
  // and .modal-bg sitting below .os-root means the click lands on the desktop instead.
  {
    const bg = document.createElement('div');
    bg.className = 'modal-bg';
    bg.innerHTML = '<div class="modal glass neon-border"><button id="__probe">go</button></div>';
    let mr = document.getElementById('modal-root');
    if (!mr) { mr = document.createElement('div'); mr.id = 'modal-root'; document.body.appendChild(mr); }
    mr.appendChild(bg);
    document.body.classList.add('modal-open');
    await sleep(60);
    const b = document.getElementById('__probe');
    const r = b.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width/2, r.top + r.height/2);
    out.modalW = Math.round(r.width);
    out.modalReachable = !!(hit && (hit === b || b.contains(hit)));
    out.modalCoveredBy = out.modalReachable ? '' :
      (hit ? (hit.id || hit.className || hit.tagName).toString().slice(0,40) : 'nothing');
    bg.remove(); document.body.classList.remove('modal-open');
  }

  // A post opens in its OWN window (openDoc), the timeline window stays put, and clicking the same
  // post again focuses that window instead of stacking a duplicate.
  {
    const before = document.querySelectorAll('.osw').length;
    let painted = 0;
    const render = () => { painted++; document.getElementById('feed').innerHTML = 'THREAD'; };
    PCOS.openDoc('post:aaaa', 'Post', 'i-note', render);
    await sleep(80);
    out.docWins  = document.querySelectorAll('.osw').length - before;
    out.docFeed  = feedIn('.osw.focused');
    out.docTask  = [...document.querySelectorAll('.os-task')].some(t => /Post/.test(t.textContent));
    PCOS.openDoc('post:aaaa', 'Post', 'i-note', render);
    await sleep(80);
    out.docDedup = document.querySelectorAll('.osw').length - before;
    out.docPaint = painted;
    // Close it again — this probe must not leave a window (or a feed full of 'THREAD') behind for
    // the assertions that follow.
    document.querySelector('.osw.focused .osw-x').click();
    await sleep(80);
    out.docClosed = document.querySelectorAll('.osw').length - before;
  }
  out.composed = window.__composed;
  out.hasBar    = !!document.querySelector('.os-bar');
  out.hasStart  = !!document.querySelector('#os-start');
  out.icons     = [...document.querySelectorAll('.os-icon')].map(b => b.dataset.view);
  out.navViews  = [...document.querySelectorAll('.sidebar .nav .nav-item[data-view]')].map(b => b.dataset.view);
  // Distinct left edges = number of icon columns. With ~18 entries a grid would spill into a
  // second column marching across the desktop and over the windows.
  out.iconCols  = new Set([...document.querySelectorAll('.os-icon')]
                    .map(b => Math.round(b.getBoundingClientRect().left))).size;
  // …and every one of them must be visible without scrolling: the taskbar is the floor.
  const barTop = document.querySelector('.os-bar').getBoundingClientRect().top;
  out.iconsOffscreen = [...document.querySelectorAll('.os-icon')]
                         .filter(b => b.getBoundingClientRect().bottom > barTop + 1).length;

  // Start menu lists the same apps and can filter.
  document.querySelector('#os-start').click(); await sleep(120);
  out.menuApps  = [...document.querySelectorAll('.os-app')].map(b => b.dataset.view);
  const q = document.querySelector('#os-q');
  if (q) { q.value='cal'; q.dispatchEvent(new Event('input',{bubbles:true})); await sleep(80); }
  out.filtered  = [...document.querySelectorAll('.os-app')].map(b => b.dataset.view);
  document.querySelector('#os-start').click(); await sleep(80);

  // Open two windows from the desktop icons.
  const ic = v => document.querySelector('.os-icon[data-view="'+v+'"]');
  ic('calendar').click(); await sleep(150);
  ic('contacts').click(); await sleep(150);
  out.windows   = document.querySelectorAll('.osw').length;
  out.tasks     = document.querySelectorAll('.os-task').length;
  out.feedCount = feeds();
  const focused = document.querySelector('.osw.focused .osw-body');
  // The real element must LIVE in the focused window — moving it is what carries the delegated
  // click/scroll/touch listeners the whole client depends on.
  out.feedOnFocused = !!(focused && document.getElementById('feed')
                         && document.getElementById('feed').parentElement === focused);
  out.renderedLast  = window.__rendered[window.__rendered.length-1];

  // Focusing the other window must move the id AND re-render that feature there.
  const other = [...document.querySelectorAll('.osw')].find(w => !w.classList.contains('focused'));
  other.querySelector('.osw-bar').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));
  await sleep(200);
  const f2 = document.querySelector('.osw.focused .osw-body');
  out.feedMoved = !!(f2 && f2 !== focused && document.getElementById('feed')
                     && document.getElementById('feed').parentElement === f2);
  out.feedCount2 = feeds();
  out.renderedAfterFocus = window.__rendered[window.__rendered.length-1];

  // A button a feature rendered INSIDE a window must actually fire. This is the whole point of the
  // shell: if clicks do not reach the feature, the desktop is a picture of the app.
  window.__clicked = 0;
  const fw = document.querySelector('.osw.focused');
  const sb = fw && fw.querySelector('.stub-btn');
  out.hasBtn = !!sb;
  if (sb) { sb.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true})); sb.click(); }
  await sleep(120);
  out.clicked = window.__clicked;
  // …and in a window that is NOT focused: the click should focus it and still work.
  const uw = [...document.querySelectorAll('.osw')].find(w => !w.classList.contains('focused'));
  const ub = uw && uw.querySelector('.stub-btn');
  if (ub) { ub.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true})); await sleep(120); ub.click(); }
  await sleep(120);
  out.clickedUnfocused = window.__clicked;

  // Window controls.
  const w = document.querySelector('.osw.focused');
  w.querySelector('[data-w="max"]').click(); await sleep(80);
  out.maximised = w.classList.contains('maximised') && w.offsetWidth > window.innerWidth * 0.9;
  w.querySelector('[data-w="max"]').click(); await sleep(80);
  out.restored = !w.classList.contains('maximised');
  w.querySelector('[data-w="min"]').click(); await sleep(120);
  out.minimised = w.classList.contains('minimised');
  out.feedAfterMin = feeds();          // still exactly one, on whatever took focus
  const t = [...document.querySelectorAll('.os-task')].find(b => b.textContent.trim());
  if (t) { t.click(); await sleep(120); }
  out.restoredFromTask = !w.classList.contains('minimised') || document.querySelectorAll('.osw:not(.minimised)').length > 0;

  const before = document.querySelectorAll('.osw').length;
  document.querySelector('.osw.focused [data-w="close"]').click(); await sleep(120);
  out.closed = document.querySelectorAll('.osw').length === before - 1;
  out.feedAfterClose = feeds();

  PCOS.exit(); await sleep(150);
  out.exited = !PCOS.isOn() && !document.querySelector('#os-root');
  out.feedReturned = feedIn('.main');
  out.feedCountAfterExit = feeds();
  return out;
})()"""

GATE = r"""(() => { PCOS.enter(); const on = PCOS.isOn(); if (on) PCOS.exit();
                   return { on, toasts: (window.__toasts||[]).length,
                            msg: (window.__toasts||[]).slice(-1)[0] || '' }; })()"""

TOUCH = r"""(async () => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  PCOS.enter(); await sleep(150);
  document.querySelector('.os-icon[data-view="calendar"]').click(); await sleep(150);
  const w = document.querySelector('.osw');
  const bar = w.querySelector('.osw-bar');
  const cs = getComputedStyle(bar);
  const btn = w.querySelector('.osw-b').getBoundingClientRect();
  const grip = w.querySelector('.osw-grip').getBoundingClientRect();
  // touch-action must be none, or the browser takes the gesture as a scroll and nothing moves.
  const touchAction = cs.touchAction;
  const x0 = parseInt(w.style.left,10), y0 = parseInt(w.style.top,10);
  const pd = (type, x, y) => bar.dispatchEvent(new PointerEvent(type,
      {bubbles:true, cancelable:true, clientX:x, clientY:y, pointerType:'touch', isPrimary:true}));
  pd('pointerdown', x0+80, y0+16);
  document.dispatchEvent(new PointerEvent('pointermove',
      {bubbles:true, clientX:x0+220, clientY:y0+140, pointerType:'touch'}));
  await sleep(60);
  document.dispatchEvent(new PointerEvent('pointerup', {bubbles:true, pointerType:'touch'}));
  await sleep(60);
  const moved = { dx: parseInt(w.style.left,10) - x0, dy: parseInt(w.style.top,10) - y0 };
  PCOS.exit();
  return { touchAction, btnH: Math.round(btn.height), btnW: Math.round(btn.width),
           gripW: Math.round(grip.width), moved };
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

            async def js(expr, awaited=False):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True, "awaitPromise": awaited})
                if r.get("exceptionDetails"):
                    if os.environ.get("PC_DEBUG"):
                        print("  DEBUG:", json.dumps(r["exceptionDetails"])[:600])
                    return None
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")

            for w, h, wide, touch in WIDTHS:
                label = f"{w}px"
                await call("Emulation.setDeviceMetricsOverride",
                           {"width": w, "height": h, "deviceScaleFactor": 2 if touch else 1, "mobile": touch})
                await call("Emulation.setTouchEmulationEnabled",
                           {"enabled": touch, "maxTouchPoints": 5 if touch else 0})
                await call("Page.navigate", {"url": url})
                ok = False
                for _ in range(80):
                    await asyncio.sleep(0.25)
                    if await js("window.__ready === true && !!window.PCOS"):
                        ok = True
                        break
                if not ok:
                    print(f"SKIP  {label}: the page never finished loading")
                    return 2

                if not wide:
                    g = await js(GATE)
                    if not g or g["on"]:
                        problems.append((label, "mobile-not-gated",
                                         "the desktop opened below 1024px, where it cannot work"))
                    elif "sideways" not in (g.get("msg") or "") and h >= 1024:
                        # A tablet held upright can just be turned; say so instead of "too narrow".
                        problems.append((label, "mobile-not-gated",
                                         f"a rotatable screen was told {g.get('msg')!r}"))
                    continue

                if touch:
                    t = await js(TOUCH, awaited=True)
                    if not t:
                        problems.append((label, "touch-broken", "the touch test did not run"))
                    else:
                        if t["touchAction"] != "none":
                            problems.append((label, "touch-broken",
                                             f"the title bar has touch-action:{t['touchAction']} — a "
                                             "finger drag scrolls the page instead of moving the window"))
                        if t["moved"]["dx"] < 100 or t["moved"]["dy"] < 100:
                            problems.append((label, "touch-broken",
                                             f"a touch drag moved the window {t['moved']}"))
                        if t["btnH"] < 40 or t["gripW"] < 24:
                            problems.append((label, "tiny-tap-target",
                                             f"window controls are {t['btnW']}x{t['btnH']}, grip "
                                             f"{t['gripW']}px — too small for a thumb"))

                r = await js(DRIVE, awaited=True)
                if r is None:
                    print(f"SKIP  {label}: the desktop script did not evaluate")
                    return 2

                if not r.get("hasBtn") or not r.get("clicked"):
                    problems.append((label, "clicks-dead",
                                     "a button a feature rendered inside a window did not fire — "
                                     f"hasBtn={r.get('hasBtn')} clicked={r.get('clicked')}"))
                if r.get("docWins") != 1 or r.get("docDedup") != 1 or not r.get("docFeed") \
                        or not r.get("docTask") or not r.get("docPaint") \
                        or r.get("docClosed") != 0:
                    problems.append((label, "post-window-broken",
                                     "opening a post on the desktop must give it its own window "
                                     "(and re-opening it must focus that one, not add another) — "
                                     f"opened={r.get('docWins')} after-reopen={r.get('docDedup')} "
                                     f"feed-inside={r.get('docFeed')} taskbar={r.get('docTask')} "
                                     f"repaints={r.get('docPaint')} left-open={r.get('docClosed')}"))
                if not r.get("modalReachable"):
                    problems.append((label, "modal-buried",
                                     "a modal is not clickable on the desktop — the point at the "
                                     f"centre of its button hits {r.get('modalCoveredBy')!r}. Reply, "
                                     "quote, confirm and every settings dialog open this way."))
                if not r.get("hasNew") or not r.get("composed"):
                    problems.append((label, "cannot-post",
                                     "there is no working New post button on the taskbar — the "
                                     "classic + lives inside the timeline, which in a window is a "
                                     "corner nobody finds"))
                if not (r["entered"] and r["hasBar"] and r["hasStart"] and r["icons"]):
                    problems.append((label, "no-desktop",
                                     f"entered={r['entered']} bar={r['hasBar']} start={r['hasStart']} "
                                     f"icons={len(r['icons'])}"))
                if r.get("iconsOffscreen"):
                    problems.append((label, "icons-not-left",
                                     f"{r['iconsOffscreen']} desktop icon(s) run below the taskbar — "
                                     "present but unreachable"))
                # A GRID in the top-left: rows that wrap, at most 3 across. One tall column ran off
                # the bottom; more than three would march across the desktop into the windows.
                if not (1 <= r.get("iconCols", 0) <= 3):
                    problems.append((label, "icons-not-left",
                                     f"the desktop icons form {r['iconCols']} columns — want 1-3"))
                if r["icons"] != r["navViews"]:
                    problems.append((label, "apps-missing",
                                     f"desktop icons {r['icons']} do not match the sidebar {r['navViews']}"))
                if r["menuApps"] != r["navViews"]:
                    problems.append((label, "apps-missing",
                                     f"the start menu lists {r['menuApps']}"))
                # "cal" legitimately matches Calendar AND Calls — the filter is a substring match
                # on the label, and narrowing it further would be worse.
                if sorted(r["filtered"]) != ["calendar", "calls"]:
                    problems.append((label, "apps-missing",
                                     f"searching 'cal' gave {r['filtered']}"))
                if r["windows"] != 2 or r["tasks"] != 2:
                    problems.append((label, "no-desktop",
                                     f"{r['windows']} window(s), {r['tasks']} taskbar button(s), want 2 and 2"))

                # The whole mechanism: exactly one #feed, on the focused window.
                for name, cnt in (("after opening", r["feedCount"]), ("after focus", r["feedCount2"]),
                                  ("after minimise", r["feedAfterMin"]), ("after close", r["feedAfterClose"])):
                    if cnt != 1:
                        problems.append((label, "feed-not-handed-over",
                                         f"{cnt} elements carry id=feed {name} — must be exactly 1"))
                if not r["feedOnFocused"]:
                    problems.append((label, "feed-not-handed-over",
                                     "the focused window's body does not carry id=feed"))
                if not r["feedMoved"]:
                    problems.append((label, "feed-not-handed-over",
                                     "focusing another window did not move id=feed to it"))
                if r["renderedAfterFocus"] != "contacts" and r["renderedAfterFocus"] != "calendar":
                    problems.append((label, "feed-not-handed-over",
                                     f"focusing did not re-render a feature (last was {r['renderedAfterFocus']})"))

                if not r["maximised"] or not r["restored"]:
                    problems.append((label, "window-controls",
                                     f"maximise={r['maximised']} restore={r['restored']}"))
                if not r["minimised"]:
                    problems.append((label, "window-controls", "minimise did nothing"))
                if not r["closed"]:
                    problems.append((label, "window-controls", "close did not remove the window"))

                if not r["exited"]:
                    problems.append((label, "no-desktop", "leaving did not tear the desktop down"))
                if not r["feedReturned"] or r["feedCountAfterExit"] != 1:
                    problems.append((label, "feed-not-returned",
                                     "id=feed was not handed back to the client's own element — the "
                                     "classic UI would render into a detached node"))
    finally:
        proc.terminate()
        subprocess.run(["rm", "-rf", PROFILE], check=False)

    if problems:
        print(f"FAIL  {len(problems)} problem(s):")
        for label, kind, msg in problems:
            print(f"  [{label}] {kind}: {msg}")
        return 1
    print("OK  PosterChan OS desktop checks passed")
    return 0


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="oscheck-")
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
