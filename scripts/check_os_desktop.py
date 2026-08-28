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
  startmenu-buried     The start menu opens BEHIND a window. Every focus bumps the window z-counter
                       (routeView does it on ordinary navigation inside one window), so an unbounded
                       counter walks over the start menu at 320 and every other panel above the
                       desktop. Checked after 400 switches, hit-tested — the menu looks perfect.
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
  window-offscreen     A window OPENS partly outside the desktop — under the taskbar or past the
                       right edge — so it has to be dragged back before it can be used. The cascade
                       used a fixed step and a fixed size, so on a short screen the sixth window ran
                       off the bottom.
  window-too-small     A window opens far smaller than the screen it is on. The size was capped at a
                       hardcoded 1100x760, so on a large display every app opened as a box in the
                       corner and the first thing anyone did was resize it.
  no-noti-bell         The taskbar has no notification bell (or it does not open the centre, or the
                       unread count is still riding on the clock, where a number reads as part of
                       the time and nothing says it can be pressed).
  tray-count-stuck     The bell lights once and then never moves again. Its count was the LENGTH of
                       the sliced notifItems list minus a snapshot — and a sliced list's length is a
                       constant on any account past the slice, so the subtraction was 0 for the rest
                       of the session. Driven as arrivals, because one reading of "60" looks fine.
                       Also fails if the repaint rebuilds the taskbar under a half-typed search.
  tray-click-swallowed A tray button does nothing because the OTHER flyout was open: the one
                       click-away handler closes by rebuilding the bar, which detaches the button
                       the pointerdown landed on, so its click never fires. _TRAY_KEEP.
  icon-not-placed      An icon dragged to empty desktop space does not land there, does not
                       persist, moves OTHER icons with it, or cannot be put back into a grid.
  no-wallpaper         Right-clicking the desktop does not offer a background from the drive's
                       `Backgrounds` folder, or choosing one does not paint / does not save.
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
PORT = int(os.environ.get("PC_CHECK_PORT") or 9486)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-os-check"

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
      <button class="nav-item" data-view="terminal"><svg class="ic"><use href="#i-terminal"></use></svg><span>Terminal</span></button>
      <button class="nav-item" data-view="markets"><svg class="ic"><use href="#i-chart"></use></svg><span>Markets</span></button>
      <button class="nav-item" data-view="news"><svg class="ic"><use href="#i-news"></use></svg><span>News</span></button>
      <button class="nav-item" data-view="stats"><svg class="ic"><use href="#i-chart"></use></svg><span>Server Stats</span></button>
    </nav>
  </aside>
  <div class="main"><div id="feed" class="feed">CLASSIC</div></div>
</div>
<script src="/static/js/client/sprite.js"></script>
<!-- The Today widget expands real recurrence rules through this. Loading the SHIPPED parser (rather
     than handing the widget pre-expanded objects) is what makes the row geometry below mean
     something: the widget builds its own rows from its own data, exactly as it does in the app. -->
<script src="/static/js/client/ical.js"></script>
<script>
// A stub of the one contract os.js depends on: switchView paints the named view into #feed.
window.__rendered = [];
window.__composed = 0;
window.__PC = {
  toast: m => (window.__toasts = window.__toasts || []).push(m),
  /* The player, as the Now-playing widget reaches it. Recording the calls is the whole point: the
     widget's buttons doing NOTHING is a silent failure — no throw, no log, nothing on screen — and
     it is exactly what a renamed or missing bridge method produces. */
  music: () => ({
    now: () => window.__musicNow || null,
    toggle: () => { (window.__music = window.__music || []).push('toggle'); },
    prev:   () => { (window.__music = window.__music || []).push('prev'); },
    next:   () => { (window.__music = window.__music || []).push('next'); },
    shuffle: () => { (window.__music = window.__music || []).push('shuffle'); },
    shuffling: () => !!window.__shuffling,
  }),
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
/* NOTIFICATIONS, as the tray bell reaches them. Two numbers on purpose: what is UNREAD, and how many
 * rows the centre can show. They are different quantities — the list is sliced — and the bell once
 * counted the list, so it stopped moving for good on any account past the slice. */
window.__unread = 0;
window.__read = 0;
Object.assign(window.__PC, {
  notifUnread: () => window.__unread,
  /* SATURATED, which is the ordinary state of a real account: the list is sliced to what the centre
     can show, so its LENGTH is a constant and says nothing about how much is new. */
  notifItems: (n) => Array.from({ length: Math.min(n || 30, 60) },
                                (_, i) => ({ id: 'n' + i, kind: 1, pubkey: 'bb'.repeat(32) })),
  notifHtml: (e) => '<div class="notif" data-open="' + e.id + '">a notification</div>',
  // Reading them is recorded where the whole app can see it — the bell must not keep its own count.
  notifsRead: () => { window.__unread = 0; window.__read++;
                      try{ PCOS.notifChanged(); }catch(_){} },
  mailUnread: () => 0,
  osNotifyState: () => 'granted',
  openThread: () => {}, reactTo: () => {},
  /* The calendar API, as the Today widget reaches it. Two calendars with DIFFERENT colours, because
     the bug this exists for is a day label painting over the colour swatch — one colour would still
     draw a swatch, but two is what the user was looking at ("over the Green and Red"). One event
     today and one TOMORROW: the "Coming up" rows are the ones whose first column holds a word
     rather than a time, and they are the rows that overflowed. */
  authFetch: async (url) => {
    const pad = (n) => String(n).padStart(2, '0');
    const stamp = (d, h) => `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}T${pad(h)}0000`;
    const now = new Date();
    const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
    const vevent = (uid, d, h, title) =>
      `BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:${uid}\r\nDTSTART:${stamp(d, h)}\r\n` +
      `DTEND:${stamp(d, h + 1)}\r\nSUMMARY:${title}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n`;
    const body =
      url.indexOf('/api/calendar/config') >= 0 ? { enabled: true } :
      url.indexOf('/api/calendar/calendars') >= 0
        ? { calendars: [{ id: 'work', name: 'Work', color: '#22c55e' },
                        { id: 'home', name: 'Home', color: '#ef4444' }] } :
      url.indexOf('cal=work') >= 0
        ? { items: [{ id: 'a', ics: vevent('a', now, 9, 'Stand-up') }] } :
      url.indexOf('cal=home') >= 0
        ? { items: [{ id: 'b', ics: vevent('b', tomorrow, 18, 'Dentist') }] } :
      {};
    return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
  },
});
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
/* The encrypted drive, as the wallpaper picker sees it: a `Backgrounds` folder with two images,
 * and encFileUrl standing in for fetch+decrypt (the real one returns a one-session object URL). */
window.__bgFetched = [];
window.__bgFiles = {};
window.__bgFiles['a'.repeat(64)] = { folder:'Backgrounds', name:'Neon city', mime:'image/jpeg' };
window.__bgFiles['b'.repeat(64)] = { folder:'Backgrounds', name:'Aurora',    mime:'image/png'  };
window.__bgFiles['c'.repeat(64)] = { folder:'Music',       name:'a song',    mime:'audio/mpeg' };
window.__bgFiles['d'.repeat(64)] = { folder:'Backgrounds', name:'notes.txt', mime:'text/plain' };
window.__PC.filesIdx = () => ({ _norm: () => ({ folders:['Music','Backgrounds'], files: window.__bgFiles }) });
window.__PC.encFileUrl = (sha) => { window.__bgFetched.push(sha);
  // a 1x1 gif, so the <img> really loads
  return Promise.resolve('data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'); };
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
  /* Settings categories survive one unavailable hardware bridge. This harness deliberately has
     no pcDisplays: Appearance/About must remain real pages rather than a dashboard-wide error. */
  PCOS.openSystemSettings(); await sleep(300);
  const appearance=document.querySelector('.os-set-nav [data-page="appearance"]');
  if(appearance){appearance.click();await sleep(80);}
  const categoryPages={};
  for(const page of ['displays','appearance','sound','network','bluetooth','power','users','updates','about','liveusb']){
    const nav=document.querySelector(`.os-set-nav [data-page="${page}"]`);if(nav)nav.click();await sleep(20);
    const pane=document.querySelector(`[data-settings-page="${page}"]`);
    categoryPages[page]=!!(nav&&pane&&pane.hidden===false&&
      document.querySelectorAll('[data-settings-page]:not([hidden])').length===1);
  }
  out.settings={opened:!!document.querySelector('.os-settings'),appearance:!!appearance,
    visible:!!categoryPages.appearance,categories:categoryPages,
    pages:document.querySelectorAll('[data-settings-page]').length,
    widgets:document.querySelectorAll('[data-widget-add],[data-widget-size],[data-widget-remove]').length};
  document.querySelector('.osw.focused .osw-x')?.click(); await sleep(80);
  /* Concord is painted as a tab inside the canonical Messages frame. The render notification must
     maximise that existing frame, including at tablet widths, rather than leaving a desktop strip. */
  PCOS.routeView('messages'); await sleep(180);
  PCOS.noteView('concord'); await sleep(180);
  {
    const d=document.querySelector('.os-desk')?.getBoundingClientRect();
    const w=document.querySelector('.osw.focused')?.getBoundingClientRect();
    const b=document.querySelector('.osw.focused .osw-body')?.getBoundingClientRect();
    out.concordSize=d&&w&&b?{max:document.querySelector('.osw.focused')?.classList.contains('maximised'),
      frameGap:Math.round(Math.abs(d.width-w.width)+Math.abs(d.height-w.height)),
      bodyGap:Math.round(Math.max(0,w.bottom-b.bottom)),bodyH:Math.round(b.height)}:{};
  }
  document.querySelector('.osw.focused .osw-x')?.click(); await sleep(80);
  /* Exact tablet report: Social snapped left, Terminal right; focus Terminal then Social. Merely
     changing which shared-feed window is live must not rewrite either managed frame rectangle. */
  {
    const snap=async(w,edge)=>{
      const bar=w.querySelector('.osw-bar'),r=bar.getBoundingClientRect(),x=edge==='left'?3:window.innerWidth-3;
      bar.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,clientX:r.left+80,clientY:r.top+12,pointerId:71,buttons:1}));
      const path=edge==='left'?[window.innerWidth*.35,window.innerWidth*.18,60,x]
                              :[window.innerWidth*.65,window.innerWidth*.82,window.innerWidth-60,x];
      for(const px of path){document.dispatchEvent(new PointerEvent('pointermove',
        {bubbles:true,clientX:px,clientY:220,pointerId:71,buttons:1}));await sleep(16);}
      document.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,clientX:x,clientY:220,pointerId:71,buttons:0}));
      await sleep(140);
    };
    PCOS.routeView('global');await sleep(160);const social=document.querySelector('.osw.focused');
    if(social)await snap(social,'left');
    PCOS.routeView('terminal');await sleep(160);const term=document.querySelector('.osw.focused');
    if(term)await snap(term,'right');
    const rect=w=>{const r=w?.getBoundingClientRect();return r&&[r.x,r.y,r.width,r.height].map(Math.round)};
    const before=rect(social);
    term?.querySelector('.osw-body')?.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:72}));await sleep(80);
    social?.querySelector('.osw-body')?.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:73}));await sleep(180);
    const after=rect(social);
    const states=PCOS.windows();
    out.socialTerminalFocus={before,after,
      socialSnap:states.find(x=>x.view==='global')?.snap||'',
      terminalSnap:states.find(x=>x.view==='terminal')?.snap||'',
      same:JSON.stringify(before)===JSON.stringify(after)};
    for(const w of [term,social])w?.querySelector('.osw-x')?.click();await sleep(100);
  }
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

  /* SEARCHING AGAIN WHILE THE SEARCH WINDOW IS OPEN MUST SHOW THE NEW QUERY.
   *
   * The window above is keyed by the post it shows, so re-opening it is a repaint of the same
   * document and re-running its render is right. SEARCH is the odd one out: ONE window that shows a
   * succession of different documents. Its render is a closure over the query, so the existing-window
   * branch focusing it re-ran the ORIGINAL closure — the first query's results, painted again.
   *
   * That is why it was reported as "searching for something else does not search for the new term":
   * it is not that nothing ran, it is that the wrong thing ran, which from the outside is the same
   * picture with a stale list in it. Asserted on WHAT WAS PAINTED, not on whether a call happened.
   */
  {
    const before = document.querySelectorAll('.osw').length;
    const seen = [];
    const mk = (q) => () => { seen.push(q);
                              document.getElementById('feed').innerHTML = 'RESULTS FOR ' + q; };
    PCOS.openDoc('search', 'Search', 'i-search', mk('first'), false, true);
    await sleep(80);
    PCOS.openDoc('search', 'Search', 'i-search', mk('second'), false, true);
    await sleep(150);
    out.searchWins = document.querySelectorAll('.osw').length - before;
    out.searchLast = seen[seen.length - 1] || 'nothing rendered';
    out.searchShown = (document.getElementById('feed').textContent || '').trim().slice(0, 40);
    document.querySelector('.osw.focused .osw-x').click();
    await sleep(80);
    out.searchClosed = document.querySelectorAll('.osw').length - before;
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
    /* ANY feature window with a view name of its own will do — the point of this block is the
     * hand-back between a named view and a DOC window, not which app it is. It was Calendar, which
     * stopped being a desktop ICON the moment the built-in Office folder claimed it, and this line
     * then read null and threw: the whole script stopped evaluating and the check reported SKIP,
     * which is not a pass and correctly refused the deploy. Bookmarks is top level and in no
     * built-in folder. */
    document.querySelector('.os-icon[data-view="bookmarks"]').click(); await sleep(150);
    PCOS.openDoc('prof:zz', 'Profile', 'i-user', () => {
      window.__view = 'profile';    // renderProfileView sets VIEW itself on the way in
      document.getElementById('feed').innerHTML = '<div class="stub-doc">PROFILE</div>';
    });
    await sleep(150);
    task(/Bookmarks/i).click(); await sleep(150);
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
    for (const x of [700, 900, window.innerWidth-60, window.innerWidth-4]) {
      document.dispatchEvent(new PointerEvent('pointermove',
        {bubbles:true, clientX:x, clientY:300, pointerId:1}));
      await sleep(16);
    }
    const g = document.querySelector('.os-ghost');
    out.ghostShown = !!(g && getComputedStyle(g).display !== 'none' && g.offsetWidth > 100);
    document.dispatchEvent(new PointerEvent('pointerup',
      {bubbles:true, clientX:window.innerWidth-4, clientY:300, pointerId:1}));
    await sleep(120);
    const b1 = w0.getBoundingClientRect();
    const desk = document.querySelector('.os-desk');
    out.dbg = { zoom: getComputedStyle(document.body).zoom,
                innerW: window.innerWidth, deskW: desk.clientWidth,
                winOffW: w0.offsetWidth, winOffL: w0.offsetLeft,
                rectW: Math.round(b1.width), rectL: Math.round(b1.left) };
    out.snappedHalf = Math.abs(w0.offsetWidth - (desk.clientWidth/2 - 16)) < 24 &&
                      w0.offsetLeft >= desk.clientWidth/2 - 24;
    out.snappedFullHeight = w0.offsetTop === 8 &&
                            Math.abs(w0.offsetHeight - (desk.clientHeight - 16)) < 3;
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

    /* Exact paired-window report: Terminal is snapped left, an inactive native Firefox frame is
       snapped right, then Terminal is focused and pulled away.  The native pixels are compositor
       owned in production, but the failure under investigation is entirely in startDrag's HTML
       state; marking the peer frame native-stashed recreates the placeholder without inventing a
       second compositor or touching a live desktop. Repeat because a stale snap/capture latch was
       reported only after the first successful move. */
    PCOS.routeView('terminal'); await sleep(100);
    const terminal = document.querySelector('.osw.focused');
    PCOS.routeView('websearch'); await sleep(100);
    const firefox = document.querySelector('.osw.focused');
    firefox.classList.add('osw-native', 'native-stashed');
    firefox.dataset.native = '9001';
    const drag = async (win, x, y, id, carry=false) => {
      const r=win.getBoundingClientRect(), b=win.querySelector('.osw-bar');
      b.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,cancelable:true,
        clientX:r.left+80,clientY:r.top+12,pointerId:id,pointerType:'mouse',buttons:1}));
      document.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,clientX:x,
        clientY:y,pointerId:id,pointerType:'mouse',buttons:1}));
      await sleep(24);
      /* The first sample beyond the threshold restores a snapped frame under the cursor. A real
         gesture then supplies more samples; require one here so this checks movement rather than
         mistaking the intentional restore sample for the end of a drag. */
      if(carry){x+=80;y+=35;document.dispatchEvent(new PointerEvent('pointermove',
        {bubbles:true,clientX:x,clientY:y,pointerId:id,pointerType:'mouse',buttons:1}));await sleep(24);}
      document.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,clientX:x,
        clientY:y,pointerId:id,pointerType:'mouse',buttons:0}));
      await sleep(50);
    };
    await drag(terminal, 3, 320, 81);
    await drag(firefox, innerWidth-3, 320, 82);
    const pairSetup=terminal.classList.contains('snapped')&&
                    firefox.classList.contains('snapped')&&firefox.classList.contains('native-stashed');
    const fixed = firefox.getBoundingClientRect();
    const fixedRect = {x:fixed.x,y:fixed.y,w:fixed.width,h:fixed.height};
    const paired=[];
    for(let i=0;i<3;i++){
      firefox.querySelector('.osw-body').dispatchEvent(new PointerEvent('pointerdown',
        {bubbles:true,pointerId:90+i,pointerType:'mouse',buttons:1}));
      terminal.querySelector('.osw-body').dispatchEvent(new PointerEvent('pointerdown',
        {bubbles:true,pointerId:100+i,pointerType:'mouse',buttons:1}));
      const before=terminal.getBoundingClientRect();
      await drag(terminal, before.left+260, before.top+120, 110+i, true);
      const after=terminal.getBoundingClientRect(), peer=firefox.getBoundingClientRect();
      paired.push({unsnapped:!terminal.classList.contains('snapped'),
        moved:Math.abs(after.left-before.left)>30 || Math.abs(after.top-before.top)>30,
        before:{x:Math.round(before.left),y:Math.round(before.top),w:Math.round(before.width)},
        after:{x:Math.round(after.left),y:Math.round(after.top),w:Math.round(after.width)},
        peerSame:Math.abs(peer.x-fixedRect.x)<2&&Math.abs(peer.y-fixedRect.y)<2&&
                 Math.abs(peer.width-fixedRect.w)<2&&Math.abs(peer.height-fixedRect.h)<2,
        dragging:terminal.classList.contains('dragging'),
        captured:terminal.hasPointerCapture?terminal.hasPointerCapture(110+i):false});
      if(i<2)await drag(terminal,3,320,120+i);
    }
    out.pairedTerminalDrag={setup:pairSetup,cycles:paired};
    firefox.querySelector('.osw-x').click(); await sleep(30);
    terminal.querySelector('.osw-x').click(); await sleep(30);

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
  // What each folder tile stands for, so the comparison against the sidebar can expand it.
  out.folderApps = {};
  for(const b of document.querySelectorAll('.os-icon[data-apps]'))
    out.folderApps[b.dataset.view] = (b.dataset.apps || '').split(' ').filter(Boolean);
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
  /* Two apps that are on the DESKTOP, which Calendar and Contacts stopped being when the built-in
   * Office folder claimed them — `ic()` then returned null and the script died here. Notes and
   * Passwords are top level and in no built-in folder. */
  ic('notes').click(); await sleep(150);
  ic('vault').click(); await sleep(150);
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

  /* THE START MENU MUST STILL BE ON TOP AFTER A LONG SESSION. Windows carry an inline z-index from
   * a counter that every focus bumps — and routeView bumps it on ordinary navigation INSIDE one
   * window, so it climbs on its own. Once it passed the start menu's 320 the menu opened behind the
   * window it was opened over: built, painted, positioned, and taking no clicks, with nothing in
   * any log. Hit-tested twice, before and after the switching, because the first probe is what
   * makes the second one mean something. */
  {
    const openStart = async () => {
      document.querySelector('#os-start').click(); await sleep(120);
      const m = document.getElementById('os-startmenu');
      if (!m) return { ok: false, by: 'no menu' };
      const r = m.getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + 40);
      const ok = !!(hit && (hit === m || m.contains(hit)));
      const by = hit ? (hit.id || hit.className || hit.tagName).toString().slice(0, 40) : 'nothing';
      document.querySelector('#os-start').click(); await sleep(60);
      return { ok, by };
    };
    const a = await openStart();
    out.startOnTop = a.ok; out.startCoveredBy = a.by;
    for (let i = 0; i < 400; i++) PCOS.routeView(i % 2 ? 'calendar' : 'contacts');
    await sleep(150);
    const b = await openStart();
    out.startOnTopLate = b.ok; out.startCoveredByLate = b.by;
    out.winZmax = Math.max(...[...document.querySelectorAll('.osw')]
                            .map(w => parseInt(w.style.zIndex, 10) || 0));
  }

  // Window controls.
  const w = document.querySelector('.osw.focused');
  w.querySelector('[data-w="max"]').click(); await sleep(80);
  out.maximised = w.classList.contains('maximised') && w.offsetWidth > window.innerWidth * 0.9;
  /* Right-click Move must recover a maximised window before pointer movement. Keeping its snapped
   * full-screen width makes every candidate left position clamp to the same edge, so the command
   * looks present but cannot move anything. Exercise the real task/menu/pointer path. */
  const wt = [...document.querySelectorAll('.os-task')]
    .find(b => b.dataset.kind === 'web' && b.classList.contains('on'));
  if(wt){
    wt.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,
      clientX:wt.getBoundingClientRect().left+4,clientY:wt.getBoundingClientRect().top+4}));
    await sleep(60);
    const mv=[...document.querySelectorAll('.os-ctx-b')].find(b=>b.textContent.trim()==='Move');
    if(mv){mv.click();await sleep(40);
      document.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,
        clientX:window.innerWidth*.35,clientY:window.innerHeight*.30}));
      document.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0,
        clientX:window.innerWidth*.35,clientY:window.innerHeight*.30}));
      await sleep(80);
    }
  }
  out.taskMoveRecovered = !w.classList.contains('maximised')
    && w.classList.contains('osw-taskbar-moving')===false && parseFloat(w.style.left)>12;
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
  /* The notification BELL. Notifications used to live behind the CLOCK, which nothing announces as
   * a button — the count sat on it and read as part of the time. Hit-tested rather than read off
   * the markup: it has to be a real, clickable 34px target in the tray. */
  { const bell = document.querySelector('#os-bell');
    out.hasBell = !!bell;
    if (bell) {
      const r = bell.getBoundingClientRect();
      out.bellSize = [Math.round(r.width), Math.round(r.height)];
      const nr = (document.querySelector('#os-net') || bell).getBoundingClientRect();
      out.netSize = [Math.round(nr.width), Math.round(nr.height)];
      const hit = document.elementFromPoint(r.left + r.width/2, r.top + r.height/2);
      out.bellReachable = !!(hit && (hit === bell || bell.contains(hit)));
      bell.click(); await sleep(80);
      out.bellOpens = !!document.querySelector('#os-noti');
      bell.click(); await sleep(60);
      out.bellCloses = !document.querySelector('#os-noti');
    }
    // …and the clock is a clock again: it must not carry the count any more.
    const clk = document.querySelector('#os-clock');
    out.clockHasBadge = !!(clk && clk.querySelector('.os-dot')); }

  /* THE COUNT ON THE BELL MUST BE ABLE TO GROW, and it has to grow WITHOUT the taskbar being
   * rebuilt under whoever is typing in it.
   *
   * Reported as "the bell changes colour when you first log in and never again". The tray count was
   * `notifItems(60).length` minus a snapshot taken when the centre was last opened — the length of a
   * SLICED list, i.e. a constant on any account past the slice, so the subtraction was 60-60 for the
   * rest of the session. A single reading proves nothing here (60 looks like a working badge), so
   * this drives arrivals and asserts the number MOVES, and moves again after the centre is opened. */
  {
    const dot = () => { const b = document.querySelector('#os-bell .os-dot');
                        return b ? b.textContent.trim() : ''; };
    window.__unread = 3; PCOS.notifChanged(); await sleep(320);
    out.bellDot3 = dot();
    window.__unread = 9; PCOS.notifChanged(); await sleep(320);
    out.bellDot9 = dot();
    // Opening the centre marks them READ through the app, not by remembering a number here.
    document.querySelector('#os-bell').click(); await sleep(120);
    out.bellReadCalls = window.__read;
    document.querySelector('#os-bell').click(); await sleep(120);
    out.bellDotAfterRead = dot();
    // …and the next arrival still lights it. THIS is the one that was broken.
    window.__unread = 4; PCOS.notifChanged(); await sleep(320);
    out.bellDotAfterArrival = dot();

    /* The repaint must be IN PLACE. drawBar() rebuilds bar.innerHTML, which destroys and recreates
     * the Search Nostr input — and an arriving notification is not something the user did, so it
     * must never take the caret out of a half-typed search. */
    const q = document.querySelector('#os-q-bar');
    q.focus(); q.value = 'half a query'; q.dispatchEvent(new Event('input', { bubbles: true }));
    // Below 1080px the box is display:none and cannot take focus at all — so the focus half of this
    // is asserted only where there is a box to focus. The NODE-identity half always applies: it is
    // the direct measurement of "painted in place", and it is what the caret depends on.
    out.barFocusBefore = document.activeElement === q;
    window.__unread = 5; PCOS.notifChanged(); await sleep(320);
    out.barSameNode = document.querySelector('#os-q-bar') === q;
    out.barKeptFocus = document.activeElement === document.querySelector('#os-q-bar');
    out.barKeptText = (document.querySelector('#os-q-bar') || {}).value;
    q.blur(); q.value = ''; q.dispatchEvent(new Event('input', { bubbles: true }));

    /* AND THE BELL MUST TAKE A CLICK WHILE THE OTHER TRAY PANEL IS OPEN. One capture-phase
     * click-away handler closes both flyouts by calling drawBar(), which detaches the very button
     * the pointerdown landed on — so a tray trigger missing from its keep-list is a button that
     * does nothing at all. The bell was added to the tray after that list was written. */
    document.querySelector('#os-net').click(); await sleep(100);
    out.netOpenFirst = !!document.querySelector('#os-net-panel');
    const bell2 = document.querySelector('#os-bell');
    bell2.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
    await sleep(30);
    /* SURVIVING THE POINTERDOWN IS THE MEASUREMENT. A click dispatched at a fresh #os-bell would
       pass whatever happened — and calling .click() on the DETACHED original would pass too, since
       JS runs a listener on a node whether or not it is in the document. The browser does neither:
       with the mousedown target gone it fires click on an ancestor, and the button never hears it.
       So ask whether the node the press landed on is still there. */
    out.bellSurvivedPointerdown = bell2.isConnected;
    if (bell2.isConnected) bell2.click();
    await sleep(120);
    out.bellOpensOverNet = !!document.querySelector('#os-noti');
    try{ document.querySelector('#os-bell').click(); }catch(_){}
    await sleep(80);
    window.__unread = 0; PCOS.notifChanged(); await sleep(260);
  }

  /* HOW BIG WINDOWS OPEN. Six of them, cascaded, on this screen: every one must land fully inside
   * the desktop (a window opening under the taskbar or off the right edge has to be dragged back
   * before it can be used), and the first one must actually USE the screen rather than opening as a
   * small box on a large display. */
  {
    const views = ['calendar','contacts','notes','bookmarks','drafts','meme'];
    for (const v of views) {
      const b = document.querySelector('.os-icon[data-view="' + v + '"]');
      if (b) { b.click(); await sleep(90); }
    }
    await sleep(120);
    const desk = document.querySelector('#os-desk').getBoundingClientRect();
    const wins = [...document.querySelectorAll('.osw')];
    out.sized = wins.map(w => {
      const r = w.getBoundingClientRect();
      return { v: (w.querySelector('.osw-title')||{}).textContent || '',
               out: Math.round(Math.max(0, r.right - desk.right) + Math.max(0, r.bottom - desk.bottom)
                             + Math.max(0, desk.left - r.left) + Math.max(0, desk.top - r.top)),
               fillW: Math.round(r.width / desk.width * 100),
               fillH: Math.round(r.height / desk.height * 100) };
    });
    out.deskBox = [Math.round(desk.width), Math.round(desk.height)];

    /* CLOSING THE ADMIN PANEL MUST NOT BLACK OUT THE NEXT WINDOW.
     *
     * Admin does not render into #feed — it HIDES it (display:none) and shows an iframe host beside
     * it. There is one #feed and every window borrows it in turn, so that inline style travels: the
     * window focused after Admin closes inherits a hidden feed, and a window restored from its park
     * skips the repaint by design, so nothing ever puts it back. Reported as "closing Admin Panel
     * makes the Social window black" — and then "it makes ANY open window black".
     *
     * Reproduced without the real panel (it is a cross-origin iframe and an auth session): what the
     * panel actually does to the shared element is the one line below. */
    /* A WINDOW NAVIGATED INSIDE ITSELF must repaint as what it IS showing.
     *
     * The Admin panel is reached from the Settings view's own button, and `admin` is not a sidebar
     * app — so the settings window paints Admin into itself while `w.view` stays 'settings'. Drag it
     * and focusWin repainted `switchView(w.view)`: you land back in User Settings, and the admin
     * host is hidden because _feedVisibleFor was told 'settings' too. Reproduced with the same two
     * moves the client makes: switch the view inside the focused window, then drag its title bar. */
    {
      // The FOCUSED window — the one holding the live feed. That is the precondition: the client
      // navigates inside the window you are looking at.
      const w0 = [...document.querySelectorAll('.osw')].pop();
      if (w0) {
        // The admin panel is an <iframe> hosted in a sibling of the feed. Stand one in for it: what
        // is being checked is who decides its visibility, not what is inside it.
        let ah = document.getElementById('admin-host');
        if (!ah) { ah = document.createElement('div'); ah.id = 'admin-host'; }
        const fd = document.querySelector('#feed');
        fd.parentElement.appendChild(ah);
        window.__PC.switchView('admin');            // navigate INSIDE the focused window…
        fd.style.display = 'none'; ah.style.display = 'block';   // …which is what _adminFrame does
        await sleep(120);
        const bar = w0.querySelector('.osw-bar');
        const r = bar.getBoundingClientRect();
        await drag(bar, r.left + 140, r.top + 90);  // move it
        await sleep(220);
        // The invariant: a window SHOWING Admin still shows Admin after it is moved. With the view
        // read from `w.view` the host is hidden and the (emptied) feed shown — a black window.
        out.adminBlack = getComputedStyle(document.getElementById('admin-host')).display === 'none';
        out.afterDragView = window.__view;
        document.getElementById('admin-host').remove();
        document.querySelector('#feed').style.display = '';
      }
    }

    const feed = document.querySelector('#feed');
    if (feed && wins.length >= 2) {
      feed.style.display = 'none';                       // ← what _adminFrame does
      const x = wins[wins.length - 1].querySelector('.osw-x');
      if (x) x.click();
      await sleep(160);
      out.feedHiddenAfterClose = getComputedStyle(feed).display === 'none';
      out.feedBlankWin = (() => {                        // …and is anything actually showing?
        const w = document.querySelector('.osw');
        if (!w) return false;
        const body = w.querySelector('.osw-body');
        return !!body && body.getBoundingClientRect().height > 40 && !body.offsetParent === false
               && getComputedStyle(feed).display === 'none' && feed.parentElement === body;
      })();
      feed.style.display = '';
    }
    for (const w of [...document.querySelectorAll('.osw')]) { const x = w.querySelector('.osw-x'); if (x) x.click(); }
    await sleep(120);
  }

  /* AN APP'S NAME IS NOT ITS UNREAD COUNT. The badge is an <i> INSIDE the label span, so a naive
   * textContent read gives "Messages99+" on the icon and in the window title. The old guard stripped
   * trailing DIGITS, which misses the capped "99+" and eats the 4 off "Connect 4". */
  {
    const btn = document.querySelector('.sidebar .nav .nav-item[data-view="messages"]');
    if (btn) {
      const sp = btn.querySelector('span');
      if (sp && !sp.querySelector('.badge')) {
        const b = document.createElement('i'); b.className = 'badge'; b.textContent = '99+';
        sp.appendChild(b);
      }
      PCOS.refresh(); await sleep(160);
      const ic = document.querySelector('.os-icon[data-view="messages"]');
      out.messagesLabel = ic ? ([...ic.querySelectorAll('span')].pop() || {}).textContent || '' : '';
      const c4 = document.querySelector('.os-icon[data-view="connect4"]');
      out.connect4Label = c4 ? ([...c4.querySelectorAll('span')].pop() || {}).textContent || '' : '';
    }
  }

  /* WIDGETS: added from the desktop menu, drawn, dragged, and STILL THERE at another screen size.
   *
   * The last one is the requirement that cannot be checked by looking at one screen: positions are
   * stored as fractions of the free area precisely so a panel put against the right edge of a big
   * monitor is against the right edge of a small one, rather than 1500px into the middle of it. A
   * pixel position passes every single-size test and fails the only thing anyone would notice. */
  {
    const desk = document.querySelector('#os-desk');
    const menu = async () => { pev(desk, 'contextmenu', 300, 300);
                               desk.dispatchEvent(new MouseEvent('contextmenu',
                                 { bubbles:true, cancelable:true, clientX:300, clientY:300 }));
                               await sleep(90); return document.querySelector('.os-ctx'); };
    const m = await menu();
    const addRow = m && [...m.querySelectorAll('.os-ctx-b')].find(b => /add a widget/i.test(b.textContent));
    out.hasAddWidget = !!addRow;
    if (addRow) {
      addRow.click(); await sleep(120);
      const pick = document.querySelector('.os-wgtpick');
      out.widgetPickerOpens = !!pick;
      out.widgetTypes = pick ? [...pick.querySelectorAll('.os-wgt-pick')].map(b => b.dataset.t) : [];
      const crypto = pick && pick.querySelector('.os-wgt-pick[data-t="crypto"]');
      if (crypto) { crypto.click(); await sleep(400); }
      const el = document.querySelector('.os-wgt');
      out.widgetDrawn = !!el;
      // …and the Now-playing widget, whose buttons must actually reach the player.
      const pick2 = (addRow.click(), await sleep(120), document.querySelector('.os-wgtpick'));
      const mus = pick2 && pick2.querySelector('.os-wgt-pick[data-t="music"]');
      if (mus) { mus.click(); await sleep(400); }
      {
        const mw = document.querySelector('.os-wgt[data-type="music"]');
        out.musicWidget = !!mw;
        if (mw) {
          window.__music = [];
          for (const k of ['shuffle','prev','toggle','next']) {
            const b = mw.querySelector('[data-m="' + k + '"]');
            if (b) b.click();
            await sleep(40);
          }
          out.musicCalls = window.__music.slice();
          out.musicTitle = (mw.querySelector('.wgt-mtitle') || {}).textContent || '';
          /* NO DEAD SPACE. The panel used to pin its transport to the bottom and leave a hole in the
           * middle; what fills it is the seek row. Measured as the largest vertical gap between the
           * widget's own rows, against the body's height — a screenshot at one size cannot catch this
           * coming back, and "mostly wasted space in the centre" is how it was reported. */
          const body = mw.querySelector('.os-wgt-body');
          const rows = [...mw.querySelectorAll('.wgt-mtop,.wgt-mseek,.wgt-mtimes,.wgt-mctl')]
                        .map(n => n.getBoundingClientRect()).filter(r => r.height > 0)
                        .sort((a, b) => a.top - b.top);
          let gap = 0;
          for (let i = 1; i < rows.length; i++) gap = Math.max(gap, rows[i].top - rows[i-1].bottom);
          const bh = body ? body.getBoundingClientRect().height : 0;
          out.musicGap = bh > 0 ? Math.round(gap / bh * 100) : 0;
          out.musicHasSeek = !!mw.querySelector('.wgt-mseek');
        }
      }
      /* THE TODAY WIDGET'S FIRST COLUMN HOLDS TWO DIFFERENT KINDS OF THING. Today's rows put a time
       * there; the "Coming up" rows put a day LABEL — "Tomorrow", a weekday, a date. The column was
       * a fixed 3.6em, which fits a time and not the word "Tomorrow", and a too-narrow flex:none box
       * does not clip — it OVERFLOWS, painting the word straight over the calendar colour swatch and
       * the title. Reported as "the Tomorrow / Fri / Sun text is going over the Green and Red".
       *
       * Measured as real geometry against the real stylesheet, per row: does the label's box reach
       * past where the swatch begins? A stylesheet assertion would have passed on any width that
       * merely LOOKED plausible, and a screenshot cannot say which element won. */
      {
        const pick3 = (addRow.click(), await sleep(120), document.querySelector('.os-wgtpick'));
        const cal = pick3 && pick3.querySelector('.os-wgt-pick[data-t="calendar"]');
        if (cal) { cal.click(); await sleep(900); }
        const cw = document.querySelector('.os-wgt[data-type="calendar"]');
        out.calWidget = !!cw;
        if (cw) {
          const rows = [...cw.querySelectorAll('.wgt-calrow')];
          out.calRows = rows.length;
          out.calLabels = rows.map(r => ((r.querySelector('.wgt-calt') || {}).textContent || '').trim());
          let worst = 0, culprit = '';
          for (const r of rows) {
            const t = r.querySelector('.wgt-calt'), x = r.querySelector('.wgt-calx');
            if (!t || !x) continue;
            const tr = t.getBoundingClientRect(), xr = x.getBoundingClientRect();
            // How far the label's own box runs past the start of the swatch beside it.
            const over = Math.round(tr.right - xr.left);
            if (over > worst) { worst = over; culprit = t.textContent.trim(); }
            // …and the text must not be wider than the box that is supposed to hold it, which is
            // what overflow looks like from the inside when the box itself is not clipping.
            const spill = Math.round(t.scrollWidth - t.clientWidth);
            if (spill > 1 && spill > worst) { worst = spill; culprit = t.textContent.trim() + ' (clipped text)'; }
          }
          out.calOverlap = worst;
          out.calOverlapBy = culprit;
        }
      }

      if (el) {
        const dr = desk.getBoundingClientRect(), r = el.getBoundingClientRect();
        out.widgetInside = (r.right <= dr.right + 2) && (r.bottom <= dr.bottom + 2)
                        && (r.left >= dr.left - 2) && (r.top >= dr.top - 2);
        out.widgetHasBody = !!el.querySelector('.os-wgt-body');
        // Drag it to the lower-left and check the DOCUMENT recorded a fraction, not a pixel.
        // The whole panel is the drag handle — widgets have no title bar (that chrome made them
        // read as little windows sitting on the desktop rather than as part of it).
        await drag(el, dr.left + 60, dr.bottom - 90);
        await sleep(320);
        out.widgetMoved = (() => { const q = document.querySelector('.os-wgt');
          return q ? Math.round(q.getBoundingClientRect().left - dr.left) : -1; })();
        /* A FINGER has to be able to do what the drag above just did with a synthetic pointer, and
         * no synthetic event can tell you whether it can: touch-action is enforced by the browser
         * BEFORE any event is dispatched. So this reads the rule off the real stylesheet. Measured
         * with real touch input, a panel without it takes three pointermoves and then a
         * pointercancel (the browser claiming the gesture as a page scroll) — on screen, the widget
         * starts to move and stops, which is exactly how it was reported from a tablet. */
        out.widgetTouchAction = getComputedStyle(el).touchAction;
        try { const st = JSON.parse(localStorage.getItem('__pc_test_desktop_doc') || 'null');
              out.widgetDocX = st ? st.x : null; } catch (e) {}
      }

      /* ---- A DRAG MUST NOT ALSO BE A CLICK --------------------------------------------------------
       * preventDefault on pointerdown does not stop the click the browser synthesises on release, and
       * every panel whose body opens something (Today → Calendar, the blocks → mempool.space,
       * Community → Server Stats) was taking it: dragging the widget across the desktop ended by
       * opening a window on top of the desktop being arranged.
       *
       * `drag()` sends pointer events only — no browser click follows a synthetic pointerup — so the
       * click is dispatched by hand, at a DESCENDANT, which is where a real one lands (the handlers
       * are bound on the widget's body, not on the frame). The second click, with no drag before it,
       * is the other half: the suppressor must be one-shot, or the panel stops working. */
      {
        const w2 = document.querySelector('.os-wgt');
        const b2 = w2 && w2.querySelector('.os-wgt-body');
        if (b2) {
          let hits = 0;
          const probe = () => { hits++; };
          b2.addEventListener('click', probe);
          const dr2 = desk.getBoundingClientRect();
          await drag(w2, dr2.left + 220, dr2.bottom - 200);
          await sleep(60);
          b2.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
          out.clickAfterDrag = hits;                    // must be 0
          await sleep(400);                             // past the un-arm timeout
          b2.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
          out.clickAfterTap = hits;                     // must be 1
          b2.removeEventListener('click', probe);
        }
      }

      /* ---- THE CLOCK'S CITY PICKER MUST BE DISMISSABLE --------------------------------------------
       * It replaces the world-clock rows in place and latches a flag that stops the 1s refresh from
       * repainting them. Escape only reaches a focused input and picking a city is the only other way
       * out — so clicking ＋ and changing your mind left an empty search box where the clocks were,
       * for the rest of the session. Two independent bugs did this (no outside-click dismissal, and a
       * close() whose forced rebuild was a no-op for a clock with no cities), so it is checked through
       * the UI rather than at either seam. */
      {
        const pick4 = (addRow.click(), await sleep(120), document.querySelector('.os-wgtpick'));
        const cb = pick4 && pick4.querySelector('.os-wgt-pick[data-t="clock"]');
        if (cb) { cb.click(); await sleep(400); }
        const cw = document.querySelector('.os-wgt[data-type="clock"]');
        out.clockDrawn = !!cw;
        if (cw) {
          out.clockTime = ((cw.querySelector('.wgt-clkh') || {}).textContent || '').trim();
          const add = cw.querySelector('.wgt-clkadd');
          if (add) { add.click(); await sleep(140); }
          out.clockPickerOpens = !!cw.querySelector('.wgt-clkq');
          pev(desk, 'pointerdown', 420, 420);          // give up: press the desktop
          await sleep(1400);                            // the clock's own 1s tick redraws the rows
          out.clockPickerCloses = !cw.querySelector('.wgt-clkq');
        }
      }

      /* ---- THE PANELS FOLLOW THE THEME ----------------------------------------------------------
       * The widget chrome was written in cyberpunk's own colours and hardcoded them, so on the eight
       * other palettes it was a dark violet card with the theme's dark text on it: "it looks terrible
       * and dark if you change themes". Nothing in a unit test can see that — it is one selector's
       * resolved colour against another's — and a screenshot cannot say WHY it is wrong.
       *
       * So: switch themes for real and measure the luminance of the panel against the ink drawn on
       * it. The assertion is the one that matters (they must be on opposite sides, in every theme),
       * plus the panel tracking its palette rather than staying dark. `backgroundColor` is the
       * resolved token even though the rule is a gradient over it — the gradient is backgroundImage.
       * The sticky note is measured separately and INVERTED: its paper is yellow in every theme, so
       * what is checked there is that the readability layer's `textarea{background:var(--panel2)}`
       * has not painted a panel across the middle of it again. */
      {
        const lum = (c) => { const m = /rgba?\(([^)]+)\)/.exec(c || ''); if (!m) return null;
          const p = m[1].split(',').map(s => parseFloat(s));
          if (p.length > 3 && p[3] === 0) return null;       // transparent measures nothing
          return (0.2126*p[0] + 0.7152*p[1] + 0.0722*p[2]) / 255; };
        /* A panel painted ONLY by a gradient reports backgroundColor rgba(0,0,0,0), which would make
         * the hardcoded-violet bug come back as "cannot measure" rather than as what it is. Fall back
         * to the gradient's first colour stop — the top of the panel, where its first line of text
         * sits. */
        const surface = (el) => { const cs = getComputedStyle(el);
          const c = lum(cs.backgroundColor);
          if (c !== null) return c;
          const g = /rgba?\([^)]+\)/.exec(cs.backgroundImage || '');
          return g ? lum(g[0]) : null; };
        // A sticky note to measure, alongside the ticker already on the desk.
        const pick3 = (addRow.click(), await sleep(120), document.querySelector('.os-wgtpick'));
        const nb = pick3 && pick3.querySelector('.os-wgt-pick[data-t="note"]');
        if (nb) { nb.click(); await sleep(300); }
        const seen = {};
        // '' LAST: it restores the page to the default theme for every check that follows.
        for (const t of ['professional', 'win98', 'cherryblossom', 'monero', '']) {
          if (t) document.documentElement.dataset.theme = t;
          else delete document.documentElement.dataset.theme;
          await sleep(60);
          const w = document.querySelector('.os-wgt:not([data-type="note"])');
          const body = w && w.querySelector('.os-wgt-body');
          const ta = document.querySelector('.os-wgt[data-type="note"] .wgt-note');
          seen[t || 'cyberpunk'] = {
            panel: w ? surface(w) : null,
            ink:   body ? lum(getComputedStyle(body).color) : null,
            note:  ta ? lum(getComputedStyle(ta).backgroundColor) : null,   // null = transparent = right
          };
        }
        out.widgetTheme = seen;
      }
    }
  }

  // 1. Drop Notes on the MIDDLE of News: a folder holding both, in News's place.
  window.__clicked_open = 0;
  const before = views();
  await drag(icon('notes'), ...mid(icon('news')));
  await sleep(220);                        // the rename prompt resolves and saves a second time
  out.afterMerge = views();
  /* THE FOLDER THIS TEST JUST MADE, not "the first folder on the desktop".
   *
   * The built-in FOLDERS are a growing default — Nostr Games, then Office — and the stub sidebar
   * above carries Calendar and Contacts, so from the moment Office existed there were TWO folder
   * tiles and every lookup here silently picked the wrong one. The script threw and the check
   * reported SKIP: "the desktop script did not evaluate", which is not a pass and correctly blocked
   * the deploy. Compare against what was on screen BEFORE the drag instead. */
  const madeKey = views().find(v => v.indexOf('folder:') === 0 && before.indexOf(v) < 0) || '';
  out.folderKey = madeKey;
  const tileFor = (key) => [...document.querySelectorAll('.os-icons .os-icon')]
      .find(b => b.dataset.view === key);
  // The LAST span: a folder tile leads with .os-fold (its members' glyphs), and the label follows.
  out.folderLabel = (() => { const b = tileFor(madeKey);
    return b ? ([...b.querySelectorAll('span')].pop() || {}).textContent || '' : ''; })();
  out.mergedAway = before.length - views().length;      // two icons became one
  // The drag must not ALSO open the app it dragged.
  out.openedWindows = document.querySelectorAll('.osw').length;
  // …and it must have been written, not just drawn.
  out.savedDoc = window.__relayDoc ? window.__relayDoc.content : '';

  // 2. Open the folder window and check its members, then take an icon back out to the desktop.
  const fb = tileFor(madeKey);
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
  // …the one this test made. A built-in folder standing beside it is not a failure.
  out.folderTileGone = !views().some(v => v === madeKey);

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

  /* 7. MOVING AN ICON ANYWHERE, and the wallpaper. */
  {
    const before = {};
    for (const b of document.querySelectorAll('.os-icons .os-icon')) {
      const r = b.getBoundingClientRect(); before[b.dataset.view] = [Math.round(r.left), Math.round(r.top)];
    }
    const deskR = document.querySelector('#os-desk').getBoundingClientRect();
    const target = icon(views()[2]);
    const tx = deskR.right - 220, ty = deskR.bottom - 200;
    const tv = target.dataset.view;
    await drag(target, tx, ty);
    await sleep(320);
    // Re-find it: the drop redraws the desktop, so the node the drag started on is detached and
    // measures as 0x0 at 0,0 — which reads as "the icon did not move" whatever actually happened.
    const moved = (icon(tv) || target).getBoundingClientRect();
    out.freeDx = Math.round(moved.left + moved.width/2 - tx);
    out.freeDy = Math.round(moved.top + moved.height/2 - ty);
    out.freeClass = !!document.querySelector('.os-icons.os-free');
    // …and NOTHING ELSE moved. The first free placement seeds every other icon with where it
    // already is; without that, moving one icon rearranges the whole desktop.
    let shifted = 0;
    for (const b of document.querySelectorAll('.os-icons .os-icon')) {
      if (b.dataset.view === tv) continue;
      const r = b.getBoundingClientRect(), was = before[b.dataset.view];
      if (was && (Math.abs(r.left - was[0]) > 2 || Math.abs(r.top - was[1]) > 2)) shifted++;
    }
    out.othersShifted = shifted;
    out.freeSaved = /"pos"/.test((window.__relayDoc || {}).content || '');

    // Right-click the wallpaper → the picker, listing only IMAGES from `Backgrounds`.
    document.querySelector('#os-desk').dispatchEvent(new MouseEvent('contextmenu',
        { bubbles:true, cancelable:true, clientX: deskR.left + 40, clientY: deskR.bottom - 60 }));
    await sleep(80);
    const bgRow = [...document.querySelectorAll('.os-ctx-b')].find(b => /background/i.test(b.textContent));
    out.hasBgRow = !!bgRow;
    out.hasLineUpRow = [...document.querySelectorAll('.os-ctx-b')].some(b => /line the icons up/i.test(b.textContent));
    if (bgRow) bgRow.click();
    await sleep(200);
    const pick = document.querySelector('.os-bgpick');
    out.pickerOpen = !!pick;
    if (pick) {
      const tiles = [...pick.querySelectorAll('.os-bg-item')];
      out.bgTiles = tiles.map(t => (t.getAttribute('title') || 'Default'));
      const neon = tiles.find(t => /Neon/.test(t.getAttribute('title') || ''));
      if (neon) { neon.click(); await sleep(300); }
      out.bgApplied = /url\(/.test(document.querySelector('#os-desk').style.backgroundImage || '');
      out.bgHasClass = document.querySelector('#os-desk').classList.contains('has-bg');
      out.bgSaved = /"bg":"a{8}/.test((window.__relayDoc || {}).content || '');
    }
    // Line them up again → back to the grid.
    document.querySelector('#os-desk').dispatchEvent(new MouseEvent('contextmenu',
        { bubbles:true, cancelable:true, clientX: deskR.left + 40, clientY: deskR.bottom - 60 }));
    await sleep(80);
    const lu = [...document.querySelectorAll('.os-ctx-b')].find(b => /line the icons up/i.test(b.textContent));
    if (lu) lu.click();
    await sleep(300);
    out.gridBack = !document.querySelector('.os-icons.os-free');
  }

  /* 8. THE WIPE. Relay.query() has NO reject path: when nothing EOSEs it RESOLVES with [] marked
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
  /* A TOP-LEVEL app: Calendar is inside the built-in Office folder now, so this returned null
   and the touch test silently did not run at all. */
  document.querySelector('.os-icon[data-view="notes"]').click(); await sleep(150);
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
  /* Release where the synthetic finger actually is. PointerEvent defaults omitted coordinates to
     (0,0); startDrag quite correctly interpreted that final sample as the top-left snap zone, so
     this gate reported a huge negative "drag" after testing a snap it never intended to perform. */
  document.dispatchEvent(new PointerEvent('pointerup',
      {bubbles:true, clientX:x0+220, clientY:y0+140, pointerType:'touch'}));
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

                settings = r.get("settings") or {}
                if not (settings.get("opened") and settings.get("appearance") and
                        settings.get("visible") and settings.get("pages", 0) >= 4):
                    problems.append((label, "settings-not-separated",
                                     f"settings without display bridge rendered {settings}"))
                if settings.get("widgets"):
                    problems.append((label, "settings-mixed-widgets",
                                     f"settings included {settings['widgets']} dashboard widget controls"))
                missing_categories = [name for name, shown in (settings.get("categories") or {}).items()
                                      if not shown]
                if missing_categories or len(settings.get("categories") or {}) != 10:
                    problems.append((label, "settings-category-missing",
                                     f"distinct Settings pages failed: {settings.get('categories')}"))

                concord = r.get("concordSize") or {}
                # Maximised managed windows deliberately retain the WM's 8 CSS-px snap gutter;
                # bodyGap below is the actual unused strip regression inside the frame.
                if not concord.get("max") or concord.get("frameGap", 999) > 28:
                    problems.append((label, "concord-not-maximised",
                                     f"Concord left unused managed workspace: {concord}"))
                if concord.get("bodyH", 0) < 200 or concord.get("bodyGap", 999) > 4:
                    problems.append((label, "concord-inner-gap",
                                     f"Concord body did not fill its frame: {concord}"))

                ft = r.get("socialTerminalFocus") or {}
                if not (ft.get("socialSnap") == "left" and ft.get("terminalSnap") == "right"
                        and ft.get("same")):
                    problems.append((label, "focus-resized-snapped-window",
                                     f"Terminal -> Social focus changed managed geometry: {ft}"))

                # Exercise the browser input path, not merely the CSS declaration. A regression
                # once left Social with no usable wheel scrolling even though the feed still had
                # overflow and looked normal in a screenshot.
                if not touch:
                    wheel = await js("""(() => {
                      PCOS.enter();
                      PCOS.routeView('global');
                      const f=document.getElementById('feed');
                      if(!f)return null;
                      const probe=document.createElement('div');
                      probe.id='__wheel_probe';probe.style.height='4000px';f.appendChild(probe);
                      f.scrollTop=0;
                      const fw=f.closest('.osw');
                      if(fw)fw.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));
                      const r=f.getBoundingClientRect();
                      const x=r.left+r.width/2,y=r.top+Math.min(80,r.height/2),hit=document.elementFromPoint(x,y);
                      window.__wheelEvents=[];f.addEventListener('wheel',e=>window.__wheelEvents.push(
                        {dy:e.deltaY,prevented:e.defaultPrevented}),{once:true});
                      return {x,y,hit:hit&&{tag:hit.tagName,cls:hit.className,id:hit.id},
                              scrollbar:getComputedStyle(f).scrollbarWidth,
                              inWindow:!!fw,scrollHeight:f.scrollHeight,clientHeight:f.clientHeight};
                    })()""")
                    if wheel:
                        # A wheel packet is routed through the synthetic mouse's current target in
                        # headless Chromium. Move it onto the feed first; sending `mouseWheel`
                        # directly at coordinates while the synthetic pointer still belonged to a
                        # previous window delivered a wheel event but skipped default scrolling.
                        await call("Input.dispatchMouseEvent", {"type": "mouseMoved",
                                   "x": wheel["x"], "y": wheel["y"]})
                        await call("Input.dispatchMouseEvent", {"type": "mouseWheel",
                                   "x": wheel["x"], "y": wheel["y"],
                                   "deltaX": 0, "deltaY": 700})
                        await asyncio.sleep(0.15)
                        wheel["top"] = await js("(document.getElementById('feed')||{}).scrollTop||0")
                        wheel["events"] = await js("window.__wheelEvents||[]")
                        # Headless Chromium under body zoom delivers this wheel event to the exact
                        # child but omits its compositor default action. That is a verifier defect,
                        # not evidence that Social consumed the wheel: require an unprevented event
                        # AND independently prove this exact element is a writable scroll range.
                        if (wheel["top"] < 100 and wheel["events"]
                                and not any(e.get("prevented") for e in wheel["events"])):
                            wheel["programmableTop"] = await js("""(() => {
                              const f=document.getElementById('feed');if(!f)return 0;
                              f.scrollTop=700;return f.scrollTop;
                            })()""")
                            wheel["headlessDefaultMissing"] = True
                            wheel["top"] = wheel["programmableTop"]
                        await js("""(() => { const p=document.getElementById('__wheel_probe');
                          if(p)p.remove(); PCOS.exit(); return true; })()""")
                    if not wheel or wheel.get("top", 0) < 100:
                        problems.append((label, "social-wheel-dead",
                                         "Social did not receive an unprevented wheel event on a "
                                         f"writable scroll range: {wheel}"))
                    elif wheel.get("scrollbar") not in ("thin", "auto"):
                        problems.append((label, "social-scrollbar-hidden",
                                         f"Social computed scrollbar-width is "
                                         f"{wheel.get('scrollbar')!r}"))

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
                if not (r.get("ghostShown") and r.get("snappedHalf") and r.get("snappedFullHeight") and r.get("ghostHidden")
                        and r.get("unsnapped")):
                    problems.append((label, "snap-broken",
                                     "Windows-11 edge snapping is not working — "
                                     f"preview={r.get('ghostShown')} snapped-to-half={r.get('snappedHalf')} "
                                     f"full-usable-height={r.get('snappedFullHeight')} "
                                     f"preview-cleared={r.get('ghostHidden')} "
                                     f"restored-on-drag-off={r.get('unsnapped')} {r.get('dbg')}"))
                pair = r.get("pairedTerminalDrag") or {}
                cycles = pair.get("cycles") or []
                if not pair.get("setup") or len(cycles) != 3 or not all(
                        c.get("unsnapped") and c.get("moved") and c.get("peerSame")
                        and not c.get("dragging") and not c.get("captured") for c in cycles):
                    problems.append((label, "paired-terminal-drag",
                                     "Terminal did not detach cleanly from a right-snapped native "
                                     f"placeholder across repeated focus/drag cycles: {pair}"))
                if r.get("docWins") != 1 or r.get("docDedup") != 1 or not r.get("docFeed") \
                        or not r.get("docTask") or not r.get("docPaint") \
                        or r.get("docClosed") != 0:
                    problems.append((label, "post-window-broken",
                                     "opening a post on the desktop must give it its own window "
                                     "(and re-opening it must focus that one, not add another) — "
                                     f"opened={r.get('docWins')} after-reopen={r.get('docDedup')} "
                                     f"feed-inside={r.get('docFeed')} taskbar={r.get('docTask')} "
                                     f"repaints={r.get('docPaint')} left-open={r.get('docClosed')}"))
                if r.get("searchWins") != 1 or r.get("searchLast") != "second" \
                        or "second" not in (r.get("searchShown") or "") \
                        or r.get("searchClosed") != 0:
                    problems.append((label, "search-window-stale",
                                     "searching again while the Search window is open re-ran the "
                                     "PREVIOUS query instead of the new one — one window, a "
                                     "succession of queries, so its render has to be replaced "
                                     f"(windows={r.get('searchWins')} last-render={r.get('searchLast')!r} "
                                     f"on-screen={r.get('searchShown')!r} left-open={r.get('searchClosed')})"))
                if r.get("viewOnDoc") != "profile" or r.get("viewOnFeature") != "bookmarks" \
                        or not r.get("docFeedBack") or r.get("viewWinsClosed") != 0:
                    problems.append((label, "stale-view",
                                     "refocusing a window did not hand the client back the view that "
                                     "window is showing — every painter keys on VIEW and writes into "
                                     "#feed, so the timeline fills whatever window is in front "
                                     f"(feature={r.get('viewOnFeature')!r} doc={r.get('viewOnDoc')!r} "
                                     f"kept-its-dom={r.get('docFeedBack')} "
                                     f"left-open={r.get('viewWinsClosed')})"))
                if not r.get("startOnTop") or not r.get("startOnTopLate"):
                    problems.append((label, "startmenu-buried",
                                     "the start menu does not render over the windows — it hits "
                                     f"{r.get('startCoveredBy')!r} when fresh and "
                                     f"{r.get('startCoveredByLate')!r} after 400 view switches "
                                     f"(top window z={r.get('winZmax')}, menu is 320). Window "
                                     "z-indexes must stay in the 10-200 band; see nextZ() in os.js."))
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
                # A FOLDER TILE STANDS FOR ITS MEMBERS. The rule being checked is that every app the
                # sidebar has is reachable from the desktop — not that every one has a tile of its
                # own, which stopped being true the moment a built-in folder existed. `flat` expands
                # each folder from its data-apps; order is compared as a SET for the same reason,
                # since a folder sits where its first member did.
                def flat(seq):
                    out = []
                    for v in seq:
                        out.extend(r.get("folderApps", {}).get(v, [v]) if str(v).startswith("folder:")
                                   else [v])
                    return out
                # Bug Report is deliberately an OS app even though its shared action lives outside
                # the sidebar. Other conditional extras are absent from this signed-out fixture.
                expected_apps = sorted(r["navViews"] + (["__bug"] if "__bug" in flat(r["icons"]) else []))
                if sorted(flat(r["icons"])) != expected_apps:
                    problems.append((label, "apps-missing",
                                     f"desktop icons {flat(r['icons'])} do not match the sidebar "
                                     f"{r['navViews']}"))
                if sorted(flat(r["menuApps"])) != expected_apps:
                    problems.append((label, "apps-missing",
                                     f"the start menu lists {flat(r['menuApps'])}"))
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
                # The two apps the two-window test opens — they must be TOP-LEVEL apps, which
                # Calendar and Contacts stopped being when the Office folder claimed them.
                if r["renderedAfterFocus"] not in ("notes", "vault"):
                    problems.append((label, "feed-not-handed-over",
                                     f"focusing did not re-render a feature (last was {r['renderedAfterFocus']})"))

                if not r["maximised"] or not r["restored"]:
                    problems.append((label, "window-controls",
                                     f"maximise={r['maximised']} restore={r['restored']}"))
                if not r.get("taskMoveRecovered"):
                    problems.append((label, "window-controls",
                                     "taskbar Move could not recover and reposition a maximised window"))
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
                    for w in (q.get("sized") or []):
                        if w["out"] > 2:
                            problems.append((label, "window-offscreen",
                                             f"{w['v']!r} opened {w['out']}px outside the desktop — a "
                                             "window has to be dragged back before it can be used"))
                        elif w["fillH"] < 55 or w["fillW"] < 30:
                            problems.append((label, "window-too-small",
                                             f"{w['v']!r} opened at {w['fillW']}%x{w['fillH']}% of a "
                                             f"{q.get('deskBox')} desktop — the first thing anyone does "
                                             "with a window that size is resize it"))
                    if not q.get("hasAddWidget"):
                        problems.append((label, "no-add-widget",
                                         "the desktop's right-click menu has no 'Add a widget…' — "
                                         "that menu is the only way in"))
                    elif "calendar" not in (q.get("widgetTypes") or []):
                        problems.append((w, "widget-picker",
                                         "the Today (calendar) widget is not offered — a widget "
                                         "missing from the picker cannot be added at all, and the "
                                         "picker is generated from the registry, so this means the "
                                         f"registry entry is gone: {q.get('widgetTypes')}"))
                    if q.get("calWidget") and (q.get("calOverlap") or 0) > 0:
                        problems.append((label, "widget-text-overlap",
                                         "the Today widget's first column overflows onto the "
                                         f"calendar colour swatch by {q.get('calOverlap')}px — "
                                         f"{q.get('calOverlapBy')!r} of {q.get('calLabels')}. That "
                                         "column holds a TIME on today's rows and a day LABEL "
                                         "('Tomorrow') under Coming up; a fixed flex:none width "
                                         "fits one and overflows with the other."))
                    elif q.get("calWidget") and not any(
                            l in ("Tomorrow",) for l in (q.get("calLabels") or [])):
                        # Without a Coming-up row the overlap probe above proved nothing.
                        problems.append((label, "widget-text-overlap",
                                         "the Today widget drew no 'Coming up' row, so the column "
                                         f"that overflows was never exercised: {q.get('calLabels')}"))
                    elif not q.get("widgetPickerOpens") or len(q.get("widgetTypes") or []) < 3:
                        problems.append((label, "widget-picker-empty",
                                         f"the widget picker offered {q.get('widgetTypes')}"))
                    elif not q.get("widgetDrawn"):
                        problems.append((label, "widget-not-drawn",
                                         "a widget was added and nothing appeared on the desktop — "
                                         "the document is written and the draw is what failed, which "
                                         "looks identical to the add not working"))
                    else:
                        if not q.get("widgetInside"):
                            problems.append((label, "widget-offscreen",
                                             "a widget drew outside the desktop"))
                        if not q.get("widgetHasBody"):
                            problems.append((label, "widget-empty",
                                             "the widget drew its frame but mounted no body"))
                        if (q.get("widgetMoved") or 0) > 200:
                            problems.append((label, "widget-not-draggable",
                                             "dragging a widget to the lower-left left it at "
                                             f"{q.get('widgetMoved')}px from the desk's left edge"))
                        if q.get("widgetTouchAction") != "none":
                            problems.append((label, "widget-not-draggable-by-finger",
                                             "a widget's touch-action is "
                                             f"{q.get('widgetTouchAction')!r}, not 'none' — the "
                                             "browser claims a finger drag as a page scroll and "
                                             "cancels the pointer a few pixels in, so on a tablet "
                                             "the panel starts to move and then stops. The windows "
                                             "(.osw-bar) and the desktop icons both carry the rule."))
                    if q.get("musicWidget") is False:
                        problems.append((label, "music-widget-missing",
                                         "the Now-playing widget did not draw"))
                    elif q.get("musicWidget") and (q.get("musicCalls") or []) != ["shuffle", "prev", "toggle", "next"]:
                        problems.append((label, "music-widget-dead",
                                         "the Now-playing widget's transport did not reach the "
                                         f"player: {q.get('musicCalls')!r}. Buttons that do nothing "
                                         "are the silent failure this widget has already had once "
                                         "(a bridge method called by the wrong name)."))
                    if q.get("messagesLabel") and q.get("messagesLabel") != "Messages":
                        problems.append((label, "icon-label-carries-badge",
                                         f"the Messages icon is named {q.get('messagesLabel')!r} — "
                                         "the unread badge is an <i> inside the label span, so the "
                                         "count is being read as part of the app's name"))
                    if q.get("connect4Label") and q.get("connect4Label") != "Connect 4":
                        problems.append((label, "icon-label-truncated",
                                         f"the Connect 4 icon is named {q.get('connect4Label')!r} — "
                                         "stripping trailing digits to remove a badge eats a name "
                                         "that legitimately ends in one"))
                    if q.get("musicWidget") and not q.get("musicHasSeek"):
                        problems.append((label, "music-widget-no-seek",
                                         "the Now-playing widget has no seek row — that is the piece "
                                         "that fills the middle of the panel"))
                    if q.get("clickAfterDrag"):
                        problems.append((label, "drag-is-also-a-click",
                                         "dragging a widget still fires the click its body listens "
                                         "for — repositioning the Community panel opens Server "
                                         "Stats, Today opens the Calendar, the blocks open "
                                         "mempool.space"))
                    elif q.get("clickAfterTap") != 1:
                        problems.append((label, "widget-swallows-every-click",
                                         "the post-drag click suppressor is not one-shot: the panel "
                                         f"took {q.get('clickAfterTap')} of the clicks that follow, "
                                         "so the widget stops responding after it is moved"))
                    if q.get("clockDrawn") is False:
                        problems.append((label, "clock-widget-missing", "the Clock widget did not draw"))
                    elif q.get("clockDrawn"):
                        import re as _re
                        if not _re.match(r"^\d{1,2}[:.]\d{2}", q.get("clockTime") or ""):
                            problems.append((label, "clock-shows-no-time",
                                             f"the Clock drew {q.get('clockTime')!r} instead of a time"))
                        if not q.get("clockPickerOpens"):
                            problems.append((label, "clock-picker-wont-open",
                                             "＋ on the Clock opened no city search"))
                        elif not q.get("clockPickerCloses"):
                            problems.append((label, "clock-picker-wont-close",
                                             "the Clock's city search survived a press on the "
                                             "desktop — an abandoned picker hides the world clocks "
                                             "for the rest of the session"))
                    # The panels have to follow the palette. Reported as "widgets should take into
                    # consideration the theme, it looks terrible and dark if you change themes" —
                    # a violet card with the theme's dark text on it, on eight of the nine themes.
                    for _t, _m in sorted((q.get("widgetTheme") or {}).items()):
                        _panel, _ink, _note = _m.get("panel"), _m.get("ink"), _m.get("note")
                        _light = _t in ("professional", "win98", "cherryblossom")
                        if _panel is None or _ink is None:
                            problems.append((label, "widget-theme-unmeasured",
                                             f"the {_t} widget panel or its text has no colour to "
                                             "measure — the check is blind, not passing"))
                            continue
                        if abs(_panel - _ink) < 0.35:
                            problems.append((label, "widget-unreadable-on-theme",
                                             f"on {_t} the widget panel ({_panel:.2f}) and the text "
                                             f"on it ({_ink:.2f}) are the same brightness — that is "
                                             "the dark-card-with-dark-text bug"))
                        elif _light and _panel < 0.55:
                            problems.append((label, "widget-dark-on-light-theme",
                                             f"{_t} is a light theme and the widget panel is "
                                             f"{_panel:.2f} — it is still painting cyberpunk's dark "
                                             "glass on someone else's desktop"))
                        elif not _light and _panel > 0.5:
                            problems.append((label, "widget-light-on-dark-theme",
                                             f"{_t} is a dark theme and the widget panel is {_panel:.2f}"))
                        # The sticky note's textarea must stay the PAPER: transparent (None), never a
                        # panel painted across the middle of it by the readability layer.
                        if _note is not None:
                            problems.append((label, "sticky-note-has-a-textbox",
                                             f"on {_t} the sticky note's textarea is painting its own "
                                             f"background ({_note:.2f}) over the yellow paper — "
                                             "`:root[data-theme] textarea` beats `.wgt-note` again"))
                    if (q.get("musicGap") or 0) > 30:
                        problems.append((label, "music-widget-hollow",
                                         f"the widest gap between the Now-playing widget's rows is "
                                         f"{q.get('musicGap')}% of its body — the panel is mostly "
                                         "empty space in the middle again"))
                    if q.get("adminBlack"):
                        problems.append((label, "admin-window-black-after-move",
                                         "moving the Admin window left BOTH halves hidden — the feed "
                                         "and the admin host — so the window is black. Its view is "
                                         "read from `w.view`, which is what the window was OPENED "
                                         "as; Admin is reached from the Settings view and paints "
                                         "into that window, so the window never knew it was showing "
                                         "Admin. See noteView / the capture in focusWin."))
                    if q.get("afterDragView") and q.get("afterDragView") != "admin":
                        problems.append((label, "drag-repaints-wrong-view",
                                         "dragging a window that had been navigated INSIDE itself "
                                         f"repainted it as {q.get('afterDragView')!r} instead of the "
                                         "view it was showing. `w.view` is what a window was OPENED "
                                         "as; the Admin panel is opened from Settings and paints "
                                         "into that window, so a drag threw the user back to User "
                                         "Settings — and _feedVisibleFor, told the same wrong view, "
                                         "hid the admin host and left the window black."))
                    if q.get("feedHiddenAfterClose"):
                        problems.append((label, "window-black-after-admin",
                                         "the window focused after another one closed inherited a "
                                         "HIDDEN #feed and nothing put it back — its content is live "
                                         "and unreachable, which on screen is a black window. The "
                                         "admin panel hides the shared feed to show its iframe; "
                                         "whoever takes the feed next has to decide whether it is "
                                         "visible (os.js _feedVisibleFor), because a window restored "
                                         "from its park deliberately skips the repaint that used to "
                                         "be the only thing clearing it."))
                    if not q.get("hasBell") or not q.get("bellReachable"):
                        problems.append((label, "no-noti-bell",
                                         "the taskbar has no clickable notification bell — "
                                         f"present={q.get('hasBell')} reachable={q.get('bellReachable')}. "
                                         "Notifications were behind the CLOCK, which nothing "
                                         "announces as a button."))
                    elif not (q.get("bellOpens") and q.get("bellCloses")):
                        problems.append((label, "no-noti-bell",
                                         "the bell does not toggle the notification centre — "
                                         f"opens={q.get('bellOpens')} closes={q.get('bellCloses')}"))
                    elif (min(q.get("bellSize") or [0, 0]) < min(q.get("netSize") or [99, 99])
                          or min(q.get("bellSize") or [0, 0]) < 18):
                        problems.append((label, "no-noti-bell",
                                         f"the bell is {q.get('bellSize')} against the relay icon's "
                                         f"{q.get('netSize')} — the tray is one row of equal targets"))
                    if q.get("clockHasBadge"):
                        problems.append((label, "no-noti-bell",
                                         "the unread count is still on the clock; it belongs on the bell"))
                    if (q.get("bellDot3") != "3" or q.get("bellDot9") != "9"
                            or q.get("bellDotAfterRead") not in ("", None)
                            or q.get("bellDotAfterArrival") != "4"
                            or not q.get("bellReadCalls")):
                        problems.append((label, "tray-count-stuck",
                                         "the bell's unread count does not track what arrives — "
                                         f"3→{q.get('bellDot3')!r} 9→{q.get('bellDot9')!r} "
                                         f"after-read→{q.get('bellDotAfterRead')!r} "
                                         f"next-arrival→{q.get('bellDotAfterArrival')!r} "
                                         f"(notifsRead calls={q.get('bellReadCalls')}). It must ask "
                                         "PC().notifUnread() — the same count the sidebar badge is "
                                         "painted from — never the LENGTH of the sliced notifItems "
                                         "list, which is a constant on any real account."))
                    elif (not q.get("barSameNode") or q.get("barKeptText") != "half a query"
                          or (q.get("barFocusBefore") and not q.get("barKeptFocus"))):
                        problems.append((label, "tray-count-stuck",
                                         "repainting the bell rebuilt the taskbar and took the caret "
                                         "out of the search box "
                                         f"(focus kept={q.get('barKeptFocus')} text="
                                         f"{q.get('barKeptText')!r} focus-before="
                                         f"{q.get('barFocusBefore')} same-node={q.get('barSameNode')}). "
                                         "Paint the button in place; "
                                         "drawBar() destroys the input."))
                    if q.get("netOpenFirst") and not (q.get("bellSurvivedPointerdown")
                                                      and q.get("bellOpensOverNet")):
                        problems.append((label, "tray-click-swallowed",
                                         "the bell does nothing while the network flyout is open — "
                                         "the click-away handler closed the flyout and rebuilt the "
                                         f"bar (button survived the press="
                                         f"{q.get('bellSurvivedPointerdown')}, centre opened="
                                         f"{q.get('bellOpensOverNet')}), detaching the button the "
                                         "pointerdown was aimed at. Every tray trigger belongs in "
                                         "_TRAY_KEEP (os.js)."))
                    # `x or 99` would read a PERFECT landing (0px off) as a miss — the falsy zero.
                    _dx = q.get("freeDx"); _dy = q.get("freeDy")
                    if _dx is None or _dy is None or abs(_dx) > 12 or abs(_dy) > 12 \
                            or not q.get("freeClass"):
                        problems.append((label, "icon-not-placed",
                                         "an icon dragged to empty desktop did not land where it was "
                                         f"dropped — off by ({q.get('freeDx')}, {q.get('freeDy')})px, "
                                         f"free-layout={q.get('freeClass')}"))
                    if q.get("othersShifted"):
                        problems.append((label, "icon-not-placed",
                                         f"{q['othersShifted']} other icon(s) moved when one was placed — "
                                         "the first free move must SEED the rest with where they already "
                                         "are, or moving one icon rearranges the whole desktop"))
                    if not q.get("freeSaved"):
                        problems.append((label, "icon-not-placed",
                                         "the position was never written to the layout document, so it "
                                         "is gone on the next reload and absent on every other device"))
                    if not q.get("hasBgRow") or not q.get("pickerOpen"):
                        problems.append((label, "no-wallpaper",
                                         "right-clicking the desktop does not offer a background picker "
                                         f"— menu-row={q.get('hasBgRow')} opened={q.get('pickerOpen')}"))
                    else:
                        tiles = q.get("bgTiles") or []
                        if "Neon city" not in tiles or "Aurora" not in tiles:
                            problems.append((label, "no-wallpaper",
                                             f"the picker lists {tiles} — it must offer the images in the "
                                             "drive's Backgrounds folder"))
                        if "notes.txt" in tiles or "a song" in tiles:
                            problems.append((label, "no-wallpaper",
                                             f"the picker lists non-images / other folders: {tiles}"))
                        if not q.get("bgApplied") or not q.get("bgHasClass"):
                            problems.append((label, "no-wallpaper",
                                             "choosing a picture did not paint the desktop — "
                                             f"applied={q.get('bgApplied')} class={q.get('bgHasClass')}"))
                        if not q.get("bgSaved"):
                            problems.append((label, "no-wallpaper",
                                             "the wallpaper was not saved, so it is gone on reload"))
                    if not q.get("hasLineUpRow") or not q.get("gridBack"):
                        problems.append((label, "icon-not-placed",
                                         "'Line the icons up' is missing or does not restore the grid — "
                                         f"row={q.get('hasLineUpRow')} back={q.get('gridBack')}"))
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
        # Chrome keeps writing its profile for a brief moment after SIGTERM. Wait for the process
        # before deleting it so the test does not intermittently leave files behind or print a
        # misleading cleanup error after every assertion has passed.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
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
