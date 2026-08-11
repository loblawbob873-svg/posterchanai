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
  drag-stuck           A window goes on following the pointer after the button is released. The
                       release can be LOST — a native drag of the title bar, a cancelled pointer, an
                       alt-tab — and then nothing ends the gesture.
  snap-broken          Dragging a window to a screen edge does not snap it to that half (or does it
                       without previewing where it will land, or cannot be dragged back off).
  reminder-buried      A fired reminder's overlay renders under the desktop. It is the one surface
                       that is meant to interrupt, and it cannot be dismissed if it is behind
                       something.
  stray-mini-player    The floating music player renders on the desktop. The Music WINDOW is the
                       player; a second, smaller one beside it (or in its place when you close it)
                       is two sets of controls for one thing.
  modal-buried         A modal is not clickable — .modal-bg was authored at z-index 100, below the
                       z-index:300 desktop, so reply / quote / confirm / settings opened INVISIBLY
                       behind it. Hit-tested with elementFromPoint, not by reading the stylesheet.
  post-window-broken   Clicking a post does not open it in its own window (or opens a second one
                       when the post is already open).
  layout-drag-dead     The desktop cannot be arranged: dragging an icon does not reorder it, dropping
                       one on another does not make a folder, an icon cannot be taken back out of
                       one, or "Hide from desktop" does not hide it.
  layout-drag-opens    The click that ends a drag also opens the app that was dragged.
  layout-not-saved     The arrangement never reached the relay — it is on screen and nowhere else.
  layout-not-hydrated  A saved arrangement is not read back (or is read back for the wrong account).
                       This is the one that reads as "my layout was lost": the DEFAULT desktop
                       draws, which looks exactly like never having arranged one.
  layout-no-rollback   A write the relay REFUSED still shows as applied, so the next reload silently
                       undoes it.
  tray-not-connected   The taskbar's network widget says "No relays configured" while the pool is
                       connected. os.js reaches the pool as the GLOBAL `Relay`, guarded as
                       `window.Relay && Relay.conns && …` — so a local binding named `Relay` in that
                       file turns every one of those into a silent false. Classic mode is unaffected,
                       which is what makes it look like a server outage.
  layout-wipe          A read that never completed is taken as "this account has no layout", so the
                       next drag publishes the DEFAULTS over the real document — on every device,
                       since it is addressable. Relay.query() resolves [] (complete:false) rather
                       than rejecting, which is what makes this look like a working empty desktop.
  stale-view           Refocusing a window leaves the client's VIEW naming the window you came from.
                       Every painter tests VIEW and then writes into `#feed` — which is the window
                       in front — so the timeline prepends its live posts, redraws on EOSE and
                       paginates on scroll into a Profile or Post window. Shipped as "opened a
                       profile in a new window and timeline posts started filling it in".

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
  // `quiet` = adopt the view WITHOUT painting. The desktop asks for it every time it refocuses a
  // window whose real DOM it still has, so a stub that painted anyway would hide the very thing
  // parking exists to keep — and would make an adopted view indistinguishable from a repaint.
  switchView: (v, quiet) => {
    window.__view = v; window.__rendered.push(v);
    if (quiet) return;
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

/* Enough of a signer and a relay for the DESKTOP LAYOUT — the arrangement of the icons, which is
 * one self-encrypted kind-30078 document. The stub is deliberately thin: it stores the event the
 * client publishes and hands it back on the next query, which is all it takes to tell "the layout
 * was saved and read again" from "the layout was applied to the screen and forgotten". The
 * ciphertext is the plaintext, so the check can read what was written. */
window.__me = { pubkey: 'aa'.repeat(32) };
window.__relayDoc = null;        // the one stored event
window.__pubOk = true;           // flip to false to make the relay refuse a write
window.__published = [];
window.__prompt = 'Stuff';
Object.assign(window.__PC, {
  me: () => window.__me,
  nip44enc: (_pk, s) => Promise.resolve(s),
  nip44dec: (_pk, s) => Promise.resolve(s),
  uiPrompt: (_t, o) => Promise.resolve(window.__prompt === null ? null : window.__prompt),
  uiConfirm: () => Promise.resolve(true),
  publish: (kind, content, tags) => {
    const ev = { id:'e'+(window.__published.length+1), kind, content, tags,
                 pubkey: window.__me.pubkey,
                 created_at: Math.floor(Date.now()/1000) + window.__published.length };
    window.__published.push(ev);
    if (window.__pubOk) window.__relayDoc = ev;
    return Promise.resolve({ ok: window.__pubOk, ev });
  },
});
window.Store = { query: () => [] };     // cold cache: the read has to come off the relay
window.Relay = {
  /* The POOL, as the taskbar sees it. These three are reached as GLOBALS by os.js
   * (`window.Relay && Relay.conns && Relay.conns()`), and a local binding named `Relay` in that
   * file shadows them into `undefined` — silently, because every call site is guarded. That shipped
   * once and the desktop's tray read "No relays configured" on a working, connected client. */
  conns: () => [{ url:'wss://relay.example/one', status:'ok', open:true, idle:100 },
                { url:'wss://relay.example/two', status:'ok', open:true, idle:100 }],
  watch: () => () => {},
  wake: () => {},
  // Honours `authors`, because "does another account see this desktop" is one of the questions —
  // a stub that answered every author with the one document could never fail that.
  query: (fs) => {
    const f = (fs || [])[0] || {}, d = window.__relayDoc;
    const mine = d && (!f.authors || f.authors.indexOf(d.pubkey) >= 0);
    return Promise.resolve(mine ? [d] : []);
  },
  subscribe: () => 1,
  close: () => {},
};
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
  for (const cls of ['modal-bg', 'modal-bg modal-sub']) {
    const bg = document.createElement('div');
    bg.className = cls;
    bg.innerHTML = '<div class="modal glass neon-border"><button id="__probe">go</button></div>';
    let mr = document.getElementById('modal-root');
    if (!mr) { mr = document.createElement('div'); mr.id = 'modal-root'; document.body.appendChild(mr); }
    mr.appendChild(bg);
    document.body.classList.add('modal-open');
    await sleep(60);
    const b = document.getElementById('__probe');
    const r = b.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width/2, r.top + r.height/2);
    const ok = !!(hit && (hit === b || b.contains(hit)));
    const by = ok ? '' : (hit ? (hit.id || hit.className || hit.tagName).toString().slice(0,40) : 'nothing');
    if (cls === 'modal-bg'){ out.modalReachable = ok; out.modalCoveredBy = by; }
    // A SUB-modal — the Blossom picker, opened over the post composer and over the email composer —
    // is the one that regressed: it carried an INLINE z-index, and an inline value beats the rule
    // that lifts modals over the desktop. Local (a native file input) worked, Blossom silently
    // did nothing.
    else { out.subReachable = ok; out.subCoveredBy = by; }
    bg.remove(); document.body.classList.remove('modal-open');
  }

  /* Overlays appended to documentElement are SIBLINGS of <body>, so no `body.os-on …` rule can ever
   * reach them. The music player is one: it kept z-index:120 and went on playing, invisible, under
   * the z-index:300 desktop. Hit-tested rather than read off the stylesheet, because the rule that
   * was wrong looked perfectly correct. */
  {
    const mp = document.createElement('div');
    mp.id = 'music-player'; mp.className = 'mp';
    mp.style.cssText = 'left:40px;bottom:70px;width:260px;height:56px';
    mp.innerHTML = '<button id="__mpb">play</button>';
    document.documentElement.appendChild(mp);
    await sleep(60);
    const b = document.getElementById('__mpb');
    const r = b.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width/2, r.top + r.height/2);
    // The desktop deliberately does NOT show the floating player — the Music WINDOW is the player,
    // and a second smaller one beside it is two sets of controls for one thing. So the requirement
    // flipped: it must not be rendered at all. (It stays z-indexed above the desktop for any build
    // that does show it, which is what the original assertion was guarding.)
    out.playerHidden = getComputedStyle(mp).display === 'none';
    out.playerCoveredBy = (hit ? (hit.id || hit.className || hit.tagName).toString().slice(0,40) : 'nothing');
    mp.remove();
  }

  /* A fired reminder must be reachable ON TOP of the desktop. It is the one thing in the app that
   * deliberately interrupts, and it mounts on <body> at z-index 600 — above the z-index:300 desktop,
   * but that is exactly the kind of arithmetic that was wrong for modals, the Blossom picker and the
   * music player in turn. Hit-tested, not read off the stylesheet. */
  {
    const ov = document.createElement('div');
    ov.id = 'reminderOverlay';
    ov.style.cssText = 'position:fixed;inset:0;z-index:600;display:grid;place-items:center';
    ov.innerHTML = '<button id="__rd">Dismiss</button>';
    document.body.appendChild(ov);
    await sleep(60);
    const b = document.getElementById('__rd');
    const r = b.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width/2, r.top + r.height/2);
    out.reminderReachable = !!(hit && (hit === b || b.contains(hit)));
    out.reminderCoveredBy = out.reminderReachable ? '' :
      (hit ? (hit.id || hit.className || hit.tagName).toString().slice(0,40) : 'nothing');
    ov.remove();
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

  /* Refocusing a window must hand the client back the VIEW that window is showing.
   *
   * `#feed` is one element that MOVES, and every painter in the client tests the VIEW global before
   * writing into it. So a window that holds the feed while VIEW still names the window you came from
   * is not a bookkeeping detail: the timeline prepends its live posts, redraws on EOSE and paginates
   * on scroll straight into whatever is in front. Shipped as "opened a profile in a new window and
   * timeline posts started filling it in". A DOC window has no view name of its own, which is
   * exactly why it was the one left behind.
   */
  {
    const before = document.querySelectorAll('.osw').length;
    const task = re => [...document.querySelectorAll('.os-task')].find(t => re.test(t.textContent));
    document.querySelector('.os-icon[data-view="calendar"]').click(); await sleep(150);
    PCOS.openDoc('prof:zz', 'Profile', 'i-user', () => {
      window.__view = 'profile';    // renderProfileView sets VIEW itself on the way in
      document.getElementById('feed').innerHTML = '<div class="stub-doc">PROFILE</div>';
    });
    await sleep(150);
    task(/Calendar/i).click(); await sleep(150);
    out.viewOnFeature = window.__view;
    task(/Profile/i).click(); await sleep(200);
    out.viewOnDoc     = window.__view;
    out.docFeedBack   = !!(document.getElementById('feed') || {}).querySelector('.stub-doc');
    for (const x of [...document.querySelectorAll('.osw')].slice(0, 2)){
      x.querySelector('.osw-x').click(); await sleep(60);
    }
    out.viewWinsClosed = document.querySelectorAll('.osw').length - before;
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
    // A drag must not be able to outlive the button. Reported as "click on it, sticks to the mouse,
    // never persists": the release is lost — the browser starts its own drag of the title, the OS
    // claims the gesture, the pointerup lands somewhere we never see — and the window then follows
    // the cursor with nothing held down. Two ways in, both checked.
    const stuck = async (endWith) => {
      const w1 = document.querySelector('.osw.focused');
      const bar1 = w1.querySelector('.osw-bar');
      const r = w1.getBoundingClientRect();
      bar1.dispatchEvent(new PointerEvent('pointerdown',
        {bubbles:true, clientX:r.left+70, clientY:r.top+12, pointerId:7, buttons:1}));
      document.dispatchEvent(new PointerEvent('pointermove',
        {bubbles:true, clientX:600, clientY:400, pointerId:7, buttons:1}));
      await sleep(40);
      endWith(w1);
      await sleep(40);
      const before = w1.getBoundingClientRect().left;
      // …now move the pointer with NOTHING held. A live drag would follow it.
      document.dispatchEvent(new PointerEvent('pointermove',
        {bubbles:true, clientX:900, clientY:500, pointerId:7, buttons:0}));
      await sleep(60);
      return Math.abs(w1.getBoundingClientRect().left - before) < 3;
    };
    out.stuckOnCancel = await stuck(() =>
      document.dispatchEvent(new PointerEvent('pointercancel', {bubbles:true, pointerId:7})));
    out.stuckOnLostUp = await stuck(() => {});   // the pointerup simply never arrives

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

"""Arranging the desktop, driven with real pointer events against the real stylesheet.

Every assertion here is something that LOOKS like it worked and did not:

  layout-drag-dead    Dragging an icon onto another does not make a folder of the two.
  layout-not-saved    The arrangement never reached the relay — it is on screen and nowhere else,
                      so it is gone on the next reload and absent on every other device.
  layout-not-hydrated A saved arrangement is not read back. This is the failure that reads as
                      "my layout was lost": the default desktop draws, which is indistinguishable
                      from never having arranged one.
  layout-no-rollback  A write the relay REFUSED still shows as applied. The icon moves, the user
                      believes it, and the next reload silently puts it back.
  layout-drag-opens   The click that ends a drag also opens the app that was dragged.
"""
LAYOUT = r"""(async () => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const out = {};
  try{
  window.__relayDoc = null; window.__published = []; window.__pubOk = true; window.__prompt = 'Stuff';
  const icon = v => [...document.querySelectorAll('.os-icons .os-icon')].find(b => b.dataset.view === v);
  const views = () => [...document.querySelectorAll('.os-icons .os-icon')].map(b => b.dataset.view);
  const pev = (el, type, x, y) => el.dispatchEvent(new PointerEvent(type,
      { bubbles:true, cancelable:true, clientX:x, clientY:y, pointerType:'mouse',
        buttons: type === 'pointerup' ? 0 : 1, button:0, isPrimary:true }));
  // A drag: press on `from`, cross the threshold, land on (x,y), release. The move events go to
  // document, which is where the handler listens — a drag that only moved over the icon it started
  // on would never leave it.
  const drag = async (from, x, y) => {
    const r = from.getBoundingClientRect();
    pev(from, 'pointerdown', r.left + r.width/2, r.top + r.height/2);
    pev(document, 'pointermove', r.left + r.width/2 + 12, r.top + r.height/2 + 12);
    await sleep(20);
    pev(document, 'pointermove', x, y);
    await sleep(20);
    pev(document, 'pointerup', x, y);
    await sleep(160);
  };
  const mid = el => { const r = el.getBoundingClientRect(); return [r.left + r.width/2, r.top + r.height/2]; };

  PCOS.enter(); await sleep(250);          // …which reads the (empty) layout off the stub relay
  out.start = views();
  // The taskbar's own view of the pool. Two live relays are stubbed above, so anything other than
  // "connected" means os.js is not reaching the global Relay object any more.
  { const nb = document.querySelector('#os-net');
    out.netTitle = nb ? (nb.getAttribute('title') || '') : '(no tray button)';
    out.netClass = nb ? nb.className : ''; }

  // 1. Drop Notes on the MIDDLE of News: a folder holding both, in News's place.
  window.__clicked_open = 0;
  const before = views();
  await drag(icon('notes'), ...mid(icon('news')));
  await sleep(220);                        // the rename prompt resolves and saves a second time
  out.afterMerge = views();
  out.folderKey = (views().find(v => v.indexOf('folder:') === 0) || '');
  // The LAST span: a folder tile leads with .os-fold (its members' glyphs), and the label follows.
  out.folderLabel = (() => { const b = [...document.querySelectorAll('.os-icons .os-icon')]
      .find(b => b.dataset.view.indexOf('folder:') === 0);
    return b ? ([...b.querySelectorAll('span')].pop() || {}).textContent || '' : ''; })();
  out.mergedAway = before.length - views().length;      // two icons became one
  // The drag must not ALSO open the app it dragged.
  out.openedWindows = document.querySelectorAll('.osw').length;
  // …and it must have been written, not just drawn.
  out.savedDoc = window.__relayDoc ? window.__relayDoc.content : '';

  // 2. Open the folder window and check its members, then take an icon back out to the desktop.
  const fb = [...document.querySelectorAll('.os-icons .os-icon')].find(b => b.dataset.view.indexOf('folder:') === 0);
  if (fb) { fb.click(); await sleep(160); }
  const slot = document.querySelector('.osw-slot.os-folder');
  out.folderMembers = slot ? [...slot.querySelectorAll('.os-icon')].map(b => b.dataset.view) : null;
  if (slot) {
    const deskR = document.querySelector('#os-desk').getBoundingClientRect();
    await drag(slot.querySelector('.os-icon[data-view="notes"]'),
               deskR.right - 60, deskR.bottom - 120);   // empty desktop, clear of the icon column
    await sleep(200);
  }
  out.afterOut = views();
  // One member left is not a folder: it dissolves, its last app goes back on the desktop in the
  // folder's place, and the window — which now has nothing to show — closes with it.
  out.folderGone = !document.querySelector('.osw-slot.os-folder');
  out.folderTileGone = !views().some(v => v.indexOf('folder:') === 0);

  // 3. Reorder: drag Social to the left edge of whatever is first, which must put it first.
  {
    const first = icon(views()[0]), r = first.getBoundingClientRect();
    await drag(icon('global'), r.left + 4, r.top + r.height/2);
    out.afterReorder = views();
  }

  // 4. Hide an icon from the desktop. It must stay in the start menu, which is the way back.
  {
    const n = icon('news');
    n.dispatchEvent(new MouseEvent('contextmenu', { bubbles:true, cancelable:true,
                                                    clientX: n.getBoundingClientRect().left + 10,
                                                    clientY: n.getBoundingClientRect().top + 10 }));
    await sleep(60);
    const row = [...document.querySelectorAll('.os-ctx-b')].find(b => /Hide/.test(b.textContent));
    out.hasHideRow = !!row;
    if (row) row.click();
    await sleep(200);
    out.afterHide = views();
    document.querySelector('#os-start').click(); await sleep(120);
    out.menuHasHidden = [...document.querySelectorAll('.os-app')].some(b => b.dataset.view === 'news');
    document.querySelector('#os-start').click(); await sleep(80);
  }

  // 5. HYDRATION. Switch identity and back, which drops the in-memory copy exactly as a reload
  //    does — then the desktop has to come back out of the relay or it was never really saved.
  const want = views();
  window.__me = { pubkey: 'bb'.repeat(32) };
  // Generous: a read that finds NOTHING retries (a first REQ at a warming socket EOSEs empty), so
  // an account with no layout of its own takes over a second to settle.
  PCOS.refresh(); await sleep(1600);
  out.otherAccount = views();
  window.__me = { pubkey: 'aa'.repeat(32) };
  PCOS.refresh(); await sleep(500);
  out.hydrated = views();
  out.wanted = want;

  // 6. A write the relay REFUSES must not stay on screen.
  window.__pubOk = false;
  {
    const beforeR = views();
    const last = icon(beforeR[beforeR.length - 1]);
    await drag(icon(beforeR[0]), ...mid(last));
    await sleep(250);
    out.refusedOrder = views();
    out.refusedWanted = beforeR;
    out.refusedToast = (window.__toasts || []).slice(-1)[0] || '';
  }
  window.__pubOk = true;

  /* 7. THE WIPE. Relay.query() has NO reject path: when nothing EOSEs it RESOLVES with [] marked
   *    `complete:false`. So a zombie socket answers exactly like an account that has never arranged
   *    anything — and if that arms the writer, the first icon dragged publishes the DEFAULTS over a
   *    real layout, on every device, because the event is addressable. This is the failure vault.js
   *    documents (its own guard was dead code for the same reason), so it is driven here rather
   *    than reasoned about: an incomplete read must publish NOTHING. */
  {
    const realQ = window.Relay.query;
    window.Relay.query = () => { const a = []; a.complete = false; return Promise.resolve(a); };
    window.__me = { pubkey: 'cc'.repeat(32) };      // a fresh identity forces a fresh read
    // Long enough for all three attempts (0 + 450 + 900ms) to finish: the window where the read is
    // still running is not the window this is about — a drag is refused then for a different and
    // correct reason, and a shorter wait would pass with the guard removed.
    PCOS.refresh(); await sleep(1900);
    const n = window.__published.length;
    const v = views();
    await drag(icon(v[1]), ...mid(icon(v[0])));
    await sleep(300);
    out.wipePublished = window.__published.length - n;
    out.wipeToast = (window.__toasts || []).slice(-1)[0] || '';
    window.Relay.query = realQ;
  }

  // A half-run must report what it got to rather than evaluating to nothing — "the script did not
  // run" is the least useful failure this harness can print.
  }catch(err){ out.err = String(err && err.stack || err).slice(0, 300); }
  try{ PCOS.exit(); }catch(_){}
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
                if not (r.get("stuckOnCancel") and r.get("stuckOnLostUp")):
                    problems.append((label, "drag-stuck",
                                     "a window keeps following the pointer after the button is gone "
                                     f"— survives-pointercancel={r.get('stuckOnCancel')} "
                                     f"survives-a-lost-pointerup={r.get('stuckOnLostUp')}"))
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
                if r.get("viewOnDoc") != "profile" or r.get("viewOnFeature") != "calendar" \
                        or not r.get("docFeedBack") or r.get("viewWinsClosed") != 0:
                    problems.append((label, "stale-view",
                                     "refocusing a window did not hand the client back the view that "
                                     "window is showing — every painter keys on VIEW and writes into "
                                     "#feed, so the timeline fills whatever window is in front "
                                     f"(feature={r.get('viewOnFeature')!r} doc={r.get('viewOnDoc')!r} "
                                     f"kept-its-dom={r.get('docFeedBack')} "
                                     f"left-open={r.get('viewWinsClosed')})"))
                if not r.get("reminderReachable"):
                    problems.append((label, "reminder-buried",
                                     "a fired reminder is not clickable on the desktop — its Dismiss "
                                     f"hits {r.get('reminderCoveredBy')!r}. It is the one thing that "
                                     "is supposed to interrupt."))
                if not r.get("playerHidden"):
                    problems.append((label, "stray-mini-player",
                                     "the floating music player renders on the desktop — the Music "
                                     "window is the player, and a second smaller one is two sets of "
                                     "controls for one thing"))
                if not r.get("subReachable"):
                    problems.append((label, "modal-buried",
                                     "a SUB-modal (the Blossom picker over a composer) is not "
                                     f"clickable — its button hits {r.get('subCoveredBy')!r}. Use "
                                     ".modal-sub; an inline z-index beats the rule that lifts "
                                     "modals over the desktop."))
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

                # Arranging the desktop — its own pass, because it re-enters and rewrites the icons.
                q = await js(LAYOUT, awaited=True)
                if q is None or q.get("err"):
                    problems.append((label, "layout-drag-dead",
                                     "the layout script threw: " + ((q or {}).get("err") or "no result")))
                if q:
                    fold = q.get("folderKey") or ""
                    if not fold or q.get("mergedAway") != 1:
                        problems.append((label, "layout-drag-dead",
                                         "dropping one icon on the middle of another did not make a "
                                         f"folder of the two — {q.get('start')} became {q.get('afterMerge')}"))
                    elif q.get("folderLabel") != "Stuff":
                        problems.append((label, "layout-drag-dead",
                                         "a new folder is not offered a name (or the name is not "
                                         f"applied) — it is called {q.get('folderLabel')!r}"))
                    if q.get("openedWindows"):
                        problems.append((label, "layout-drag-opens",
                                         f"{q['openedWindows']} window(s) opened during a drag — the "
                                         "click that ends a drag must not also open the app"))
                    if "notes" not in (q.get("savedDoc") or ""):
                        problems.append((label, "layout-not-saved",
                                         "the arrangement never reached the relay — it is on screen "
                                         "and nowhere else, so it is gone on the next reload "
                                         f"(stored: {(q.get('savedDoc') or '')[:80]!r})"))
                    if q.get("folderMembers") is not None and sorted(q["folderMembers"]) != ["news", "notes"]:
                        problems.append((label, "layout-drag-dead",
                                         f"the folder window lists {q['folderMembers']}, want news+notes"))
                    if "notes" not in (q.get("afterOut") or []):
                        problems.append((label, "layout-drag-dead",
                                         "dragging a member out of a folder onto the desktop did not "
                                         f"put it there — {q.get('afterOut')}"))
                    elif not (q.get("folderGone") and q.get("folderTileGone")
                              and "news" in (q.get("afterOut") or [])):
                        problems.append((label, "layout-drag-dead",
                                         "a folder left holding ONE app is still a folder — a tile "
                                         "you have to open to reach a single icon "
                                         f"(tile-gone={q.get('folderTileGone')} "
                                         f"window-gone={q.get('folderGone')} icons={q.get('afterOut')})"))
                    if (q.get("afterReorder") or [None])[0] != "global":
                        problems.append((label, "layout-drag-dead",
                                         f"reordering did not move the icon — {q.get('afterReorder')}"))
                    if not q.get("hasHideRow") or "news" in (q.get("afterHide") or []):
                        problems.append((label, "layout-drag-dead",
                                         "'Hide from desktop' did not remove the icon — "
                                         f"menu-row={q.get('hasHideRow')} icons={q.get('afterHide')}"))
                    elif not q.get("menuHasHidden"):
                        problems.append((label, "layout-drag-dead",
                                         "a hidden icon is not in the start menu either — that is a "
                                         "deleted app, not a hidden one, and there is no way back"))
                    if q.get("hydrated") != q.get("wanted"):
                        problems.append((label, "layout-not-hydrated",
                                         "the saved arrangement did not come back off the relay — "
                                         f"{q.get('hydrated')} instead of {q.get('wanted')}. This is "
                                         "the failure that reads as 'my layout was lost': the default "
                                         "desktop draws, which looks the same as never having one."))
                    if q.get("otherAccount") == q.get("wanted") and q.get("wanted"):
                        problems.append((label, "layout-not-hydrated",
                                         "another account sees the first account's desktop"))
                    if "No relays configured" in (q.get("netTitle") or "") \
                            or "Connected" not in (q.get("netTitle") or ""):
                        problems.append((label, "tray-not-connected",
                                         "the taskbar reports "
                                         f"{q.get('netTitle')!r} with two live relays in the pool — "
                                         "os.js is not reaching the GLOBAL Relay object (a local "
                                         "binding named `Relay` shadows it, and every call site is "
                                         "guarded so it fails silently)"))
                    if q.get("wipePublished"):
                        problems.append((label, "layout-wipe",
                                         "a rearrangement was PUBLISHED after a read that never "
                                         "completed — Relay.query() resolves [] with complete:false "
                                         "when nothing EOSEs, so a zombie socket reads as 'this "
                                         "account has no layout' and the first drag replaces the "
                                         "real document with the defaults on every device"))
                    if q.get("refusedOrder") != q.get("refusedWanted"):
                        problems.append((label, "layout-no-rollback",
                                         "a rearrangement the relay REFUSED is still on screen — it "
                                         "is not stored, so the next reload silently puts it back "
                                         f"({q.get('refusedOrder')} vs {q.get('refusedWanted')})"))
                    elif "couldn" not in (q.get("refusedToast") or ""):
                        problems.append((label, "layout-no-rollback",
                                         "a refused rearrangement is rolled back without saying so — "
                                         f"last toast was {q.get('refusedToast')!r}"))

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
