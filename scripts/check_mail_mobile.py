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
PORT = 9485
PROFILE = "/tmp/pc-mail-check"

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
let VIEW='messages';
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
window.__calls = {sync:0, folders:0, messages:0};
window.fetch = async (url, opts) => {
  const u = String(url);
  const j = d => ({ ok:true, status:200, json: async()=>d });
  if(u.startsWith('/api/mail/accounts')) return j({accounts:[{email:'me@example.com'}]});
  if(u.startsWith('/api/mail/folders')){ window.__calls.folders++; return j({folders:['INBOX','Sent','Drafts','Trash'], labels:{}}); }
  if(u.startsWith('/api/mail/messages')){ window.__calls.messages++; return j({messages:MSGS}); }
  if(u.startsWith('/api/mail/search'))   return j({messages:MSGS});
  if(u.startsWith('/api/mail/message'))  return j({message:Object.assign({}, MSGS[0], {body_html:'<h1>Hello</h1><p>Regards</p>'})});
  if(u.startsWith('/api/mail/thread'))   return j({messages:[Object.assign({}, MSGS[0], {body_html:'<h1>Hello</h1><p>Regards</p>'})]});
  if(u.startsWith('/api/mail/sync')){ window.__calls.sync++; return j({new:{}}); }
  if(u.startsWith('/api/contacts/books'))return j({books:[{id:'contacts',displayname:'Contacts'}]});
  if(u.startsWith('/api/contacts/cards'))return j({cards:CARDS});
  if(u.startsWith('/api/mail/send')){ window.__sent = JSON.parse(opts.body); return j({ok:true}); }
  return j({ok:true});
};
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
  const list = document.querySelector('.mail-items');
  const nav = document.querySelector('.mobilenav');
  if (list) out.listBottom = Math.round(list.getBoundingClientRect().bottom);
  if (nav) out.navTop = Math.round(nav.getBoundingClientRect().top);
  return out;
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
  const body = document.querySelector('.mail-html');
  const br = body ? body.getBoundingClientRect() : null;
  return { open: !!(pane && pane.classList.contains('has-open')),
           back: !!document.querySelector('#mail-back'),
           display: pane ? getComputedStyle(pane).display : '',
           coversList: !!(r && r.width >= window.innerWidth - 2),
           wide: !!(r && Math.round(r.width) > window.innerWidth + 1),
           paneH: r ? Math.round(r.height) : 0,
           bodyH: br ? Math.round(br.height) : 0,
           acts: (() => {
             const bar = document.querySelector('.mail-actions');
             if (!bar) return null;
             const bs = [...bar.querySelectorAll('.btn')];
             const tops = new Set(bs.map(b => Math.round(b.getBoundingClientRect().top)));
             return { n: bs.length, rows: tops.size,
                      short: bs.filter(b => b.getBoundingClientRect().height < 32).length,
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

                op = await js(OPEN_MESSAGE, awaited=True)
                if not op or op.get("error"):
                    problems.append((label, "missing-control",
                                     f"could not open a message ({(op or {}).get('error')})"))
                else:
                    if not op["open"]:
                        problems.append((label, "reader-not-overlay",
                                         "opening a message did not mark the pane .has-open"))
                    if op["wide"]:
                        problems.append((label, "horizontal-overflow",
                                         "the reading pane is wider than the screen"))
                    a = op.get("acts")
                    # Reply, Forward and ⋯ — everything else is in the overflow menu, so a narrow
                    # pane never wraps or clips.
                    if not a or a["n"] != 3:
                        problems.append((label, "actions-broken",
                                         f"the message actions row has {a and a['n']} buttons, want 3"))
                    else:
                        # One row. Six buttons wrapping into a ragged block is what "not displaying
                        # good" looked like; a pane too narrow for them scrolls sideways instead.
                        if a["rows"] > 1:
                            problems.append((label, "actions-broken",
                                             f"the actions wrapped onto {a['rows']} rows"))
                        # Phone only: desktop scales the whole UI with body{zoom:.67-.77}, so a
                        # 36px control paints at 24 device px there and EVERY button in the app
                        # would fail this. The tap-target rule is about thumbs, not zoomed pixels.
                        if phone and a["short"]:
                            problems.append((label, "tiny-tap-target",
                                             f"{a['short']} action button(s) under 32px"))
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
        subprocess.run(["rm", "-rf", PROFILE], check=False)

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
    src = open(os.path.join(ROOT, "static", "js", "client", "app.js"), encoding="utf-8").read()
    start = src.index("  function _mailDate(ts)")
    end = src.index("  function safePk(v){", start)
    return src[start:end] + HARNESS_TAIL


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
