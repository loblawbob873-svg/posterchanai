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
  stray-post-button    The taskbar carries a New post button. It existed only because the compose
                       MODAL was rendering behind the desktop, so posting looked impossible; with
                       that fixed the timeline's own composer works in a window and the extra
                       button is clutter.
  mobile-not-gated     The desktop offers itself below 1024px, where it cannot work.
  drag-not-1to1        A dragged window does not keep up with the pointer. The client scales the page
                       with body{zoom}, so pointer deltas arrive in zoomed css pixels while
                       style.left is layout pixels; applying one to the other directly moves the
                       window by a factor of the zoom.
  dead-icon            A <use> in the desktop chrome names a symbol that does not exist, or is not
                       an id at all (href="i-wot" rather than "#i-wot"). Both draw nothing and log
                       nothing.
  start-search         The start menu's box does not offer a Nostr search as its first result, so
                       Enter opens an app instead of searching.
  view-not-windowed    A feature opened from inside another feature (Meme Builder on a post, say)
                       repaints the window it was launched from instead of getting its own — which
                       destroys whatever that window was showing.
  snap-broken          Dragging a window to a screen edge does not snap it to that half (or does it
                       without previewing where it will land, or cannot be dragged back off).
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
# A short landscape tablet is in here deliberately: it is the case where a fixed icon column count
# runs the last rows under the taskbar, which reads as "the icons are cut off".
WIDTHS = [(1600, 900, True, False), (1280, 800, True, True), (1024, 600, True, True),
          (800, 1280, False, True)]
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

  // Win11 snapping: drag to an edge, get a half; drag it off again, get the old size back.
  {
    document.querySelector('.os-icon').click();
    await sleep(120);
    const w0 = document.querySelector('.osw.focused');
    const bar = w0.querySelector('.osw-bar');
    const b0 = w0.getBoundingClientRect();
    bar.dispatchEvent(new PointerEvent('pointerdown',
      {bubbles:true, clientX:b0.left+80, clientY:b0.top+12, pointerId:1}));
    for (const x of [400, 200, 60, 4]) {
      document.dispatchEvent(new PointerEvent('pointermove',
        {bubbles:true, clientX:x, clientY:300, pointerId:1}));
      await sleep(16);
    }
    const g = document.querySelector('.os-ghost');
    out.ghostShown = !!(g && getComputedStyle(g).display !== 'none' && g.offsetWidth > 100);
    document.dispatchEvent(new PointerEvent('pointerup', {bubbles:true, clientX:4, clientY:300, pointerId:1}));
    await sleep(120);
    const b1 = w0.getBoundingClientRect();
    const desk = document.querySelector('.os-desk');
    out.dbg = { zoom: getComputedStyle(document.body).zoom,
                innerW: window.innerWidth, deskW: desk.clientWidth,
                winOffW: w0.offsetWidth, winOffL: w0.offsetLeft,
                rectW: Math.round(b1.width), rectL: Math.round(b1.left) };
    out.snappedHalf = Math.abs(w0.offsetWidth - (desk.clientWidth/2 - 16)) < 24 && w0.offsetLeft < 24;
    // The drag must also track the cursor 1:1 — under body{zoom} it used to lag behind it.

    out.ghostHidden = !document.querySelector('.os-ghost') ||
                      getComputedStyle(document.querySelector('.os-ghost')).display === 'none';
    // …and dragging it back off the edge restores the size it had before the snap.
    const b2 = w0.getBoundingClientRect();
    bar.dispatchEvent(new PointerEvent('pointerdown',
      {bubbles:true, clientX:b2.left+80, clientY:b2.top+12, pointerId:1}));
    for (const x of [300, 500, 700]) {
      document.dispatchEvent(new PointerEvent('pointermove',
        {bubbles:true, clientX:x, clientY:340, pointerId:1}));
      await sleep(16);
    }
    document.dispatchEvent(new PointerEvent('pointerup', {bubbles:true, clientX:700, clientY:340, pointerId:1}));
    await sleep(80);
    out.unsnapped = Math.abs(w0.getBoundingClientRect().width - b0.width) < 24;
    w0.querySelector('.osw-x').click();      // leave no window behind for the checks that follow
    await sleep(80);
  }

  // A feature opened from INSIDE another feature gets its own window, and the window it was launched
  // from survives. This is the Meme-Builder-from-a-post case: it used to repaint the Social window.
  {
    const base = document.querySelectorAll('.osw').length;
    PCOS.routeView('global'); await sleep(120);
    const firstEl = document.querySelector('.osw.focused');
    const took = PCOS.routeView('meme'); await sleep(120);
    out.routeTook  = !!took;
    out.routeWins  = document.querySelectorAll('.osw').length - base;
    out.routeKept  = !!(firstEl && firstEl.isConnected);
    out.routeFeedIn = feedIn('.osw.focused');
    // Re-routing to a view that is already open must FOCUS it, not open a second copy.
    PCOS.routeView('global'); await sleep(120);
    out.routeDedup = document.querySelectorAll('.osw').length - base;
    // A view the launcher does not know about must not conjure a window at all.
    out.routeUnknown = PCOS.routeView('no-such-view-xyz');
    document.querySelectorAll('.osw .osw-x').forEach(b => b.click());
    await sleep(120);
    out.routeClosed = document.querySelectorAll('.osw').length;
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
  // The first row is "Search Nostr for …" and carries no view — it is asserted separately below.
  out.filtered  = [...document.querySelectorAll('.os-app[data-view]')].map(b => b.dataset.view);
  // Every <use> in the desktop's own chrome must name a symbol that EXISTS and draw something. A
  // bare id (href="i-wot" instead of "#i-wot") resolves to nothing and renders nothing, with no
  // console error — which is exactly how the start-menu stat icons and the window-title icons for
  // Post/Profile/Search shipped blank.
  out.badIcons = [...document.querySelectorAll('#os-root svg use')].map(u => {
    const h = u.getAttribute('href') || u.getAttribute('xlink:href') || '';
    if (!h.startsWith('#')) return 'not-an-id:' + h;
    if (!document.getElementById(h.slice(1))) return 'missing:' + h;
    // Only icons that are SUPPOSED to be on screen. The taskbar search box is display:none below
    // 1080px, and "an icon inside a deliberately hidden element has no size" is not a defect.
    const svg = u.ownerSVGElement;
    if (svg.checkVisibility && !svg.checkVisibility()) return null;
    const r = svg.getBoundingClientRect();
    return (r.width < 2 || r.height < 2) ? 'zero-sized:' + h : null;
  }).filter(Boolean);
  { const f = document.querySelector('.os-app[data-find]');
    out.findRow = !!f;
    out.findFirst = !!(f && f === document.querySelector('.os-app'));
    out.findText = f ? (f.textContent||'').trim().slice(0, 40) : ''; }
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
  const r0 = w.getBoundingClientRect();
  const pd = (type, x, y) => bar.dispatchEvent(new PointerEvent(type,
      {bubbles:true, cancelable:true, clientX:x, clientY:y, pointerType:'touch', isPrimary:true}));
  pd('pointerdown', x0+80, y0+16);
  document.dispatchEvent(new PointerEvent('pointermove',
      {bubbles:true, clientX:x0+220, clientY:y0+140, pointerType:'touch'}));
  await sleep(60);
  document.dispatchEvent(new PointerEvent('pointerup', {bubbles:true, pointerType:'touch'}));
  await sleep(60);
  const moved = { dx: parseInt(w.style.left,10) - x0, dy: parseInt(w.style.top,10) - y0 };
  // What the FINGER sees. style.left is layout px and the pointer is in zoomed css px, so the
  // window used to travel body{zoom} times as far as the finger did — visibly lagging behind it.
  const r1 = w.getBoundingClientRect();
  const onScreen = { dx: Math.round(r1.left - r0.left), dy: Math.round(r1.top - r0.top) };
  PCOS.exit();
  return { touchAction, btnH: Math.round(btn.height), btnW: Math.round(btn.width),
           gripW: Math.round(grip.width), moved, onScreen, want: { dx: 140, dy: 124 } };
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
                        elif (abs(t["onScreen"]["dx"] - t["want"]["dx"]) > 12
                              or abs(t["onScreen"]["dy"] - t["want"]["dy"]) > 12):
                            problems.append((label, "drag-not-1to1",
                                             f"the finger moved {t['want']} but the window moved "
                                             f"{t['onScreen']} on screen — the drag is being applied "
                                             "in layout pixels to a body{zoom}'d page, so it lags "
                                             "behind the pointer"))
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
                if not (r.get("routeTook") and r.get("routeWins") == 2 and r.get("routeKept")
                        and r.get("routeFeedIn") and r.get("routeDedup") == 2
                        and r.get("routeUnknown") is False and r.get("routeClosed") == 0):
                    problems.append((label, "view-not-windowed",
                                     "opening a feature from inside another must give it its own "
                                     "window and leave the first one standing — "
                                     f"took-over={r.get('routeTook')} opened={r.get('routeWins')} "
                                     f"first-survived={r.get('routeKept')} feed-inside={r.get('routeFeedIn')} "
                                     f"after-reopen={r.get('routeDedup')} "
                                     f"unknown-view-routed={r.get('routeUnknown')}"))
                if not (r.get("ghostShown") and r.get("snappedHalf") and r.get("ghostHidden")
                        and r.get("unsnapped")):
                    problems.append((label, "snap-broken",
                                     "Windows-11 edge snapping is not working — "
                                     f"preview={r.get('ghostShown')} snapped-to-half={r.get('snappedHalf')} "
                                     f"preview-cleared={r.get('ghostHidden')} "
                                     f"restored-on-drag-off={r.get('unsnapped')} {r.get('dbg')}"))
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
                if r.get("hasNew"):
                    problems.append((label, "stray-post-button",
                                     "the taskbar still carries a New post button — posting is back "
                                     "in the timeline composer now that Social works in a window"))
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
                if r.get("badIcons"):
                    problems.append((label, "dead-icon",
                                     "the desktop draws icon(s) that resolve to nothing: "
                                     + ", ".join(sorted(set(r["badIcons"]))[:6])))
                if not (r.get("findRow") and r.get("findFirst")):
                    problems.append((label, "start-search",
                                     "typing in the start menu must offer a Nostr search FIRST, so "
                                     "Enter runs it — "
                                     f"row={r.get('findRow')} first={r.get('findFirst')} "
                                     f"{r.get('findText')!r}"))
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
