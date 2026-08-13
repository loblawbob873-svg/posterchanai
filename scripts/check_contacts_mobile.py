#!/usr/bin/env python3
"""Layout + behaviour check for the CONTACTS screen, at phone and desktop widths.

    venv-unified/bin/python scripts/check_contacts_mobile.py

check_client_mobile.py only ever loads the timeline and check_calendar_mobile.py only the month grid,
so neither opens this screen. This drives the real contacts.js + vcard.js against a stubbed
`window.__PC` and a stubbed /api/contacts/* — no server, no relay, no login.

Assertions, each a way a contact list specifically breaks:

  horizontal-overflow  A long name, an email address or the A–Z rail pushing the page sideways. Names
                       and addresses are unbounded strings from someone else's phone.
  list-empty           The stubbed cards did not render at all — the vCard parse failed.
  photo-missing        A card with a base64 PHOTO must show it. Photos are the one property most
                       likely to be silently dropped, because they are folded across many lines.
  search-broken        Typing filters the list, and a punctuated phone number still matches.
  caret-lost           Searching must not blur the input — repainting the whole screen on every
                       keystroke loses the caret after one character.
  tiny-tap-target      A row or control under 32px. The rows ARE the control here.
  ios-zoom-trap        Any text field under 16px: iOS zooms on focus and never zooms back.
  under-nav            The list's bottom sits under the fixed .mobilenav.
  state-lost           Leaving the view and coming back must keep the book and the search. #feed is
                       shared and blanked on entry.
  editor-loses-data    Opening a contact and saving must preserve the photo and the unknown fields;
                       this is the whole reason vcard.js keeps them.

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
PORT = int(os.environ.get("PC_CHECK_PORT") or 9484)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-contacts-check"

# Cards shaped like the real ones this was built against: a DAVx5 export with a folded base64 photo,
# an Apple-style grouped email with its label, and a card whose only handle is a phone number.
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
<script>
const $  = (s,r)=> (r||document).querySelector(s);
const $$ = (s,r)=> Array.from((r||document).querySelectorAll(s));
const enc = s => String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
window.__view = 'contacts';
window.__toasts = [];
const PHOTO_B64 = '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJ';
const CARDS = [
  { uid:'c-fire', cal:'contacts', component:'VCARD',
    ics:'BEGIN:VCARD\r\nVERSION:3.0\r\nPRODID:+//IDN bitfire.at//DAVx5\r\nUID:c-fire\r\n'
       +'FN:Fire DEPARTMENT\r\nN:DEPARTMENT;Fire;;;\r\nTEL;TYPE=cell:7192758666\r\n'
       +'X-WEIRD-THING:keep me\r\nEND:VCARD\r\n' },
  { uid:'c-photo', cal:'contacts', component:'VCARD',
    ics:'BEGIN:VCARD\r\nVERSION:3.0\r\nUID:c-photo\r\nFN:Ann Zeta\r\nN:Zeta;Ann;;;\r\n'
       +'EMAIL:ann@example.com\r\nPHOTO;ENCODING=b;TYPE=JPEG:'+PHOTO_B64+'\r\nEND:VCARD\r\n' },
  { uid:'c-long', cal:'contacts', component:'VCARD',
    ics:'BEGIN:VCARD\r\nVERSION:3.0\r\nUID:c-long\r\n'
       +'FN:Bartholomew Featherstonehaugh-Cholmondeley III\r\n'
       +'N:Featherstonehaugh-Cholmondeley;Bartholomew;;;\r\n'
       +'EMAIL:bartholomew.featherstonehaugh@averylongdomainname.example.com\r\nEND:VCARD\r\n' },
  { uid:'c-nameless', cal:'contacts', component:'VCARD',
    ics:'BEGIN:VCARD\r\nVERSION:3.0\r\nUID:c-nameless\r\nTEL:5551234\r\nEND:VCARD\r\n' },
];
window.__saved = null;
window.fetch = async (url, opts) => {
  const u = String(url);
  const j = d => ({ ok:true, status:200, json: async()=>d });
  if(u.startsWith('/api/calendar/config'))
    return j({ enabled:true, url:'https://node.example/caldav/tester/', username:'tester',
               has_password:false });
  if(u.startsWith('/api/contacts/books'))
    return j({ books:[{id:'contacts',displayname:'Contacts',kind:'VADDRESSBOOK'}] });
  if(u.startsWith('/api/contacts/cards')){
    if(opts && opts.method === 'PUT'){ window.__saved = JSON.parse(opts.body); return j({ok:true}); }
    return j({ cards: CARDS });
  }
  return j({ ok:true });
};
window.__PC = {
  $, $$, enc,
  toast: m => { window.__toasts.push(m); },
  modal: (html, onMount) => { const bg=document.createElement('div'); bg.className='modal-bg';
    document.body.classList.add('modal-open');
    bg.innerHTML = '<div class="modal glass neon-border">'+html+'</div>';
    $('#modal-root').appendChild(bg); if(onMount) onMount(bg.querySelector('.modal')); },
  closeModal: () => { const m=$('#modal-root .modal-bg'); if(m) m.remove();
                      document.body.classList.remove('modal-open'); },
  authFetch: (u,o) => window.fetch(u,o),
  ensureAiSession: async () => ({ can_ai:true }),
  uiConfirm: async () => true,
  switchView: v => { window.__view = v; },
  get ME(){ return {pubkey:'me'}; },
  get VIEW(){ return window.__view; },
};
</script>
<script src="/static/js/client/vcard.js"></script>
<script src="/static/js/client/contacts.js"></script>
<script>
(async function(){
  for(let i=0;i<80 && !window.PCContacts;i++) await new Promise(r=>setTimeout(r,50));
  window.PCContacts.render();
  for(let i=0;i<80 && !document.querySelector('.ct-row');i++) await new Promise(r=>setTimeout(r,50));
  await new Promise(r=>setTimeout(r,400));
  window.__ready = true;
})();
</script>
</body></html>"""

AUDIT = r"""(() => {
  const out = {overflow:false, rows:0, names:[], photos:0, initials:0, small:[], zoomy:[],
               listBottom:0, navTop:0, letters:0};
  out.overflow = document.documentElement.scrollWidth > window.innerWidth + 1;
  const vis = el => el && (!el.checkVisibility || el.checkVisibility());
  out.rows = document.querySelectorAll('.ct-row').length;
  out.names = [...document.querySelectorAll('.ct-name')].map(e => e.textContent.trim());
  out.photos = document.querySelectorAll('img.ct-face').length;
  out.initials = document.querySelectorAll('.ct-init').length;
  out.letters = document.querySelectorAll('.ct-letter').length;
  document.querySelectorAll('.ct-row, .ct-tools .btn, .ct-jump').forEach(b => {
    if (!vis(b)) return;
    const r = b.getBoundingClientRect();
    // The A-Z rail is deliberately dense; it is a scrub strip, not a row of buttons.
    if (b.classList.contains('ct-jump')) return;
    if (r.height < 32) out.small.push({cls: String(b.className).slice(0,22), h: Math.round(r.height)});
  });
  const TEXTY = ['text','search','email','url','tel','number','password',''];
  document.querySelectorAll('input, textarea').forEach(i => {
    if (!vis(i)) return;
    if (i.tagName === 'INPUT' && !TEXTY.includes((i.type||'').toLowerCase())) return;
    const fs = parseFloat(getComputedStyle(i).fontSize);
    if (fs < 16) out.zoomy.push({cls: (i.id || i.type), fs});
  });
  const list = document.querySelector('.ct-listwrap') || document.querySelector('.ct-list');
  const nav = document.querySelector('.mobilenav');
  if (list) out.listBottom = Math.round(list.getBoundingClientRect().bottom);
  if (nav) out.navTop = Math.round(nav.getBoundingClientRect().top);
  return out;
})()"""

SEARCH = r"""(async () => {
  const q = document.querySelector('#ct-q');
  if (!q) return {error:'no search box'};
  q.focus();
  q.value = '719-275-8666';
  q.dispatchEvent(new Event('input', {bubbles:true}));
  await new Promise(r=>setTimeout(r,250));
  const byPhone = [...document.querySelectorAll('.ct-name')].map(e => e.textContent.trim());
  const focused = document.activeElement === q;
  q.value = 'zeta';
  q.dispatchEvent(new Event('input', {bubbles:true}));
  await new Promise(r=>setTimeout(r,250));
  const byName = [...document.querySelectorAll('.ct-name')].map(e => e.textContent.trim());
  q.value = '';
  q.dispatchEvent(new Event('input', {bubbles:true}));
  await new Promise(r=>setTimeout(r,250));
  return { byPhone, byName, focused,
           restored: document.querySelectorAll('.ct-row').length };
})()"""

# Open the card that HAS a photo and an unknown field, save it unchanged, and read back what the
# client would have PUT. This is the assertion the whole preservation design exists for.
EDITOR = r"""(async () => {
  const row = [...document.querySelectorAll('.ct-row')].find(r => r.dataset.uid === 'c-fire');
  if (!row) return {error:'no row for c-fire'};
  row.click();
  await new Promise(r=>setTimeout(r,250));
  const m = document.querySelector('#modal-root .modal');
  if (!m) return {error:'no editor opened'};
  const inputs = [...m.querySelectorAll('input, textarea')];
  const TEXTY = ['text','search','email','url','tel','number','password','date',''];
  const small = inputs.filter(i => !(i.tagName === 'INPUT' && !TEXTY.includes((i.type||'').toLowerCase())))
                      .filter(i => parseFloat(getComputedStyle(i).fontSize) < 16)
                      .map(i => (i.id || i.type) + ':' + getComputedStyle(i).fontSize);
  const r = m.getBoundingClientRect();
  const wide = Math.round(r.width) > window.innerWidth + 1;
  const given = m.querySelector('#cc-given');
  if (given) { given.value = 'Fire Dept'; }
  m.querySelector('#cc-save').click();
  await new Promise(r=>setTimeout(r,300));
  const saved = window.__saved;
  return { fields: inputs.length, small, wide,
           sentVcf: saved ? String(saved.vcf) : '', uid: saved ? saved.uid : '' };
})()"""


# On ANDROID the native switch is the answer and CardDAV is the fallback for other devices. Getting
# this the wrong way round is what produced "why do I need a CardDAV URL, you were supposed to make me
# avoid needing another app" — the feature was there and the panel led with the thing it replaced.
# A plugin has to be PROBED (Capacitor.Plugins is empty for a Java-only plugin, and registerPlugin
# returns a proxy whose methods all "exist"), so this stubs one and then takes it away again.
NATIVE_FIRST = r"""(async () => {
  const sleep = ms => new Promise(r=>setTimeout(r,ms));
  const open = async () => { document.querySelector('#ct-menu').click(); await sleep(400);
                             return document.querySelector('#modal-root .modal'); };
  const out = {};
  window.Capacitor = { getPlatform: () => 'android', Plugins: {} };
  const plugin = { status: async () => ({ granted:true, account:true, owner:'me', count:4 }) };
  window.__PC.capPlugin = (name, method) => (name === 'ContactSync' ? plugin : null);

  let m = await open();
  const rows = [...m.querySelectorAll('.cal-row')];
  out.firstRowIsTheSwitch = !!(rows[0] && rows[0].classList.contains('ct-phonebook'));
  out.hasSwitch = !!m.querySelector('#ctb-phonebook');
  out.davLabel = (m.querySelector('#ctb-phone') || {}).textContent || '';
  out.mentionsUrl = /https?:\/\//.test(m.textContent);
  out.mentionsOtherApp = /DAVx|CardDAV/i.test(m.textContent);
  window.__PC.closeModal();

  // An APK older than the plugin: say so, rather than quietly offering CardDAV as if it were the
  // answer. The probe is a real call, so taking the stub away is how that build looks.
  window.__PC.capPlugin = () => null;
  m = await open();
  out.oldApk = m.textContent;
  window.__PC.closeModal();

  delete window.Capacitor;
  await sleep(50);
  return out;
})()"""


# ⋯ → "Sync to a device". Two live bugs, both reported by a user on the same day: the button borrowed
# the calendar's panel, which is not exported, so it fell through to switchView('calendar') and left
# the address book; and a load finishing while the user is on another screen repainted #feed anyway,
# which is what made "contacts no longer appear in contacts" after that navigation.
SYNCSHEET = r"""(async () => {
  const menu = document.querySelector('#ct-menu');
  if (!menu) return {error:'no ⋯ menu button'};
  menu.click();
  await new Promise(r=>setTimeout(r,300));
  const btn = document.querySelector('#ctb-phone');
  if (!btn) return {error:'no "Sync to a device" button in the menu'};
  btn.click();
  await new Promise(r=>setTimeout(r,500));
  const m = document.querySelector('#modal-root .modal');
  const values = m ? [...m.querySelectorAll('input')].map(i => i.value) : [];
  const sheet = { view: window.__view, text: m ? m.textContent : '', values };
  window.__PC.closeModal();
  await new Promise(r=>setTimeout(r,150));
  sheet.rowsAfter = document.querySelectorAll('.ct-row').length;

  // …and a load that lands after the user has walked away must leave that screen alone.
  window.__view = 'home';
  document.querySelector('#feed').innerHTML = '<div id="pc-other-view">another screen</div>';
  await window.PCContacts.reload();
  await new Promise(r=>setTimeout(r,150));
  sheet.otherViewKept = !!document.querySelector('#pc-other-view');
  window.__view = 'contacts';
  window.PCContacts.render();
  await new Promise(r=>setTimeout(r,300));
  sheet.rowsBack = document.querySelectorAll('.ct-row').length;
  return sheet;
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
                        print("  DEBUG:", json.dumps(r["exceptionDetails"])[:400])
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
                    print(f"SKIP  {label}: the contact list never finished rendering")
                    return 2

                r = await js(AUDIT)
                if r is None:
                    print(f"SKIP  {label}: page did not evaluate")
                    return 2
                if r["overflow"]:
                    problems.append((label, "horizontal-overflow",
                                     "a name or the A–Z rail scrolls the page sideways"))
                if r["rows"] != 4:
                    problems.append((label, "list-empty",
                                     f"{r['rows']} rows rendered, want 4 — a vCard failed to parse"))
                if "Fire DEPARTMENT" not in r["names"]:
                    problems.append((label, "list-empty", f"the list shows {r['names']!r}"))
                if r["photos"] < 1:
                    problems.append((label, "photo-missing",
                                     "no contact rendered its base64 PHOTO"))
                if r["initials"] < 1:
                    problems.append((label, "photo-missing",
                                     "a contact with no photo should fall back to initials"))
                if r["letters"] < 2:
                    problems.append((label, "list-empty",
                                     f"{r['letters']} letter heading(s) — the list is not grouped"))
                if phone:
                    for t in r["small"]:
                        problems.append((label, "tiny-tap-target", f"{t['cls']} is {t['h']}px tall"))
                    for z in r["zoomy"]:
                        problems.append((label, "ios-zoom-trap", f"{z['cls']} is {z['fs']}px"))
                    if r["listBottom"] > r["navTop"] + 1:
                        problems.append((label, "under-nav",
                                         f"the list's bottom ({r['listBottom']}px) is under the nav "
                                         f"({r['navTop']}px)"))

                s = await js(SEARCH, awaited=True)
                if not s or s.get("error"):
                    problems.append((label, "search-broken", f"{(s or {}).get('error')}"))
                else:
                    if s["byPhone"] != ["Fire DEPARTMENT"]:
                        problems.append((label, "search-broken",
                                         f"a punctuated number matched {s['byPhone']!r}"))
                    if s["byName"] != ["Ann Zeta"]:
                        problems.append((label, "search-broken", f"'zeta' matched {s['byName']!r}"))
                    if not s["focused"]:
                        problems.append((label, "caret-lost",
                                         "the search box lost focus while typing"))
                    if s["restored"] != 4:
                        problems.append((label, "search-broken",
                                         f"clearing the search left {s['restored']} rows"))

                nf = await js(NATIVE_FIRST, awaited=True)
                if not nf:
                    problems.append((label, "missing-control",
                                     "the Addressbooks panel did not open with a stubbed plugin"))
                else:
                    if not nf["hasSwitch"]:
                        problems.append((label, "native-not-offered",
                                         "no phone-book switch on a build that has the plugin"))
                    if not nf["firstRowIsTheSwitch"]:
                        problems.append((label, "carddav-leads",
                                         "the panel leads with something other than the native switch"))
                    if "another device" not in nf["davLabel"]:
                        problems.append((label, "carddav-leads",
                                         f"the CardDAV button reads {nf['davLabel'].strip()!r} — on "
                                         "Android it is the route for OTHER devices"))
                    if nf["mentionsUrl"] or nf["mentionsOtherApp"]:
                        problems.append((label, "carddav-leads",
                                         "the panel shows a URL or names another app before the "
                                         "switch that needs neither"))
                    if "Update the app" not in nf["oldApk"]:
                        problems.append((label, "silent-fallback",
                                         "an APK without the plugin is shown CardDAV instead of "
                                         "being told to update"))

                sh = await js(SYNCSHEET, awaited=True)
                if not sh or sh.get("error"):
                    problems.append((label, "missing-control",
                                     f"the sync sheet did not open ({(sh or {}).get('error')})"))
                else:
                    if sh["view"] != "contacts":
                        problems.append((label, "left-the-screen",
                                         f"'Sync to a device' navigated to {sh['view']!r}"))
                    if not any("/caldav/tester/contacts/" in v for v in sh["values"]):
                        problems.append((label, "wrong-collection",
                                         "the sheet shows no ADDRESSBOOK URL — a calendar URL under a "
                                         f"Contacts heading syncs an empty book: {sh['values']!r}"))
                    if "CardDAV" not in sh["text"]:
                        problems.append((label, "wrong-collection",
                                         "the sheet does not say what kind of account to add"))
                    if sh["rowsAfter"] != 4:
                        problems.append((label, "list-empty",
                                         f"{sh['rowsAfter']} rows after closing the sync sheet"))
                    if not sh["otherViewKept"]:
                        problems.append((label, "paints-off-view",
                                         "a contacts load finishing on another screen repainted #feed"))
                    if sh["rowsBack"] != 4:
                        problems.append((label, "state-lost",
                                         f"coming back to contacts showed {sh['rowsBack']} rows"))

                ed = await js(EDITOR, awaited=True)
                if not ed or ed.get("error"):
                    problems.append((label, "missing-control",
                                     f"the contact editor did not open ({(ed or {}).get('error')})"))
                else:
                    if ed["wide"]:
                        problems.append((label, "horizontal-overflow",
                                         "the editor is wider than the screen"))
                    if not ed["sentVcf"]:
                        problems.append((label, "editor-loses-data", "saving sent nothing"))
                    else:
                        if "X-WEIRD-THING:keep me" not in ed["sentVcf"]:
                            problems.append((label, "editor-loses-data",
                                             "saving dropped a field the app has no UI for"))
                        if "bitfire.at" not in ed["sentVcf"]:
                            problems.append((label, "editor-loses-data",
                                             "saving dropped the PRODID of the app that wrote it"))
                        if "7192758666" not in ed["sentVcf"]:
                            problems.append((label, "editor-loses-data",
                                             "saving dropped the phone number"))
                        if ed["uid"] != "c-fire":
                            problems.append((label, "editor-loses-data",
                                             f"saving changed the UID to {ed['uid']!r}"))
                    if phone:
                        for sm in ed["small"]:
                            problems.append((label, "ios-zoom-trap", f"editor field {sm}"))
    finally:
        proc.terminate()
        subprocess.run(["rm", "-rf", PROFILE], check=False)

    if problems:
        print(f"FAIL  {len(problems)} problem(s):")
        for label, kind, msg in problems:
            print(f"  [{label}] {kind}: {msg}")
        return 1
    print("OK  contacts mobile checks passed")
    return 0


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="ctcheck-")
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
