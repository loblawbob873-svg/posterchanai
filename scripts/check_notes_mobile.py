#!/usr/bin/env python3
"""Layout + behaviour check for NOTES, at phone AND desktop widths.

Run BEFORE deploying a Notes change:

    venv-unified/bin/python scripts/check_notes_mobile.py

check_client_mobile.py only ever loads the timeline — it never opens Notes, so a three-pane layout
that is unusable on a phone would ship having "passed the mobile check". This drives the real
notes.js against a stubbed `window.__PC` (no relay, no login, no network needed) with a seeded
library, and audits at phone widths.

Assertions, each corresponding to a way this specific screen breaks on a phone:

  horizontal-overflow  the panes push the page sideways. Three columns at 390px is the default
                       failure of any notes layout.
  both-panes-visible   list AND editor on screen at once at phone width: two 180px columns, neither
                       readable. Opening a note must REPLACE the list, and there must be a way back.
  no-way-back          the editor is open with no back control — a dead end on a phone, since there
                       is no second pane to click.
  tiny-tap-target      a row or button under 32px tall.
  text-truncated       a button label clipped by its own box. Three labelled buttons across a
                       200px sidebar left ~35px of text each, truncating every one to "Fol…".
  ios-zoom-trap        a text input under 16px: iOS Safari zooms the page on focus and never
                       zooms back out. Applies to the title, the body, search and the tag field.
  editor-under-nav     the editor's bottom is behind the fixed .mobilenav (~62px + safe area), i.e.
                       someone wrote 100vh instead of 100dvh, or forgot to reserve the nav.
  mismatched-buttons   Two icon buttons side by side in the editor header rendered at different
                       sizes, because one carried .btn.small and the other did not.
  notes-cross-saved    Switching notes with a save still debounced wrote one note's fields onto
                       another. `.nt-editor` is ONE element whose innerHTML is replaced per note, so
                       a commit that looks its inputs up when it fires reads whichever note is on
                       screen then. The result is half of each note and looks entirely plausible.
  folder-pane-hogs-screen
                       the folder tree is on screen at rest on a phone, or the note list is left
                       under 80% of the pane. It shipped as a pane stacked above the list, capped at
                       40vh: folders took half the screen permanently and the notes got 273px of 726.
  folders-unreachable  …and the drawer that replaced it doesn't open, or won't close again.
  image-stampede       opening a note fetched most of its attachments at once. Every `pcres:` image
                       is a full download of the ciphertext plus a decrypt — the real library fired
                       131 of them in eleven seconds and the note looked broken until they landed.
                       They must load as they come into view, and the strip below the note must not
                       list a thumbnail for every attachment either.
  image-failure-permanent
                       a picture that failed to load was replaced by a text placeholder. The element
                       is destroyed, so one dropped request out of a hundred in flight is
                       indistinguishable from a lost attachment and nothing can ask again.
  queue-wedged         a STALLED read (a fetch that never settles, which is what a dropped socket
                       actually gives you — not a rejection) held its slot forever. Four of those
                       and no picture in any note loads again for the rest of the session, with
                       nothing on screen to say why.
  attachments-eat-the-note
                       the attachment strip crushed the note's own text. It is a wrapping flex row
                       whose automatic minimum size is its content, so thirty thumbnails claimed
                       fifteen rows and left the note 36px on a phone.
  attachment-wont-open a PDF (or any non-image attachment) opened nowhere. window.open() after an
                       await is an unsolicited popup, and 'noopener' returns null so the failure is
                       invisible. The tab must be reserved during the click, with a download as the
                       fallback.
  backup-unsaveable    no save dialog (Firefox has none: showSaveFilePicker is Chromium-only, and
                       Electron denied the permission until the shell granted it) made the backup
                       SKIP every attachment — a "backup" of a note library without its files.
  offline-write-lost   THE data-loss one, and not a layout question at all: with publishing failing
                       (offline), a typed note must still be in the library and queued, never gone.
                       publish() rolls its optimistic cache save BACK when the relay refuses, so a
                       note taken on a train is exactly the write that disappears if this regresses.

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome / websockets).
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
# Phone widths, plus the two DESKTOP widths where the sidebar is at its narrowest. The three-pane
# layout only exists above 820px, so a phone-only audit never looked at the sidebar as a column at
# all — which is exactly where three labelled buttons truncated to "Fol… Imp… Bac…". 900px is the
# 170px-sidebar tier, 1280px the 210px one.
WIDTHS = [(390, 844, True), (360, 780, True), (900, 800, False), (1280, 860, False)]
PORT = 9475
PROFILE = "/tmp/pc-notes-mobile-check"

# The host page. notes.js takes every helper off window.__PC and reads window.Relay/window.Store,
# so a stub is enough — and is the point: this tests the module, not the server.
PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css">
</head><body>
<div id="feed"></div>
<nav class="mobilenav glass"><button class="nav-item"><b>Home</b></button></nav>
<div id="modal-root"></div><div id="toast-root"></div>
<script src="/static/js/client/sprite.js"></script>
<script>
// ---- stub host -------------------------------------------------------------------
// Encryption is identity here: this file is auditing layout and the offline write path, and a real
// NIP-44 round trip would only be testing the browser's crypto. The SHAPES stay honest — content is
// still a string that must survive JSON.parse, and every event still carries its d/l tags.
const $  = (s,r)=> (r||document).querySelector(s);
const $$ = (s,r)=> Array.from((r||document).querySelectorAll(s));
const enc = s => String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
window.__events = [];        // what "the relay" accepted
window.__online = true;      // flip to false to simulate offline
window.__encCalls = [];      // every attachment decrypt the client asked for
window.__encFail = false;    // true → every decrypt fails (a dropped request)
window.__encHang = 0;        // >0 → that many reads never settle at all (a stalled socket)
let _seq = 0;

window.Store = {
  _evs: [],
  query(filters){ return this._evs.slice(); },
  saveEvent(ev){ this._evs = this._evs.filter(e => _d(e) !== _d(ev)); this._evs.push(ev); },
  removeEvent(id){ this._evs = this._evs.filter(e => e.id !== id); },
};
function _d(ev){ return ((ev.tags||[]).find(t=>t[0]==='d')||[])[1]||''; }
window.Relay = {
  query: async () => window.__online ? window.__events.slice() : [],
  publish: async (ev) => { if(!window.__online) return {ok:false};
                           window.__events = window.__events.filter(e=>_d(e)!==_d(ev));
                           window.__events.push(ev); return {ok:true}; },
};
window.__PC = {
  $, $$, enc,
  toast: m => { window.__toasts = (window.__toasts||[]).concat([m]); },
  uiConfirm: async () => true,
  uiPrompt: async () => 'New folder',
  modal: (html, onMount) => { const bg=document.createElement('div'); bg.className='modal-bg';
    bg.innerHTML = '<div class="modal glass neon-border">'+html+'</div>';
    $('#modal-root').appendChild(bg); if(onMount) onMount(bg.querySelector('.modal')); },
  closeModal: () => { const m=$('#modal-root .modal-bg'); if(m) m.remove(); },
  // The real publish(): signs, optimistically saves, and ROLLS THE SAVE BACK when the relay
  // refuses. Reproducing the rollback is the whole point of the offline-write-lost assertion.
  publish: async (kind, content, tags, opts) => {
    const ev = { id:'ev'+(++_seq), pubkey:'me', kind, content, tags, created_at: Math.floor(Date.now()/1000)+_seq, sig:'x' };
    window.Store.saveEvent(ev);
    const r = await window.Relay.publish(ev);
    if(!r.ok) window.Store.removeEvent(ev.id);
    return { ev, ...r };
  },
  nip44enc: async (pk, s) => s,
  nip44dec: async (pk, s) => s,
  // Enough markdown to produce the one thing this file needs to see: an <img src="pcres:…">, the
  // same shape the real _mdUrl allowlists. Without it a note full of pictures renders as text and
  // the whole attachment path goes untested.
  mdToHtml: s => '<p>' + enc(s).replace(/!\[([^\]]*)\]\((pcres:[0-9a-f]{64})\)/g,
                                        (m, alt, u) => `<img src="${u}" alt="${alt}">`) + '</p>',
  uploadEncFile: async () => 'sha'+(++_seq),
  // Records every decrypt so the test can count them, and can be told to FAIL — a picture that
  // won't load is the normal case here (a dropped request), not an exotic one.
  encFileUrl: async (sha) => {
    window.__encCalls.push(sha);
    if(window.__encFail) throw new Error('blob HTTP 502');
    // A STALLED read: a promise that never settles, which is what a dropped socket actually gives
    // you — not a rejection. The first __encHang calls hang forever.
    if(window.__encHang > 0){ window.__encHang--; return new Promise(() => {}); }
    // A picture with REAL dimensions, not a 1x1 pixel: a loaded image that collapses to 3px pulls
    // the next thirty up into view, so every one of them loads and the lazy path looks broken when
    // it is the test's own placeholder that is wrong.
    return 'data:image/svg+xml,' + encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"></svg>');
  },
  get ME(){ return {pubkey:'me'}; },
  get VIEW(){ return 'notes'; },
};
</script>
<script src="/static/js/client/joplin.js"></script>
<script src="/static/js/client/notes.js"></script>
<script>
(async function(){
  // Seed a library the way the app would have: a folder and a few notes, already "on the relay".
  const mk = (d, obj) => ({ id:'seed'+d, pubkey:'me', kind:30078, created_at: 1700000000,
                            tags:[['d',d],['l','pcai-notes']], content: JSON.stringify(obj), sig:'x' });
  // An imported note carrying a lot of pictures — the shape that made this screen unusable. Each
  // one is a separate download + decrypt, so what matters is that opening the note does NOT ask for
  // all of them. Sorted last (oldest `updated`) so it can't disturb the two tests that address
  // notes by position.
  const shas = Array.from({length:30}, (_,i) => (i+1).toString(16).padStart(2,'0').repeat(32));
  const picBody = shas.map((s,i) => `shot ${i}\n\n![shot ${i}](pcres:${s})`).join('\n\n');
  window.__events = [
    mk('pcai:notefolder:f1', {v:1, id:'f1', name:'Work', created:1, updated:1}),
    mk('pcai:note:n1', {v:1, id:'n1', title:'Quarterly plan', body:'line one\nline two', folder:'f1', tags:['work'], created:1, updated:1700000000, res:[]}),
    mk('pcai:note:n2', {v:1, id:'n2', title:'Groceries', body:'milk', folder:'', tags:[], created:1, updated:1699000000,
                        res:[{sha:'ab'.repeat(32), name:'receipt.png', mime:'image/png', size:5242880}]}),
    mk('pcai:note:n3', {v:1, id:'n3', title:'Screenshots', body:picBody, folder:'', tags:[], created:1, updated:1698000000,
                        res: shas.map((s,i) => ({sha:s, name:`shot${i}.png`, mime:'image/png', size:1024}))}),
  ];
  for(let i=0;i<80 && !window.PCNotes;i++) await new Promise(r=>setTimeout(r,50));
  await window.PCNotes.render();
  for(let i=0;i<80 && !document.querySelector('.nt-item');i++) await new Promise(r=>setTimeout(r,50));
  window.__ready = true;
})();
</script>
</body></html>"""

# Audited in the page. Returns plain data; every judgement is made in Python.
AUDIT = r"""(() => {
  const vw = window.innerWidth;
  const box = el => { const r = el.getBoundingClientRect();
                      return {x:r.x, y:r.y, w:r.width, h:r.height, bottom:r.bottom, right:r.right}; };
  const vis = el => !!(el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
  const out = { vw, overflow: document.documentElement.scrollWidth > vw + 1 };
  out.wrap = !!document.querySelector('.nt-wrap');
  out.listVisible = vis(document.querySelector('.nt-list'));
  out.editorVisible = vis(document.querySelector('.nt-editor'));
  out.items = document.querySelectorAll('.nt-item').length;
  const small = [];
  for(const el of document.querySelectorAll('.nt-item, .nt-folder, .nt-side-head .btn, .nt-res-item, .nt-ed-head .btn')){
    if(!vis(el)) continue;
    const b = box(el);
    if(b.h < 32) small.push({sel: el.className, h: Math.round(b.h), text:(el.textContent||'').trim().slice(0,24)});
  }
  out.small = small;
  const zoomy = [];
  for(const el of document.querySelectorAll('.nt-wrap input, .nt-wrap textarea, .nt-wrap select')){
    if(!vis(el)) continue;
    const fs = parseFloat(getComputedStyle(el).fontSize) || 0;
    if(fs < 16) zoomy.push({cls: el.className, fs});
  }
  out.zoomy = zoomy;
  const wrapEl = document.querySelector('.nt-wrap');
  out.wrapBottom = wrapEl ? box(wrapEl).bottom : 0;
  const nav = document.querySelector('.mobilenav');
  out.navTop = (nav && vis(nav)) ? box(nav).y : window.innerHeight;
  out.back = vis(document.querySelector('.nt-back'));
  const ed = document.querySelector('.nt-editor');
  out.editorBottom = (ed && vis(ed)) ? box(ed).bottom : 0;
  const body = document.querySelector('.nt-body');
  out.bodyBottom = (body && vis(body)) ? box(body).bottom : 0;
  // Icon buttons that sit side by side must BE the same size. Preview shipped as a plain .btn and
  // Delete as a .btn.small, so they were visibly different heights next to each other.
  // Text CLIPPED by its own box: scrollWidth beats clientWidth when a label doesn't fit. Three
  // labelled buttons across a 200px sidebar truncated every one to "Fol…", which no amount of
  // padding tuning fixes — the width isn't there.
  out.clipped = [];
  // Every text-bearing node, not just the buttons: `text-overflow: ellipsis` is almost always set
  // on an inner <span>, so checking only the button measures a box that never overflows and the
  // assertion silently passes on a layout that is visibly cut off. (It did exactly that once.)
  for(const el of document.querySelectorAll('.nt-side-head .btn, .nt-side-head .btn span, .nt-side-actions .btn, .nt-side-actions .btn span, .nt-folder span, .nt-item b, .nt-list-head b')){
    if(!vis(el)) continue;
    const t = (el.textContent||'').trim();
    if(t && el.scrollWidth > el.clientWidth + 2)
      out.clipped.push({ text:t.slice(0,20), shown:Math.round(el.clientWidth), needs:Math.round(el.scrollWidth) });
  }
  out.headBtns = Array.from(document.querySelectorAll('.nt-ed-head .btn')).filter(vis)
    .map(el => ({ cls: el.className, w: Math.round(box(el).w), h: Math.round(box(el).h) }));
  // How the screen is DIVIDED. vis() is not enough for the folder tree: a drawer that has been
  // translated off-canvas still has client rects, so ask where it actually is.
  const onScreen = el => { if(!vis(el)) return false; const b = box(el);
                           return b.right > 0 && b.x < vw && b.bottom > 0 && b.y < window.innerHeight; };
  out.foldersOnScreen = onScreen(document.querySelector('.nt-folder[data-f]'));
  out.folderBtn = vis(document.querySelector('.nt-fbtn'));
  out.searchVisible = vis(document.querySelector('.nt-search'));
  const listEl = document.querySelector('.nt-list');
  out.listH = (listEl && vis(listEl)) ? Math.round(box(listEl).h) : 0;
  out.wrapH = wrapEl ? Math.round(box(wrapEl).h) : 0;
  return out;
})()"""

# Tap the folder handle. On a phone the tree is a drawer, and this is the only way to it.
OPEN_DRAWER = r"""(() => { const b = document.querySelector('.nt-fbtn');
                           if(!b || !b.getClientRects().length) return false; b.click(); return true; })()"""
CLOSE_DRAWER = r"""(() => { const s = document.querySelector('.nt-scrim');
                            if(s && !s.hidden) s.click(); return true; })()"""

# Open a note, then report what the layout does. Separate from AUDIT because it MUTATES.
OPEN_NOTE = r"""(() => { const it = document.querySelector('.nt-item'); if(!it) return false;
                         it.click(); return true; })()"""

# Edit note A, and switch to note B INSIDE the 700ms debounce. A's edit must land on A, whole, and
# nothing of B's may leak into it.
CROSS_SAVE = r"""(async () => {
  const items = document.querySelectorAll('.nt-item');
  if(items.length < 2) return {error:'need two notes'};
  items[0].click();
  await new Promise(r => setTimeout(r, 60));
  const openTitle = () => (document.querySelector('.nt-title')||{}).value;
  const first = openTitle();
  const tagIn = document.querySelector('.nt-tagin');
  const body  = document.querySelector('.nt-body');
  tagIn.value = 'alpha-only';
  tagIn.dispatchEvent(new Event('change', {bubbles:true}));
  body.value = 'body of the first note';
  body.dispatchEvent(new Event('input', {bubbles:true}));
  // Switch immediately — well inside the debounce window.
  document.querySelectorAll('.nt-item')[1].click();
  await new Promise(r => setTimeout(r, 1600));
  // Read back what was actually published for each note.
  const byTitle = {};
  for(const ev of window.__events){
    const d = ((ev.tags||[]).find(t=>t[0]==='d')||[])[1]||'';
    if(!d.startsWith('pcai:note:') || !ev.content) continue;
    let o = null; try{ o = JSON.parse(ev.content); }catch(e){ continue; }
    if(o && o.title) byTitle[o.title] = o;
  }
  const a = byTitle[first] || null;
  const others = Object.keys(byTitle).filter(t => t !== first).map(t => byTitle[t]);
  return {
    first,
    aTags: a ? a.tags : null,
    aBody: a ? a.body : null,
    leaked: others.some(o => (o.tags||[]).includes('alpha-only') ||
                             (o.body||'').includes('body of the first note')),
  };
})()"""

# Both picture tests start from the same note, so they open with the same preamble.
_PIC_NOTE = r"""(async () => {
  const picNote = () => Array.from(document.querySelectorAll('.nt-item'))
    .find(b => (b.textContent||'').includes('Screenshots'));
"""

# Opening a picture-heavy note must not fetch every picture in it. Each `pcres:` reference is a full
# download of the ciphertext plus a decrypt; resolving all of them on open fired 131 requests in
# eleven seconds on the real library and left the note looking broken until they landed. What has to
# be true: only what is near the viewport loads on open, and the rest still loads when scrolled to.
LAZY_IMAGES = _PIC_NOTE + r"""
  const item = picNote(); if(!item) return {error:'the picture-heavy note is not in the list'};
  window.__encCalls = [];
  item.click();
  await new Promise(r => setTimeout(r, 1000));
  const imgs = () => Array.from(document.querySelectorAll('.nt-render img'));
  const withSrc = () => imgs().filter(i => (i.getAttribute('src')||'').startsWith('data:')).length;
  // UNIQUE blobs. The stub has no in-flight dedupe (the real encFileUrl does), so counting raw calls
  // would be measuring the stub — what matters is how many distinct attachments were pulled.
  const uniq = () => new Set(window.__encCalls).size;
  const total = imgs().length, onOpen = uniq(), loadedTop = withSrc();
  // How the editor divides itself. A note's own text must not be crushed by its attachment strip.
  const h = sel => { const e = document.querySelector(sel); if(!e) return 0;
                     return Math.round(e.getBoundingClientRect().height); };
  const panes = { render: h('.nt-render'), res: h('.nt-res'), editor: h('.nt-editor'),
                  thumbs: document.querySelectorAll('.nt-res-thumb').length };
  // The pictures at the BOTTOM of the note: lazy has to mean "later", not "never".
  const pane = document.querySelector('.nt-render');
  if(pane) pane.scrollTop = pane.scrollHeight;
  await new Promise(r => setTimeout(r, 1400));
  return { total, onOpen, loadedTop, afterScroll: uniq(), loadedEnd: withSrc(), panes };
})()"""

# A picture that fails to load must stay a picture. This used to replace the <img> with a permanent
# "[image unavailable]" — the element was destroyed, so one dropped request out of a hundred in
# flight was indistinguishable from a lost attachment and there was no way to ask again.
IMAGE_RETRY = _PIC_NOTE + r"""
  const item = picNote(); if(!item) return {error:'the picture-heavy note is not in the list'};
  window.__encFail = true;
  window.__encCalls = [];
  item.click();
  await new Promise(r => setTimeout(r, 900));
  const failed = document.querySelectorAll('.nt-render img.nt-img-fail').length;
  const survived = document.querySelectorAll('.nt-render img').length;
  const tombstones = document.querySelectorAll('.nt-render .nt-img-miss').length;
  window.__encFail = false;             // the network comes back
  await new Promise(r => setTimeout(r, 2200));   // the automatic retry is at 1.5s
  const recovered = Array.from(document.querySelectorAll('.nt-render img'))
    .filter(i => (i.getAttribute('src')||'').startsWith('data:')).length;
  return { failed, survived, tombstones, recovered };
})()"""

# A STALLED read must not wedge the queue. encFileUrl is a bare fetch(), and a fetch that stalls
# never settles — it does not reject, so nothing downstream ever runs. With a fixed number of slots
# and no timer, four stalls means no picture in any note ever loads again for the rest of the
# session, silently. Hangs exactly as many reads as the queue is wide, then waits out the slot
# release and checks that work resumed. Slow on purpose (~23s); run at one width.
STALLED_QUEUE = _PIC_NOTE + r"""
  const item = picNote(); if(!item) return {error:'the picture-heavy note is not in the list'};
  const loaded = () => Array.from(document.querySelectorAll('.nt-render img, .nt-res-thumb img'))
    .filter(i => (i.getAttribute('src')||'').startsWith('data:')).length;
  window.__encHang = 4;                 // every slot, held open forever
  window.__encCalls = [];
  item.click();
  await new Promise(r => setTimeout(r, 1200));
  const early = loaded();
  await new Promise(r => setTimeout(r, 21000));   // the slot release is at 20s
  const pane = document.querySelector('.nt-render');
  if(pane) pane.scrollTop = pane.scrollHeight;
  await new Promise(r => setTimeout(r, 1500));
  return { early, later: loaded(), calls: new Set(window.__encCalls).size };
})()"""

# Opening an attachment (a PDF, say) must not depend on a popup that the browser has already stopped
# allowing. window.open() AFTER an await is treated as unsolicited — the click is over by the time
# the blob is decrypted — and 'noopener' made it unrecoverable, because with that flag open() returns
# null by spec, so a blocked popup and a working one look identical. Reported as "windows app can't
# open PDF from Note" and then "firefox can't open pdf from note either".
OPEN_ATTACHMENT = r"""(async () => {
  const it = Array.from(document.querySelectorAll('.nt-item'))
    .find(b => (b.textContent||'').includes('Groceries'));
  if(!it) return {error:'the seeded note with an attachment is missing'};
  it.click();
  await new Promise(r => setTimeout(r, 500));

  // A browser that refuses popups: window.open returns null, exactly as it does once the gesture
  // has expired. The attachment must still reach the user — as a download.
  const realOpen = window.open;
  let openedWith = null, downloaded = null;
  window.open = (u) => { openedWith = u; return null; };
  const realClick = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function(){ if(this.download) downloaded = this.href; else realClick.call(this); };

  const btn = document.querySelector('.nt-res-item') || document.querySelector('.nt-res-thumb');
  if(!btn){ window.open = realOpen; HTMLAnchorElement.prototype.click = realClick;
            return {error:'no attachment control rendered'}; }
  btn.click();
  await new Promise(r => setTimeout(r, 900));
  window.open = realOpen; HTMLAnchorElement.prototype.click = realClick;
  return { reservedTab: openedWith === '', downloaded: !!downloaded,
           toasts: (window.__toasts||[]).slice(-2) };
})()"""

# A REFUSED save dialog must degrade honestly. showSaveFilePicker is the only way to write a library
# with attachments (they are gigabytes), so when the picker is denied the backup has to fall back to
# notes-only AND say so — never quietly attempt to assemble every attachment in memory, which is the
# failure the streaming path exists to avoid and which the fallback used to walk straight into.
BACKUP_REFUSED = r"""(async () => {
  window.__encCalls = [];
  window.__toasts = [];
  // The API EXISTS but refuses — a denied permission, not a cancelled dialog.
  window.showSaveFilePicker = async () => { const e = new Error('permission denied'); e.name = 'NotAllowedError'; throw e; };
  let clicked = 0;
  const realClick = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function(){ if(this.download) clicked++; else realClick.call(this); };
  const btn = document.querySelector('.nt-export');
  if(!btn) return {error:'no backup button'};
  btn.click();
  await new Promise(r => setTimeout(r, 1500));      // uiConfirm auto-confirms in this harness
  HTMLAnchorElement.prototype.click = realClick;
  return { attachmentReads: window.__encCalls.length, downloaded: clicked,
           said: (window.__toasts||[]).join(' | ') };
})()"""

# The offline write. Types into the open editor with publishing failing, waits out the 700ms
# debounce, and reports whether the text survived anywhere it could be recovered from.
# PCNotes.save() — the door the rest of the app comes in through (the post-card "Save to Notes"
# button is the first caller). Two things have to hold, and the second is the one that bites: the
# ATTACHMENT must become an encrypted pcres: reference rather than a link, and an offline save must
# still leave the note in the local library. save() returns {ok:false, queued:true} when the relay
# refuses, and publish() rolls its optimistic cache write BACK on refusal — so a caller that treats
# queued as success over a note publish() already deleted would report "saved" about nothing.
SAVE_API = r"""(async () => {
  if(!window.PCNotes || !window.PCNotes.save) return {error:'PCNotes.save is gone'};
  const png = new File([new Uint8Array([137,80,78,71])], 'post.png', {type:'image/png'});
  const out = {};
  window.__online = true;
  const a = await window.PCNotes.save({ title:'A card', body:'the post text\n',
                                        tags:['saved-post'], files:[png] });
  out.onlineId = a && a.id;
  out.onlineQueued = !!(a && a.queued);

  window.__online = false;
  const b = await window.PCNotes.save({ title:'Saved on a train', body:'offline body\n',
                                        tags:['saved-post'], files:[png] });
  out.offlineId = b && b.id;
  out.offlineQueued = !!(b && b.queued);
  // The note the caller was told about must actually be readable back.
  out.offlineInStore = window.Store._evs.some(e => (e.content||'').includes('offline body'));
  let pending = [];
  try{ pending = JSON.parse(localStorage.getItem('pcaiNotesPending')||'[]'); }catch(e){}
  out.offlineInPending = pending.some(e => (e.content||'').includes('offline body'));
  // The picture is an encrypted attachment, not a URL: a linked blob goes blank offline and puts
  // the image on a server this library is specifically encrypted against.
  const ev = window.Store._evs.filter(e => (e.content||'').includes('the post text')).pop();
  out.hasRes = !!(ev && /"res":\[\{"sha"/.test(ev.content||''));
  out.hasRef = !!(ev && /pcres:/.test(ev.content||''));
  out.hasTag = !!(ev && /saved-post/.test(ev.content||''));
  window.__online = true;
  return out;
})()"""

OFFLINE_WRITE = r"""(async () => {
  window.__online = false;
  const t = document.querySelector('.nt-title'), b = document.querySelector('.nt-body');
  if(!t || !b) return {error:'editor not open'};
  t.value = 'Written on a train';
  b.value = 'this must not disappear';
  t.dispatchEvent(new Event('input', {bubbles:true}));
  b.dispatchEvent(new Event('input', {bubbles:true}));
  await new Promise(r => setTimeout(r, 1600));
  let pending = [];
  try{ pending = JSON.parse(localStorage.getItem('pcaiNotesPending')||'[]'); }catch(e){}
  const inStore = window.Store._evs.some(e => (e.content||'').includes('must not disappear'));
  const inPending = pending.some(e => (e.content||'').includes('must not disappear'));
  const state = (document.querySelector('.nt-state')||{}).textContent || '';
  // Back to the list and in again — the note must still be there, which is what "saved on this
  // device" has to mean.
  const survives = window.PCNotes.pendingCount() > 0;
  return { inStore, inPending, state, survives, pendingCount: window.PCNotes.pendingCount() };
})()"""


async def drive(url):
    import websockets
    subprocess.run(["rm", "-rf", PROFILE], check=False)
    chrome = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
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
                        return msg.get("result")

            async def js(expr, awaited=False):
                r = await call("Runtime.evaluate",
                               {"expression": expr, "returnByValue": True, "awaitPromise": awaited})
                if r.get("exceptionDetails"):
                    return None
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            for w, h, phone in WIDTHS:
                label = f"{w}px"
                await call("Emulation.setDeviceMetricsOverride",
                           {"width": w, "height": h, "deviceScaleFactor": 2 if phone else 1,
                            "mobile": phone})
                await call("Emulation.setTouchEmulationEnabled",
                           {"enabled": phone, "maxTouchPoints": 5 if phone else 0})
                await call("Page.navigate", {"url": url})
                ready = False
                for _ in range(60):
                    await asyncio.sleep(0.25)
                    if await js("window.__ready === true"):
                        ready = True
                        break
                if not ready:
                    print(f"SKIP  {label}: the page never finished rendering Notes")
                    return 2

                r = await js(AUDIT)
                if r is None:
                    print(f"SKIP  {label}: page did not evaluate")
                    return 2
                if not r["wrap"]:
                    problems.append((label, "missing-control", "the notes pane did not render"))
                    continue
                if r["overflow"]:
                    problems.append((label, "horizontal-overflow", "the page scrolls sideways"))
                if not r["items"]:
                    problems.append((label, "missing-control", "no notes rendered from the seeded library"))
                for c in (r.get("clipped") or []):
                    problems.append((label, "text-truncated",
                                     f"{c['text']!r} is cut off — {c['shown']}px shown, {c['needs']}px needed"))
                # Tap size is a PHONE question — a 21px row is fine with a mouse, and the desktop
                # tiers are under body{zoom} anyway, so a measured height there isn't a CSS pixel.
                if phone:
                    for s in r["small"]:
                        problems.append((label, "tiny-tap-target",
                                         f"{s['text'] or s['sel']} is {s['h']}px tall"))
                if phone:
                    for z in r["zoomy"]:
                        problems.append((label, "ios-zoom-trap",
                                         f"{z['cls']} is {z['fs']}px — iOS zooms the page on focus"))
                if phone and r["wrapBottom"] > r["navTop"] + 1:
                    problems.append((label, "editor-under-nav",
                                     f"the pane's bottom ({round(r['wrapBottom'])}px) is under the nav "
                                     f"({round(r['navTop'])}px) — 100vh instead of 100dvh?"))

                # Search filters the list, so it has to be ON the list — not behind a drawer.
                if not r["searchVisible"]:
                    problems.append((label, "missing-control", "the search field is not on screen"))
                if phone:
                    # THE ONE THIS SCREEN GOT WRONG: the folder tree was a pane stacked above the
                    # list, capped at 40vh, so folders took half a phone screen at all times and the
                    # notes got what was left.
                    if r["foldersOnScreen"]:
                        problems.append((label, "folder-pane-hogs-screen",
                                         "the folder tree is on screen at rest — it must be a drawer, "
                                         "not a permanent pane, on a phone"))
                    if r["wrapH"] and r["listH"] < r["wrapH"] * 0.8:
                        problems.append((label, "folder-pane-hogs-screen",
                                         f"the note list gets only {r['listH']}px of {r['wrapH']}px"))
                    # A drawer nobody can open is worse than the pane it replaced.
                    if not r["folderBtn"]:
                        problems.append((label, "folders-unreachable",
                                         "no control on the list opens the folder tree"))
                    elif await js(OPEN_DRAWER):
                        await asyncio.sleep(0.4)
                        rd = await js(AUDIT)
                        if not (rd or {}).get("foldersOnScreen"):
                            problems.append((label, "folders-unreachable",
                                             "tapping the folder control did not bring the tree on screen"))
                        if (rd or {}).get("overflow"):
                            problems.append((label, "horizontal-overflow", "the open drawer scrolls the page sideways"))
                        await js(CLOSE_DRAWER)
                        await asyncio.sleep(0.35)
                        if (await js(AUDIT) or {}).get("foldersOnScreen"):
                            problems.append((label, "folders-unreachable",
                                             "the drawer would not close again"))
                else:
                    # Desktop is a three-pane layout and must stay one: the drawer rules must not leak.
                    if not r["foldersOnScreen"]:
                        problems.append((label, "missing-control",
                                         "the folder sidebar is off screen at desktop width"))

                # Open a note: on a phone that must REPLACE the list, not sit beside it.
                if not await js(OPEN_NOTE):
                    problems.append((label, "missing-control", "could not open a note"))
                    continue
                await asyncio.sleep(0.4)
                r2 = await js(AUDIT)
                if phone and r2["listVisible"] and r2["editorVisible"]:
                    problems.append((label, "both-panes-visible",
                                     "the list and the editor are both on screen at phone width"))
                if not r2["editorVisible"]:
                    problems.append((label, "missing-control", "opening a note showed no editor"))
                if phone and not r2["back"]:
                    problems.append((label, "no-way-back",
                                     "the editor is open with no back control"))
                if r2["overflow"]:
                    problems.append((label, "horizontal-overflow", "the open editor scrolls sideways"))
                if phone and r2["bodyBottom"] > r2["navTop"] + 1:
                    problems.append((label, "editor-under-nav",
                                     "the text area runs under the bottom nav"))
                if phone:
                    for z in r2["zoomy"]:
                        problems.append((label, "ios-zoom-trap",
                                         f"{z['cls']} is {z['fs']}px — iOS zooms the page on focus"))

                hb = r2.get("headBtns") or []
                if len(hb) >= 2:
                    widths = {b["w"] for b in hb}
                    heights = {b["h"] for b in hb}
                    if len(widths) > 1 or len(heights) > 1:
                        problems.append((label, "mismatched-buttons",
                                         "the editor's icon buttons are different sizes: " +
                                         ", ".join(f"{b['cls'].split()[-1]} {b['w']}x{b['h']}" for b in hb)))

                # Switching notes mid-debounce must not mix them.
                x = await js(CROSS_SAVE, awaited=True)
                if not x or x.get("error"):
                    problems.append((label, "notes-cross-saved",
                                     f"could not run the switch test ({(x or {}).get('error')})"))
                else:
                    if x["aTags"] is None:
                        problems.append((label, "notes-cross-saved",
                                         "the edit was never saved when the note was switched away from"))
                    else:
                        if "alpha-only" not in (x["aTags"] or []):
                            problems.append((label, "notes-cross-saved",
                                             f"the first note lost its own tags (got {x['aTags']!r})"))
                        if "body of the first note" not in (x["aBody"] or ""):
                            problems.append((label, "notes-cross-saved",
                                             "the first note lost its own body"))
                    if x["leaked"]:
                        problems.append((label, "notes-cross-saved",
                                         "one note's edit was written onto ANOTHER note"))

                # Opening a note full of pictures must not fetch all of them.
                li = await js(LAZY_IMAGES, awaited=True)
                if os.environ.get("PC_DEBUG"): print(f"  DEBUG {label} lazy={li}")
                if not li or li.get("error"):
                    problems.append((label, "image-stampede",
                                     f"could not run the picture test ({(li or {}).get('error')})"))
                else:
                    if not li["total"]:
                        problems.append((label, "image-stampede",
                                         "the picture-heavy note rendered no images at all"))
                    else:
                        if li["onOpen"] > li["total"] * 0.6:
                            problems.append((label, "image-stampede",
                                             f"opening the note fetched {li['onOpen']} of {li['total']} "
                                             "attachments at once — they must load as they come into view"))
                        # …and the strip below the note must not list every one of them either.
                        if (li.get("panes") or {}).get("thumbs", 0) >= li["total"]:
                            problems.append((label, "image-stampede",
                                             "the attachment strip rendered a thumbnail for every "
                                             "attachment — each one is a private download and decrypt"))
                        if not li["loadedTop"]:
                            problems.append((label, "image-stampede",
                                             "no picture loaded at all — lazy has to mean later, not never"))
                        if li["afterScroll"] <= li["onOpen"]:
                            problems.append((label, "image-stampede",
                                             "scrolling to the end of the note loaded no further pictures"))
                        # …and they have to ARRIVE, not merely be requested: a picture that is
                        # fetched and never given a src is the same blank box to the reader.
                        if li["loadedEnd"] <= li["loadedTop"]:
                            problems.append((label, "image-stampede",
                                             f"scrolling requested more pictures but only {li['loadedEnd']} "
                                             "ever appeared"))
                    # The attachment strip is a wrapping flex row whose automatic minimum size is its
                    # content: thirty thumbnails claimed fifteen rows and crushed the note itself,
                    # which has min-height:0, to 36px. The text is what the screen is FOR.
                    p = li.get("panes") or {}
                    if p.get("editor") and p.get("render", 0) < p["editor"] * 0.45:
                        problems.append((label, "attachments-eat-the-note",
                                         f"the note's text gets {p['render']}px of the editor's "
                                         f"{p['editor']}px — the attachment strip takes {p.get('res')}px"))

                # A failed picture must stay a picture, and come back on its own.
                ir = await js(IMAGE_RETRY, awaited=True)
                if not ir or ir.get("error"):
                    problems.append((label, "image-failure-permanent",
                                     f"could not run the retry test ({(ir or {}).get('error')})"))
                else:
                    if ir["tombstones"]:
                        problems.append((label, "image-failure-permanent",
                                         "a failed image was replaced by a text placeholder — the element "
                                         "is gone, so nothing can retry it"))
                    if not ir["survived"]:
                        problems.append((label, "image-failure-permanent",
                                         "the <img> elements did not survive a failed load"))
                    elif not ir["failed"]:
                        problems.append((label, "image-failure-permanent",
                                         "a failed image is not marked as failed — it just looks empty"))
                    if not ir["recovered"]:
                        problems.append((label, "image-failure-permanent",
                                         "no image recovered after the network came back"))

                # A stalled read must not take the queue with it. One width only — it waits out a
                # 20s timer, and the answer cannot differ by viewport.
                if label == "390px":
                    sq = await js(STALLED_QUEUE, awaited=True)
                    if not sq or sq.get("error"):
                        problems.append((label, "queue-wedged",
                                         f"could not run the stall test ({(sq or {}).get('error')})"))
                    elif not sq["later"]:
                        problems.append((label, "queue-wedged",
                                         f"{sq['calls']} reads stalled and nothing loaded afterwards — "
                                         "a dead socket takes every picture in every note with it"))

                if label == "390px":
                    oa = await js(OPEN_ATTACHMENT, awaited=True)
                    if not oa or oa.get("error"):
                        problems.append((label, "attachment-wont-open",
                                         f"could not run the open test ({(oa or {}).get('error')})"))
                    else:
                        if not oa["reservedTab"]:
                            problems.append((label, "attachment-wont-open",
                                             "the tab was not reserved before decrypting, so the "
                                             "browser sees an unsolicited popup and blocks it"))
                        if not oa["downloaded"]:
                            problems.append((label, "attachment-wont-open",
                                             "the popup was refused and nothing reached the user — "
                                             f"toasts: {oa['toasts']}"))

                # A refused save dialog must not try to hold the whole library in memory.
                if label == "390px":
                    bk = await js(BACKUP_REFUSED, awaited=True)
                    if not bk or bk.get("error"):
                        problems.append((label, "backup-unsaveable",
                                         f"could not run the backup test ({(bk or {}).get('error')})"))
                    else:
                        if not bk["downloaded"]:
                            problems.append((label, "backup-unsaveable",
                                             "no save dialog and nothing was downloaded at all"))
                        # Firefox has NO showSaveFilePicker, so this is every Firefox user. Dropping
                        # the attachments there means their backup is not a backup of their library.
                        if not bk["attachmentReads"]:
                            problems.append((label, "backup-unsaveable",
                                             "the attachments were skipped because the browser has no "
                                             "save dialog — they must be written in parts instead"))

                sv = await js(SAVE_API, awaited=True)
                if not sv or sv.get("error"):
                    problems.append((label, "save-api-broken",
                                     f"PCNotes.save could not run ({(sv or {}).get('error')})"))
                else:
                    if not sv.get("onlineId"):
                        problems.append((label, "save-api-broken", "an online save returned no note id"))
                    if not sv.get("hasRes") or not sv.get("hasRef"):
                        problems.append((label, "save-api-broken",
                                         "the attachment was not stored as an encrypted pcres: "
                                         "reference — a linked blob goes blank offline"))
                    if not sv.get("hasTag"):
                        problems.append((label, "save-api-broken", "the tags were dropped"))
                    if not sv.get("offlineInStore"):
                        problems.append((label, "offline-write-lost",
                                         "a note saved through PCNotes.save while offline is NOT in "
                                         "the local cache — publish()'s rollback ate it"))
                    if not sv.get("offlineInPending"):
                        problems.append((label, "offline-write-lost",
                                         "an offline PCNotes.save was not queued to send"))
                    if not sv.get("offlineQueued"):
                        problems.append((label, "save-api-broken",
                                         "an offline save did not tell the caller it was queued, so "
                                         "the UI cannot say the note will sync later"))

                # And the one that isn't about layout at all.
                w3 = await js(OFFLINE_WRITE, awaited=True)
                if not w3 or w3.get("error"):
                    problems.append((label, "offline-write-lost",
                                     f"could not run the offline write ({(w3 or {}).get('error')})"))
                else:
                    if not w3["inStore"]:
                        problems.append((label, "offline-write-lost",
                                         "a note typed while offline is NOT in the local cache — "
                                         "publish()'s rollback ate it"))
                    if not w3["inPending"]:
                        problems.append((label, "offline-write-lost",
                                         "a note typed while offline was not queued to send"))
                    if "sync" not in (w3["state"] or "") and "saved" not in (w3["state"] or ""):
                        problems.append((label, "offline-write-lost",
                                         f"the editor does not say the note was saved (state={w3['state']!r})"))
                print(f"{label}: notes={r['items']} overflow={r['overflow']} "
                      f"tiny={len(r['small'])} zoomy={len(r['zoomy'])} "
                      f"offline_ok={bool(w3 and w3.get('inStore') and w3.get('inPending'))}")

        if problems:
            print("\nREGRESSIONS")
            for label, kind, detail in problems:
                print(f"  [{label}] {kind}: {detail}")
            return 1
        print("OK  notes mobile checks passed")
        return 0
    finally:
        proc.terminate()
        subprocess.run(["rm", "-rf", PROFILE], check=False)


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="notescheck-")
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
    url = f"http://127.0.0.1:{srv.server_port}/index.html"
    try:
        return asyncio.run(drive(url))
    finally:
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
