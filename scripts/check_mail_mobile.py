#!/usr/bin/env python3
"""Layout + behaviour check for the EMAIL client, at phone and desktop widths.

    venv-unified/bin/python scripts/check_mail_mobile.py

check_client_mobile.py only ever loads the timeline, so it never opens Messages → 📧 Email. This
drives the real Mail object out of app.js against a stubbed `window.__PC`-style environment and a
stubbed /api/mail + /api/contacts — no server, no relay, no IMAP, no login.

Assertions, each a way a mail client specifically breaks on a phone:

  horizontal-overflow  A three-pane mail layout is the single most likely thing to push a 360px page
                       sideways. Subjects, addresses and sender names are unbounded foreign strings.
  panes-not-collapsed  On a phone the three panes must become one flow: the reading pane is an
                       overlay, not a 1fr column squeezed next to two others.
  reader-not-overlay   Opening a message must cover the list (.mail-read.has-open), and a Back
                       control must exist — otherwise there is no way out of a message on a phone.
  under-nav            The message list's bottom sits under the fixed .mobilenav, so the last mail
                       in the list can never be tapped.
  tiny-tap-target      A list row, folder button or action under 32px.
  ios-zoom-trap        Any text field under 16px: iOS zooms on focus and never zooms back. The
                       compose form and the contact picker's search box are nothing but text fields.
  compose-overflow     The composer is wider than the screen.
  contacts-broken      The 👤 Contacts picker must list contacts that HAVE an email, and picking one
                       must fill the To field. This is the bridge to the Contacts feature; if it
                       silently lists nothing, the composer just looks empty.

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
WIDTHS = [(390, 844, True), (360, 780, True), (1280, 860, False)]
PORT = int(os.environ.get("PC_CHECK_PORT") or 9485)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or f"/tmp/pc-mail-check-{os.getpid()}"

# Long, unbounded strings on purpose: a real inbox is full of them and they are what breaks a phone
# layout. One contact card carries a grouped email, the shape DAVx5 writes.
PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css">
</head><body>
<div class="app" style="display:flex;flex-direction:column;height:100dvh">
  <div id="feed" class="feed"></div>
</div>
<nav class="mobilenav glass"><button class="nav-item"><b>Home</b></button></nav>
<div id="modal-root"></div><div id="toast-root"></div>
<script src="/static/js/client/sprite.js"></script>
<script src="/static/js/client/vcard.js"></script>
<script>
const $  = (s,r)=> (r||document).querySelector(s);
const $$ = (s,r)=> Array.from((r||document).querySelectorAll(s));
const enc = s => String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
window.__toasts = [];
const toast = m => window.__toasts.push(m);
const closeModal = () => { const m=$('#modal-root .modal-bg'); if(m) m.remove();
                           document.body.classList.remove('modal-open'); };
const modal = (html, onMount) => { const bg=document.createElement('div'); bg.className='modal-bg';
  document.body.classList.add('modal-open');
  bg.innerHTML = '<div class="modal glass neon-border">'+html+'</div>';
  $('#modal-root').appendChild(bg); if(onMount) onMount(bg.querySelector('.modal')); };
const switchView = ()=>{};
const _fmtBytes = n => (n||0) + ' B';
const bumpDm = ()=>{};
const ME = { pubkey:'me' };
const CFG = {};
const mediaServer = ()=>'';
const FilesIdx = null;
// This is the AUTHENTICATED layout/interaction scenario. Mail now correctly refuses to issue any
// protected request until the shared session proof succeeds, so the lifted IIFE needs the same
// successful contract the full app supplies (the previous fixture accidentally relied on Mail
// swallowing a missing ensureAiSession binding and continuing tokenless).
const _aiToken = 'mail-harness-token';
const ensureAiSession = async () => ({ username:'mail-harness' });
window.__PC_API_BASE__ = 'https://mail.instance.test';
const _instanceBase = () => window.__PC_API_BASE__;
let VIEW='mail';
const MSGS = [
  { uid:'1', account:'me@example.com', folder:'INBOX', read:false,
    from:'Bartholomew Featherstonehaugh-Cholmondeley <bartholomew.featherstonehaugh@averylongdomainname.example.com>',
    to:'me@example.com', subject:'Quarterly review of the unusually long subject line that will not fit',
    preview:'This preview is also deliberately long so the list row has to clamp it somewhere sensible.',
    ts: Math.floor(Date.now()/1000), attachments:[{name:'report.pdf', size:1234}] },
  { uid:'2', account:'me@example.com', folder:'INBOX', read:true,
    from:'Short Sender <a@b.co>', to:'me@example.com', subject:'Hi', preview:'short one',
    ts: Math.floor(Date.now()/1000)-86400, attachments:[] },
];
const CARDS = [
  { uid:'c1', ics:'BEGIN:VCARD\r\nVERSION:3.0\r\nUID:c1\r\nFN:Ann Zeta\r\nN:Zeta;Ann;;;\r\n'
      +'EMAIL;TYPE=INTERNET:ann@example.com\r\nEND:VCARD\r\n' },
  { uid:'c2', ics:'BEGIN:VCARD\r\nVERSION:3.0\r\nUID:c2\r\nFN:Labelled Person\r\n'
      +'item1.EMAIL;TYPE=INTERNET:labelled@example.com\r\nitem1.X-ABLABEL:School\r\nEND:VCARD\r\n' },
  { uid:'c3', ics:'BEGIN:VCARD\r\nVERSION:3.0\r\nUID:c3\r\nFN:No Email Here\r\nTEL:5551234\r\nEND:VCARD\r\n' },
];
window.__sent = null;
window.__mailErrors = [];
window.addEventListener('error', e => window.__mailErrors.push(String(e.error || e.message || 'error')));
window.addEventListener('unhandledrejection', e => window.__mailErrors.push(String(e.reason || 'rejection')));
window.__calls = {sync:0, folders:0, messages:0};
window.fetch = async (url, opts) => {
  const u = String(url);
  const j = d => ({ ok:true, status:200, json: async()=>d });
  if(u.startsWith('/api/mail/accounts')) return j({accounts:[{email:'me@example.com'}]});
  if(u.startsWith('/api/mail/folders')){ window.__calls.folders++; return j({folders:['INBOX','Sent','Drafts','Trash'], labels:{}}); }
  if(u.startsWith('/api/mail/messages')){ window.__calls.messages++; return j({messages:MSGS}); }
  if(u.startsWith('/api/mail/search'))   return j({messages:MSGS});
  // LONG on purpose: a short mail fits any box, so only a long one can show whether the message is
  // read on the page or through a fixed porthole with its own scrollbar.
  const LONG_HTML = '<h1>Hello</h1>' + Array.from({length:60},(_,i)=>'<p>Paragraph '+i+' of a long message that used to be read through a small scrolling window inside the page.</p>').join('');
  if(u.startsWith('/api/mail/message'))  return j({message:Object.assign({}, MSGS[0], {body_html:LONG_HTML})});
  // A REAL CONVERSATION IS SEVERAL MESSAGES. A one-message thread cannot show whether reading a
  // conversation is a continuous scroll or a stack of cramped boxes.
  if(u.startsWith('/api/mail/thread'))   return j({messages: Array.from({length:6},(_,i)=>Object.assign({}, MSGS[0], {
      uid: String(900+i), message_id:'<m'+i+'@x>', ts: 1700000000+i*3600,
      from: (i%2 ? 'Me <me@example.com>' : 'Sender '+i+' <s'+i+'@example.com>'),
      subject: MSGS[0].subject, body_html: LONG_HTML}))});
  if(u.startsWith('/api/mail/sync')){ window.__calls.sync++; return j({new:{}}); }
  if(u.startsWith('/api/contacts/books'))return j({books:[{id:'contacts',displayname:'Contacts'}]});
  if(u.startsWith('/api/contacts/cards'))return j({cards:CARDS});
  if(u.startsWith('/api/mail/send')){ window.__sent = JSON.parse(opts.body); return j({ok:true}); }
  return j({ok:true});
};
window.__PC = { authFetch:(url,opts)=>window.fetch(url,opts) };
</script>
<script src="/static/js/client/mailharness.js"></script>
</body></html>"""

# The Mail object and its helpers live inside app.js's IIFE, so they are lifted out by name into a
# standalone script. Extracting keeps the test honest: this is the SHIPPED source, not a copy.
HARNESS_TAIL = r"""
window.renderMessages = function(){
  // The shipped email branch of renderMessages, verbatim in behaviour: remount only when it is not
  // already mounted. If this ever rebuilds unconditionally again, the storm below will show it.
  const feed=document.getElementById('feed');
  const mounted=feed.querySelector('#mail-root');
  if(mounted && Mail.root===mounted) return;
  feed.innerHTML='<div id="mail-root" class="mail-root"></div>';
  return Mail.render(feed.querySelector('#mail-root'));
};
window.__mail = Mail;   // the select-all toggle test needs the real object, not the DOM alone
(async function(){
  const root=document.createElement('div'); root.id='mail-root'; root.className='mail-root';
  document.getElementById('feed').appendChild(root);
  await Mail.render(root);
  for(let i=0;i<80 && !document.querySelector('.mail-item'); i++) await new Promise(r=>setTimeout(r,50));
  await new Promise(r=>setTimeout(r,300));
  window.__ready = true;
})();
"""

AUDIT = r"""(() => {
  const out = {overflow:false, items:0, panes:'', readDisplay:'', small:[], zoomy:[],
               listBottom:0, navTop:0, folders:0, hasCompose:false};
  out.overflow = document.documentElement.scrollWidth > window.innerWidth + 1;
  const vis = el => el && (!el.checkVisibility || el.checkVisibility());
  out.items = document.querySelectorAll('.mail-item').length;
  out.folders = document.querySelectorAll('.mail-folder').length;
  out.hasCompose = !!document.querySelector('#mail-compose');
  const wrap = document.querySelector('.mail-wrap');
  if (wrap) out.panes = getComputedStyle(wrap).gridTemplateColumns;
  const read = document.querySelector('.mail-read');
  if (read) out.readDisplay = getComputedStyle(read).display;
  document.querySelectorAll('.mail-item, .mail-folder, .mail-list-top .mini, #mail-compose').forEach(b => {
    if (!vis(b)) return;
    const r = b.getBoundingClientRect();
    if (r.height < 32) out.small.push({cls: String(b.className||b.id).slice(0,24), h: Math.round(r.height)});
  });
  const TEXTY = ['text','search','email','url','tel','number','password',''];
  document.querySelectorAll('input, textarea').forEach(i => {
    if (!vis(i)) return;
    if (i.tagName === 'INPUT' && !TEXTY.includes((i.type||'').toLowerCase())) return;
    const fs = parseFloat(getComputedStyle(i).fontSize);
    if (fs < 16) out.zoomy.push({cls: (i.id || i.className || i.type), fs});
  });
  /* HOW MUCH OF THE PHONE IS ACTUALLY MAIL? Reported as "email UI is terrible on mobile, you get
     less than half the screen now" — which no assertion here could see, because everything was
     measured for overflow and tap size and nothing for the vertical BUDGET. */
  const px = el => { if(!el) return 0; const r=el.getBoundingClientRect();
                     return (!el.checkVisibility || el.checkVisibility()) ? Math.max(0, Math.round(r.height)) : 0; };
  out.vh = window.innerHeight;
  out.chrome = {
    top:     px(document.querySelector('.mail-list-top')),
    folders: px(document.querySelector('.mail-folders')),
    head:    px(document.querySelector('.mail-head')),
    acct:    px(document.querySelector('.mail-accounts')),
    nav:     px(document.querySelector('.mobilenav')),
  };
  out.listH = px(document.querySelector('.mail-items'));
  out.readH = px(document.querySelector('.mail-read'));
  out.listFrac = out.vh ? +(out.listH / out.vh).toFixed(2) : 0;
  out.readFrac = out.vh ? +(out.readH / out.vh).toFixed(2) : 0;
  const list = document.querySelector('.mail-items');
  const nav = document.querySelector('.mobilenav');
  if (list) out.listBottom = Math.round(list.getBoundingClientRect().bottom);
  if (nav) out.navTop = Math.round(nav.getBoundingClientRect().top);
  return out;
})()"""

# Select All has to be a TOGGLE. It selected fine and had no way to undo itself, because the handler
# read the checkbox's own `checked` state — which updateBulk() rewrites on every redraw as
# `n === msgs.length`. So the second half of this test is the one that matters: it grows the message
# list under a full selection (a background sync, "Load older", a folder switch — all of which do
# exactly this), which used to leave everything selected with the box UNCHECKED, so the next press
# read it as "select all" and re-added them. Pressing twice must always end at zero.
SELALL = r"""(async () => {
  const M = window.__mail; if(!M) return {skip:'no Mail object'};
  const sa = document.getElementById('mail-selall'); if(!sa) return {skip:'no select-all control'};
  const tap = () => { sa.click(); };           // a real click, the way a finger arrives
  const n = () => (M.sel ? M.sel.size : 0);
  const sleep = ms => new Promise(r=>setTimeout(r,ms));

  M.sel && M.sel.clear(); M.updateBulk();
  tap(); await sleep(50); const afterFirst = n();
  tap(); await sleep(50); const afterSecond = n();

  // ...and again, with the list having grown underneath the selection.
  M.sel.clear(); M.updateBulk();
  tap(); await sleep(50); const grownFirst = n();
  M.msgs.push({uid:'99', account:'me@example.com', folder:'INBOX', read:true,
               from:'Late Arrival <late@b.co>', to:'me@example.com', subject:'Arrived after you selected',
               preview:'', ts: Math.floor(Date.now()/1000), attachments:[]});
  M.drawList(); await sleep(50);
  tap(); await sleep(50); const grownSecond = n();
  return {afterFirst, afterSecond, grownFirst, grownSecond, total:M.msgs.length};
})()"""

KEYS = r"""(async () => {
  const key = (k) => document.dispatchEvent(
    new KeyboardEvent('keydown', {key:k, bubbles:true, cancelable:true}));
  const cur = () => {
    const c = document.querySelector('.mail-item.cursor');
    return c ? [...document.querySelectorAll('.mail-item')].indexOf(c) : -1;
  };
  key('j'); await new Promise(r=>setTimeout(r,120)); const afterJ = cur();
  key('j'); await new Promise(r=>setTimeout(r,120)); const afterJJ = cur();
  key('k'); await new Promise(r=>setTimeout(r,120)); const afterK = cur();
  // A key aimed at a text field must NOT move the cursor — typing "j" in search is just a letter.
  const q = document.querySelector('#mail-search');
  if (q) { q.focus(); q.dispatchEvent(new KeyboardEvent('keydown',{key:'j',bubbles:true,cancelable:true}));
           await new Promise(r=>setTimeout(r,120)); }
  const afterTyping = cur();
  if (q) q.blur();
  key('Enter'); await new Promise(r=>setTimeout(r,300));
  const opened = !!document.querySelector('.mail-read.has-open');
  key('Escape'); await new Promise(r=>setTimeout(r,250));
  const closed = !document.querySelector('.mail-read.has-open');
  return { afterJ, afterJJ, afterK, afterTyping, opened, closed };
})()"""

BULK_BAR = r"""(async () => {
  const cb = document.querySelector('.mail-item .mi-chk');
  if (!cb) return {error:'no checkbox on a row'};
  cb.click();
  await new Promise(r=>setTimeout(r,250));
  const bar = document.querySelector('.mail-bulk');
  const act = document.querySelector('.mail-bulk-act');
  if (!bar || !act) return {error:'no bulk bar'};
  const br = bar.getBoundingClientRect();
  const bs = [...act.querySelectorAll('.btn')];
  return { n: bs.length,
           labels: bs.map(b => b.textContent.trim()),
           // Past the BAR's right edge, or hidden inside a scroll container: either way the action
           // cannot be reached. The list column is ~330px, so this is the tight case.
           clipped: bs.filter(b => b.getBoundingClientRect().right > br.right + 1).length,
           hidden: act.scrollWidth > act.clientWidth + 1,
           barW: Math.round(br.width),
           barBottom: Math.round(br.bottom), barTop: Math.round(br.top) };
})()"""

OPEN_MESSAGE = r"""(async () => {
  const c = document.querySelector('.mail-item .mi-content');
  if (!c) return {error:'no message row'};
  c.click();
  for (let i=0;i<80 && !document.querySelector('.mail-read.has-open'); i++) await new Promise(r=>setTimeout(r,50));
  await new Promise(r=>setTimeout(r,250));
  const pane = document.querySelector('.mail-read');
  const r = pane ? pane.getBoundingClientRect() : null;
  // How much of the reading pane the message body actually occupies. A fixed-height body leaves a
  // band of dead pane under every short mail and letterboxes every long one.
  // THE OPEN MESSAGE'S body. With a multi-message thread the first `.mail-html` belongs to a
  // COLLAPSED message and is display:none, so measuring it reports 0 and fails the UI for a
  // fault in the probe.
  const body = document.querySelector('.mail-msg.open .mail-html') || document.querySelector('.mail-html');
  const br = body ? body.getBoundingClientRect() : null;
  return { open: !!(pane && pane.classList.contains('has-open')),
           errors: window.__mailErrors.slice(),
           paneText: pane ? pane.textContent.trim().slice(0,160) : '',
           back: !!document.querySelector('#mail-back'),
           display: pane ? getComputedStyle(pane).display : '',
           coversList: !!(r && r.width >= window.innerWidth - 2),
           wide: !!(r && Math.round(r.width) > window.innerWidth + 1),
           paneH: r ? Math.round(r.height) : 0,
           bodyH: br ? Math.round(br.height) : 0,
           /* THE VERTICAL BUDGET WHILE READING — "you get less than half the screen".
              vh is the whole phone; paneFrac is how much of it the open mail occupies, and
              bodyFrac how much is the message itself rather than headers, actions and app chrome. */
           vh: window.innerHeight,
           paneFrac: r ? +(r.height / window.innerHeight).toFixed(2) : 0,
           bodyFrac: br ? +(br.height / window.innerHeight).toFixed(2) : 0,
           /* WHERE THE READING SCREEN'S PIXELS GO. The pane is position:fixed inset:0 on a phone,
              so it already owns the whole viewport — the list chrome behind it is covered, not
              competing. What matters is how the pane divides itself up. */
           /* HOW A CONVERSATION READS. Reported as "make reading email threads like infinite
              scroll, you are cramming everything into a tiny space". */
           convo: (() => {
             const msgs = [...document.querySelectorAll('.mail-msg')];
             const h = e => Math.round(e.getBoundingClientRect().height);
             return { count: msgs.length,
                      open: msgs.filter(m => m.classList.contains('open')).length,
                      heights: msgs.map(h),
                      threadScrollH: (() => { const t=document.querySelector('.mail-thread');
                        return t ? Math.round(t.scrollHeight) : 0; })(),
                      threadH: (() => { const t=document.querySelector('.mail-thread');
                        return t ? Math.round(t.getBoundingClientRect().height) : 0; })() };
           })(),
           layout: (() => {
             const box = sel => { const e=document.querySelector(sel); if(!e) return null;
               const r=e.getBoundingClientRect();
               return {t:Math.round(r.top), b:Math.round(r.bottom), h:Math.round(r.height)}; };
             return { vh: window.innerHeight, wrap: box('.mail-wrap'), read: box('.mail-read'),
                      thread: box('.mail-thread'), root: box('.mail-root'), feed: box('#feed'),
                      items: box('.mail-items') };
           })(),
           paneKids: (() => {
             const pane = document.querySelector('.mail-read'); if(!pane) return [];
             return [...pane.querySelectorAll('*')].filter(e => e.parentElement === pane
                      || (e.parentElement && e.parentElement.parentElement === pane))
               .map(e => ({ c: String(e.className||e.tagName).slice(0,28),
                            h: Math.round(e.getBoundingClientRect().height) }))
               .filter(x => x.h > 8).slice(0, 14);
           })(),
           parts: (() => {
             const h = sel => { const e=document.querySelector(sel); if(!e) return null;
               const r=e.getBoundingClientRect(); return Math.round(r.height); };
             return { head:h('.mail-read-head'), subj:h('.mail-subject'), meta:h('.mail-meta'),
                      acts:h('.mail-actions'), body:h('.mail-html'), text:h('.mail-text'),
                      top:h('.mail-list-top'), folders:h('.mail-folders'), nav:h('.mobilenav'),
                      back:h('#mail-back') };
           })(),
           listStillThere: (() => { const l=document.querySelector('.mail-items');
             if(!l) return 0; const lr=l.getBoundingClientRect();
             return (!l.checkVisibility || l.checkVisibility()) ? Math.round(lr.height) : 0; })(),
           attachment: (() => {
             const a = document.querySelector('.mail-att');
             return a ? { href:a.href, host:new URL(a.href).host } : null;
           })(),
           acts: (() => {
             const bar = document.querySelector('.mail-actions');
             if (!bar) return null;
             const bs = [...bar.querySelectorAll('.btn')];
             const tops = new Set(bs.map(b => Math.round(b.getBoundingClientRect().top)));
             const br = bar.getBoundingClientRect();
             const widths = bs.map(b => Math.round(b.getBoundingClientRect().width));
             return { n: bs.length, rows: tops.size,
                      barH: Math.round(br.height),
                      widths, usedW: widths.reduce((n,w)=>n+w,0) + Math.max(0,bs.length-1)*6,
                      short: bs.filter(b => b.getBoundingClientRect().height < 32).length,
                      // A button whose right edge is past the row's is CUT OFF — which is what a
                      // 112px minimum column did to Delete on a narrow reading pane.
                      clipped: bs.filter(b => b.getBoundingClientRect().right > br.right + 1).length,
                      // "Filled" means a solid colour OR a gradient — the primary and destructive
                      // buttons paint with background-image, where backgroundColor reads transparent.
                      unfilled: bs.filter(b => {
                        const st = getComputedStyle(b);
                        const solid = st.backgroundColor !== 'rgba(0, 0, 0, 0)'
                                   && st.backgroundColor !== 'transparent';
                        const grad = (st.backgroundImage || 'none') !== 'none';
                        return !solid && !grad;
                      }).length,
                      overflows: bar.scrollWidth > bar.clientWidth + 1 };
           })(),
           hdrOverflow: (() => {
             const hd = document.querySelector('.mail-msg-hd');
             return hd ? hd.scrollWidth > hd.clientWidth + 1 : false;
           })() };
})()"""

COMPOSE_CONTACTS = r"""(async () => {
  const btn = document.querySelector('#mail-compose');
  if (!btn) return {error:'no compose button'};
  btn.click();
  await new Promise(r=>setTimeout(r,200));
  const m = document.querySelector('#modal-root .modal');
  if (!m) return {error:'composer did not open'};
  const mr = m.getBoundingClientRect();
  const wide = Math.round(mr.width) > window.innerWidth + 1;
  // A composer is a window you write pages in, not a dialog. Measure how much of the screen it takes
  // and how much of ITSELF the message body gets.
  const bodyEl = m.querySelector('#cm-body');
  const br = bodyEl ? bodyEl.getBoundingClientRect() : null;
  const fill = { w: mr.width / window.innerWidth, h: mr.height / window.innerHeight,
                 bodyH: br ? Math.round(br.height) : 0, boxH: Math.round(mr.height) };
  const TEXTY = ['text','search','email','url','tel','number','password',''];
  const small = [...m.querySelectorAll('input, textarea')]
    .filter(i => !(i.tagName === 'INPUT' && !TEXTY.includes((i.type||'').toLowerCase())))
    .filter(i => parseFloat(getComputedStyle(i).fontSize) < 16)
    .map(i => (i.id||i.type) + ':' + getComputedStyle(i).fontSize);
  const cbtn = m.querySelector('#cm-contacts');
  if (!cbtn) return {wide, small, error:'no Contacts button in the composer'};
  cbtn.click();
  for (let i=0;i<80 && !document.querySelector('.mc-item'); i++) await new Promise(r=>setTimeout(r,50));
  await new Promise(r=>setTimeout(r,200));
  const rows = [...document.querySelectorAll('.mc-item .mc-mail')].map(e => e.textContent.trim());
  const tiny = [...document.querySelectorAll('.mc-item')]
    .filter(e => e.getBoundingClientRect().height < 32).length;
  const qs = document.querySelector('#mc-q');
  const qFont = qs ? parseFloat(getComputedStyle(qs).fontSize) : 99;
  const first = document.querySelector('.mc-item');
  if (first) first.click();
  await new Promise(r=>setTimeout(r,200));
  const to = document.querySelector('#cm-to');
  const picked = to ? to.value : '';
  // Type-ahead: two characters of a known contact must offer it, and Enter must complete it into
  // the field being edited without eating the recipient already there.
  to.value = 'someone@else.test, ann';
  to.focus();
  to.dispatchEvent(new Event('input', {bubbles:true}));
  for (let i=0;i<60 && !document.querySelector('.mc-auto-item'); i++) await new Promise(r=>setTimeout(r,50));
  const suggested = [...document.querySelectorAll('.mc-auto-item .mc-mail')].map(e => e.textContent.trim());
  const tinyAuto = [...document.querySelectorAll('.mc-auto-item')]
    .filter(e => e.getBoundingClientRect().height < 32).length;
  const firstAuto = document.querySelector('.mc-auto-item');
  if (firstAuto) firstAuto.dispatchEvent(new MouseEvent('mousedown', {bubbles:true}));
  await new Promise(r=>setTimeout(r,150));
  return { wide, small, rows, tiny, qFont, to: picked, fill,
           suggested, tinyAuto, completed: to.value };
})()"""


RENDER_STORM = r"""(async () => {
  const before = JSON.parse(JSON.stringify(window.__calls));
  for (let i = 0; i < 12; i++) { renderMessages(); await new Promise(r=>setTimeout(r,40)); }
  await new Promise(r=>setTimeout(r,400));
  return { before, after: window.__calls,
           mounted: !!document.querySelector('#mail-root .mail-wrap') };
})()"""


async def drive(url):
    import websockets  # noqa: F401,F811
    shutil.rmtree(PROFILE, ignore_errors=True)
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
                        print("  DEBUG:", json.dumps(r["exceptionDetails"])[:500])
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
                for _ in range(80):
                    await asyncio.sleep(0.25)
                    if await js("window.__ready === true"):
                        ready = True
                        break
                if not ready:
                    print(f"SKIP  {label}: the mail client never finished rendering")
                    return 2

                r = await js(AUDIT)
                if r is None:
                    print(f"SKIP  {label}: page did not evaluate")
                    return 2
                if os.environ.get("PC_DEBUG"):
                    print(f"  DEBUG {label}: vh={r.get('vh')} list={r.get('listH')}"
                          f"({r.get('listFrac')}) read={r.get('readH')}({r.get('readFrac')}) "
                          f"chrome={r.get('chrome')}", flush=True)
                if r["overflow"]:
                    problems.append((label, "horizontal-overflow",
                                     "a subject or address scrolls the page sideways"))
                if r["items"] != 2:
                    problems.append((label, "list-empty", f"{r['items']} message rows, want 2"))
                if not r["hasCompose"] or r["folders"] < 3:
                    problems.append((label, "missing-control",
                                     f"compose={r['hasCompose']} folders={r['folders']}"))
                if phone:
                    # Three columns on a 360px screen is ~120px each — the collapse is the feature.
                    cols = [c for c in (r["panes"] or "").split() if c not in ("none", "")]
                    if len(cols) > 1:
                        problems.append((label, "panes-not-collapsed",
                                         f"the mail grid is still {len(cols)} columns ({r['panes']})"))
                    if r["readDisplay"] != "none":
                        problems.append((label, "panes-not-collapsed",
                                         f"the reading pane is displayed ({r['readDisplay']}) "
                                         "instead of waiting to become an overlay"))
                    for t in r["small"]:
                        problems.append((label, "tiny-tap-target", f"{t['cls']} is {t['h']}px tall"))
                    for z in r["zoomy"]:
                        problems.append((label, "ios-zoom-trap", f"{z['cls']} is {z['fs']}px"))
                    if r["listBottom"] > r["navTop"] + 1:
                        problems.append((label, "under-nav",
                                         f"the message list's bottom ({r['listBottom']}px) is under "
                                         f"the nav ({r['navTop']}px)"))

                sel = await js(SELALL, awaited=True)
                if not sel or sel.get("skip"):
                    problems.append((label, "selall-missing",
                                     (sel or {}).get("skip", "the select-all test did not run")))
                else:
                    if sel["afterFirst"] < 2:
                        problems.append((label, "selall-broken",
                                         f"Select All selected {sel['afterFirst']} of 2 messages"))
                    if sel["afterSecond"] != 0:
                        problems.append((label, "selall-broken",
                                         f"pressing Select All twice left {sel['afterSecond']} "
                                         f"selected — it must unselect them all"))
                    if sel["grownSecond"] != 0:
                        problems.append((label, "selall-broken",
                                         f"with a message arriving after Select All ({sel['grownFirst']} "
                                         f"selected, list grew to {sel['total']}), pressing it again left "
                                         f"{sel['grownSecond']} selected instead of clearing"))

                kb = await js(KEYS, awaited=True)
                if not kb:
                    problems.append((label, "keys-broken", "the keyboard test did not run"))
                else:
                    if kb["afterJ"] != 0 or kb["afterJJ"] != 1 or kb["afterK"] != 0:
                        problems.append((label, "keys-broken",
                                         f"j/k moved the cursor to {kb['afterJ']}/{kb['afterJJ']}/"
                                         f"{kb['afterK']}, want 0/1/0"))
                    if kb["afterTyping"] != kb["afterK"]:
                        problems.append((label, "keys-broken",
                                         "typing a letter in the search box moved the message cursor"))
                    if not kb["opened"]:
                        problems.append((label, "keys-broken", "Enter did not open the message"))
                    if not kb["closed"]:
                        problems.append((label, "keys-broken", "Escape did not close the message"))

                bb = await js(BULK_BAR, awaited=True)
                if not bb or bb.get("error"):
                    problems.append((label, "bulk-bar-broken", f"{(bb or {}).get('error')}"))
                else:
                    if bb["n"] != 3:
                        problems.append((label, "bulk-bar-broken",
                                         f"selecting a message showed {bb['n']} bulk actions, want 3"))
                    if bb["clipped"] or bb["hidden"]:
                        problems.append((label, "bulk-bar-broken",
                                         f"{bb['clipped']} bulk action(s) cut off in a "
                                         f"{bb['barW']}px bar ({bb['labels']})"))

                op = await js(OPEN_MESSAGE, awaited=True)
                _c = (op or {}).get("convo") or {}
                if phone and _c.get("count", 0) > 1:
                    # A CONVERSATION SCROLLS; IT DOES NOT SQUASH. `.mail-thread` is a flex column, so
                    # its messages shrink to fit unless told not to. Measured before the fix on a
                    # six-message thread: every collapsed message squeezed from its 58px header down
                    # to TEN PIXELS — unreadable and barely tappable. Reported as "you are cramming
                    # everything into a tiny space".
                    _small = [h for h in _c.get("heights", [])[:-1] if h < 40]
                    if _small:
                        problems.append((label, "conversation-squashed",
                                         f"collapsed messages are {_small}px tall — a thread is "
                                         f"being squeezed into the visible box instead of scrolling"))
                    if _c.get("threadScrollH", 0) <= _c.get("threadH", 0):
                        problems.append((label, "conversation-not-scrolling",
                                         "a multi-message thread fits its box exactly, which means "
                                         "it is being shrunk to fit rather than scrolled"))
                if phone and op and op.get("vh"):
                    # THE VERTICAL BUDGET, asserted. Reported as "email UI is terrible on mobile,
                    # you get less than half the screen" — and nothing here could see it, because
                    # every existing assertion was about overflow, tap size and button rows, and
                    # none about how much of the phone the MAIL gets. Measured at the time of
                    # writing: pane 100% of the viewport, body 62% of it. The floor is set below
                    # what was measured, so it catches a regression rather than pinning a pixel.
                    if op.get("paneFrac", 0) < 0.9:
                        problems.append((label, "reading-pane-too-small",
                                         f"the open mail covers {op['paneFrac']} of the screen — "
                                         f"on a phone it should be the whole of it"))
                    if op.get("bodyFrac", 0) < 0.5:
                        problems.append((label, "message-body-squeezed",
                                         f"the message itself gets {op['bodyFrac']} of the screen "
                                         f"({op.get('bodyH')}px of {op.get('vh')}px) — the rest is "
                                         f"headers, actions and chrome"))
                if op is not None:
                    _errs = op.get("errors") or []
                    print(f"  ERRORS {label}: {len(_errs)} page error(s)"
                          + (f" — first: {str(_errs[0])[:90]}" if _errs else ""), flush=True)
                if os.environ.get("PC_DEBUG") and op:
                    print(f"  DEBUG {label} OPEN: vh={op.get('vh')} pane={op.get('paneH')}"
                          f"({op.get('paneFrac')}) body={op.get('bodyH')}({op.get('bodyFrac')}) "
                          f"listLeft={op.get('listStillThere')} acts={(op.get('acts') or {}).get('barH')} "
                          f"convo={op.get('convo')}",
                          flush=True)
                if not op or op.get("error"):
                    problems.append((label, "missing-control",
                                     f"could not open a message ({(op or {}).get('error')})"))
                else:
                    if op.get("errors"):
                        problems.append((label, "reader-error", "; ".join(op["errors"])))
                    if not op["open"]:
                        problems.append((label, "reader-not-overlay",
                                         "opening a message did not mark the pane .has-open"))
                    if op["wide"]:
                        problems.append((label, "horizontal-overflow",
                                         "the reading pane is wider than the screen"))
                    a = op.get("acts")
                    # Reply, Reply all, Forward, ✨ AI (the menu holding Summarize / AI reply /
                    # Add to Budget), Unread, Move, Delete. New AI actions join the MENU, never the
                    # row — the row is a grid, and every extra button costs a phone a column.
                    if not a or a["n"] != 7:
                        problems.append((label, "actions-broken",
                                         f"the message actions row has {a and a['n']} buttons, want 7"))
                    else:
                        # One row. Six buttons wrapping into a ragged block is what "not displaying
                        # good" looked like; a pane too narrow for them scrolls sideways instead.
                        if a["rows"] > 2:
                            problems.append((label, "actions-broken",
                                             f"the actions spread over {a['rows']} rows"))
                        if a["overflows"]:
                            problems.append((label, "actions-broken",
                                             "the actions row is clipped — buttons out of reach"))
                        if a.get("clipped"):
                            problems.append((label, "actions-broken",
                                             f"{a['clipped']} action button(s) run past the pane edge"))
                        if a.get("unfilled"):
                            problems.append((label, "actions-broken",
                                             f"{a['unfilled']} action button(s) have no fill — the row "
                                             "should read as one control strip"))
                        # Phone only: desktop scales the whole UI with body{zoom:.67-.77}, so a
                        # 36px control paints at 24 device px there and EVERY button in the app
                        # would fail this. The tap-target rule is about thumbs, not zoomed pixels.
                        if phone and a["short"]:
                            problems.append((label, "tiny-tap-target",
                                             f"{a['short']} action button(s) under 32px"))
                        if phone and (a["rows"] != 1 or a["barH"] > 54):
                            problems.append((label, "actions-oversized",
                                             f"message actions occupy {a['rows']} rows / {a['barH']}px; "
                                             "the mobile reader needs one compact toolbar"))
                        if not phone and (max(a["widths"]) > 48 or a["usedW"] > 360):
                            problems.append((label, "actions-oversized",
                                             f"desktop icon actions grew to {a['widths']}px / "
                                             f"{a['usedW']}px total instead of a compact toolbar"))
                    att = op.get("attachment")
                    if not att or att.get("host") != "mail.instance.test":
                        problems.append((label, "attachment-wrong-origin",
                                         f"attachment resolved to {(att or {}).get('href')!r}, "
                                         "not the configured mail instance"))
                    if op.get("hdrOverflow"):
                        problems.append((label, "horizontal-overflow",
                                         "a long From/To pushes the message header out of the pane"))
                    # The body must use most of the pane it is given. This is a plain-text stub
                    # message, so the floor is deliberately modest — it catches a body pinned to a
                    # fixed height inside a much taller pane, not a short mail.
                    if op["paneH"] > 300 and op["bodyH"] < 0.4 * op["paneH"]:
                        problems.append((label, "reader-not-maximised",
                                         f"the message body is {op['bodyH']}px inside a "
                                         f"{op['paneH']}px pane"))
                    if phone:
                        if not op["coversList"]:
                            problems.append((label, "reader-not-overlay",
                                             "the open message does not cover the list"))
                        if not op["back"]:
                            problems.append((label, "reader-not-overlay",
                                             "no Back control — a phone cannot leave the message"))

                st = await js(RENDER_STORM, awaited=True)
                if st:
                    dsync = st["after"]["sync"] - st["before"]["sync"]
                    dfold = st["after"]["folders"] - st["before"]["folders"]
                    if not st["mounted"]:
                        problems.append((label, "render-loop",
                                         "the mail client was torn down by a re-render"))
                    # Twelve re-renders must cost NOTHING: the client is already mounted. One full
                    # IMAP sync per arriving DM is the loop that made the desktop app unusable.
                    if dsync or dfold:
                        problems.append((label, "render-loop",
                                         f"12 re-renders fired {dsync} sync(s) and {dfold} folder "
                                         "fetch(es) — remounting on every event"))

                cc = await js(COMPOSE_CONTACTS, awaited=True)
                if not cc or cc.get("error"):
                    problems.append((label, "contacts-broken",
                                     f"{(cc or {}).get('error') or 'composer/contacts failed'}"))
                else:
                    if cc["wide"]:
                        problems.append((label, "compose-overflow",
                                         "the composer is wider than the screen"))
                    f = cc.get("fill") or {}
                    # Phone: the whole screen. Desktop: a real window, not a 720px dialog.
                    want_h = 0.9 if phone else 0.6
                    want_w = 0.98 if phone else 0.55
                    if f.get("h", 0) < want_h or f.get("w", 0) < want_w:
                        problems.append((label, "compose-too-small",
                                         f"the composer is {f.get('w',0)*100:.0f}% x "
                                         f"{f.get('h',0)*100:.0f}% of the screen"))
                    # …and the message body must own most of that window, not a fixed nine rows.
                    if f.get("boxH", 0) and f.get("bodyH", 0) < 0.3 * f["boxH"]:
                        problems.append((label, "compose-too-small",
                                         f"the message body is {f.get('bodyH')}px inside a "
                                         f"{f.get('boxH')}px composer"))
                    # Two of the three stub cards have an email; the third must not be offered.
                    if sorted(cc["rows"]) != ["ann@example.com", "labelled@example.com"]:
                        problems.append((label, "contacts-broken",
                                         f"the picker listed {cc['rows']!r} — a grouped item1.EMAIL "
                                         "must be found and a card with no email must not appear"))
                    if "ann@example.com" not in (cc["to"] or ""):
                        problems.append((label, "contacts-broken",
                                         f"picking a contact left To as {cc['to']!r}"))
                    if cc.get("suggested") != ["ann@example.com"]:
                        problems.append((label, "autocomplete-broken",
                                         f"typing 'ann' after an existing recipient offered "
                                         f"{cc.get('suggested')!r}"))
                    done = cc.get("completed") or ""
                    if "someone@else.test" not in done or "ann@example.com" not in done:
                        problems.append((label, "autocomplete-broken",
                                         f"completing overwrote the field: {done!r}"))
                    if phone and cc.get("tinyAuto"):
                        problems.append((label, "tiny-tap-target",
                                         f"{cc['tinyAuto']} autocomplete row(s) under 32px"))
                    if phone:
                        if cc["tiny"]:
                            problems.append((label, "tiny-tap-target",
                                             f"{cc['tiny']} contact row(s) under 32px"))
                        if cc["qFont"] < 16:
                            problems.append((label, "ios-zoom-trap",
                                             f"the contact search box is {cc['qFont']}px"))
                        for s in cc["small"]:
                            problems.append((label, "ios-zoom-trap", f"composer field {s}"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        shutil.rmtree(PROFILE, ignore_errors=True)

    if problems:
        print(f"FAIL  {len(problems)} problem(s):")
        for label, kind, msg in problems:
            print(f"  [{label}] {kind}: {msg}")
        return 1
    print("OK  email mobile checks passed")
    return 0


def _harness_js():
    """Lift the Mail object and its helpers out of app.js by name.

    app.js is one big IIFE, so the client cannot be imported. Slicing the shipped source keeps this
    test pointed at the real code — a copy would drift the moment either side changed.
    """
    app_js = os.environ.get("PC_INSTALLED_APP_JS") or os.path.join(
        ROOT, "static", "js", "client", "app.js")
    src = open(app_js, encoding="utf-8").read()
    # Mail attachment rendering shares the shipped file-preview classifier. Keep that helper in the
    # lifted harness too: otherwise the browser check fails before it can render the toolbar/body,
    # while production (where the helper is in the same IIFE) works normally.
    preview_start = src.index("  const _PREVIEW_EXT =")
    preview_end = src.index("  /* Blossom implementations disagree", preview_start)
    start = src.index("  function _mailDate(ts)")
    end = src.index("  function safePk(v){", start)
    return src[preview_start:preview_end] + src[start:end] + HARNESS_TAIL


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="mailcheck-")
    with open(os.path.join(tmp, "index.html"), "w") as fh:
        fh.write(PAGE)
    try:
        harness = _harness_js()
    except ValueError as e:
        print(f"SKIP  could not lift the Mail client out of app.js ({e})")
        return 2
    with open(os.path.join(tmp, "mailharness.js"), "w") as fh:
        fh.write(harness)

    class H(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            path = path.split("?")[0].split("#")[0]
            if path == "/static/js/client/mailharness.js":
                return os.path.join(tmp, "mailharness.js")
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
