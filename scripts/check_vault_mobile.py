#!/usr/bin/env python3
"""Layout + behaviour check for PASSWORDS (vault.js), at phone AND desktop widths.

Run BEFORE deploying a vault change:

    venv-unified/bin/python scripts/check_vault_mobile.py

check_client_mobile.py only ever loads the timeline and check_notes_mobile.py only opens Notes, so a
password screen that is unusable on a phone would ship having "passed the mobile checks". This drives
the real vault.js against a stubbed `window.__PC` (no relay, no login, no network) with a seeded
vault, and audits what a phone actually gets.

Assertions, each a way THIS screen breaks:

  horizontal-overflow  the panes push the page sideways.
  folder-pane-hogs-screen
                       the folder list is on screen at rest on a phone, or the item list is left
                       under 80% of the pane — the mistake Notes shipped and had to undo.
  folders-unreachable  …and the drawer that replaces it doesn't open or won't close.
  both-panes-visible   list AND entry on screen at once at phone width, or an entry open with no way
                       back — a dead end, since there is no second pane to click.
  tiny-tap-target      a row or button under 32px. The copy/reveal/generate buttons sit in a row and
                       are the controls people actually press.
  ios-zoom-trap        a text input under 16px: iOS Safari zooms on focus and never zooms back.
  editor-under-nav     the entry runs under the fixed .mobilenav — 100vh instead of 100dvh.
  password-on-screen   THE one that is specific to this screen: the password field must not render
                       its value in clear on open. A manager that shows every password to whoever
                       glances at the phone is not a manager.
  totp-dead            a stored one-time code secret produces no code, or no countdown. The code is
                       the reason 2FA is in here at all.
  generator-broken     the generator produces nothing, or ignores the character classes.
  pairing-broken       Pair a device does not reach a pairing code at all.
  copy-code-unusable   …or reaches one whose Copy button is off the side, under 34px, not the
                       primary action, or a sliver on a phone. That code has to leave this window;
                       Copy is the step, not a footnote to it.
  damaged-entry-not-repaired
                       an entry stored by an older build as one comma-joined URI (host
                       `blackhillsenergy.com,https`, matching nothing) was not fixed on load. The
                       damage is unambiguous, so the repair is automatic — nobody should have to
                       re-import to recover from a parsing bug.
  import-merged-entries
                       a Bitwarden import merged two DIFFERENT credentials into one, destroying a
                       password — two logins for the same service in different regions, or a secure
                       note colliding with a login of the same name. Re-importing must also update
                       in place rather than duplicating the vault.
  vault-key-reminted   THE data-loss one: when the relay is unreachable, the vault must NOT mint a
                       new key. Doing so replaces the only key that can read the existing items —
                       the same failure the encrypted drive's master key learned once already.

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
WIDTHS = [(390, 844, True), (360, 780, True), (900, 800, False), (1280, 860, False)]
PORT = int(os.environ.get("PC_CHECK_PORT") or 9477)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-vault-mobile-check"

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css">
</head><body>
<div id="feed"></div>
<nav class="mobilenav glass"><button class="nav-item"><b>Home</b></button></nav>
<div id="modal-root"></div><div id="toast-root"></div>
<script src="/static/js/client/sprite.js"></script>
<script>
// Stub host. The CRYPTO IS REAL here — vaultcore.js seals and opens with WebCrypto exactly as it
// does in the app — because the thing most worth checking is that an item written by this code can
// be read back by it. Only the relay and the signer are fakes.
const $  = (s,r)=> (r||document).querySelector(s);
const $$ = (s,r)=> Array.from((r||document).querySelectorAll(s));
const enc = s => String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
window.__events = [];
window.__online = true;
window.__relayThrows = false;     // "the relay is unreachable", which is NOT "the vault is empty"
let _seq = 0;
window.Store = {
  _evs: [],
  query(){ return this._evs.slice(); },
  saveEvent(ev){ this._evs = this._evs.filter(e => _d(e) !== _d(ev)); this._evs.push(ev); },
  removeEvent(id){ this._evs = this._evs.filter(e => e.id !== id); },
};
function _d(ev){ return ((ev.tags||[]).find(t=>t[0]==='d')||[])[1]||''; }
window.Relay = {
  query: async () => { if(window.__relayThrows) throw new Error('offline'); return window.__events.slice(); },
  publish: async (ev) => { if(!window.__online) return {ok:false};
                           window.__events = window.__events.filter(e=>_d(e)!==_d(ev));
                           window.__events.push(ev); return {ok:true}; },
};
window.Session = { load: () => ({mode:'local', sk:'a'.repeat(64)}), save(){}, clear(){} };
window.__PC = {
  $, $$, enc,
  isView: v => v === 'vault',
  toast: m => { window.__toasts = (window.__toasts||[]).concat([m]); },
  uiConfirm: async () => true,
  uiPrompt: async () => 'x',
  modal: (html, onMount) => { const bg=document.createElement('div'); bg.className='modal-bg';
    bg.innerHTML = '<div class="modal glass neon-border">'+html+'</div>';
    $('#modal-root').appendChild(bg); if(onMount) onMount(bg.querySelector('.modal')); },
  closeModal: () => { const m=$('#modal-root .modal-bg'); if(m) m.remove(); },
  publish: async (kind, content, tags) => {
    const ev = { id:'ev'+(++_seq), pubkey:'me', kind, content, tags,
                 created_at: Math.floor(Date.now()/1000)+_seq, sig:'x' };
    window.Store.saveEvent(ev);
    const r = await window.Relay.publish(ev);
    if(!r.ok) window.Store.removeEvent(ev.id);
    return { ev, ...r };
  },
  // The vault key is NIP-44-wrapped to self; identity here, so the SHAPE stays honest (a JSON
  // envelope that has to survive a round trip) without testing the browser's ECDH.
  nip44enc: async (pk, s) => s,
  nip44dec: async (pk, s) => s,
  get ME(){ return {pubkey:'me', mode:'local'}; },
  get VIEW(){ return 'vault'; },
  CFG: { relay_url: 'wss://example.invalid' },
};
</script>
<script src="/static/js/client/vaultcore.js"></script>
<script src="/static/js/client/vault.js"></script>
<script>
(async function(){
  for(let i=0;i<80 && !window.PCVault;i++) await new Promise(r=>setTimeout(r,50));
  if(location.search.includes('norelay')){
    // Nothing cached, nothing published, nothing reachable. Opening the vault here must FAIL and
    // say so — minting a key would replace the only one that can read the real items.
    try{ localStorage.clear(); }catch(_){ }
    window.__relayThrows = true;
    window.__events = [];
    await window.PCVault.render();
    window.__ready = true;
    return;
  }
  // A seeded vault, written through the REAL save path so the ciphertext is real.
  // CLEAR FIRST: every width reuses one browser profile, so the previous width's *unwrapped* key
  // cache (`pcaiVaultKey:raw:*`, which is what stops a phone ever asking for anything) would still
  // be there and would open none of the freshly-sealed items. Each width has to be a fresh device.
  try{ localStorage.clear(); }catch(_){ }
  const V = window.PCVaultCore;
  const key = V.newVaultKey();
  localStorage.setItem('pcaiVaultKey:me', JSON.stringify({k: V.toB64(key), v:1}));
  const mk = async (id, obj) => ({ id:'seed'+id, pubkey:'me', kind:30078, created_at:1700000000,
      tags:[['d','pcai:pw:'+id],['l','pcai-pw']], content: await V.seal(key, obj), sig:'x' });
  window.__events = [
    await mk('a', {v:1, id:'a', kind:'login', title:'GitHub', username:'me@example.com',
                   password:'hunter2-hunter2', totp:'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ',
                   uris:['https://github.com'], notes:'', tags:[], folder:'Work',
                   created:1, updated:1700000000}),
    // Damaged exactly as the pre-fix importer stored it: several URLs in one cell, so the parsed
    // host is `blackhillsenergy.com,https` and the entry matches nothing on its own site. The repair
    // must fix this on load, with no import and no user action.
    await mk('dmg', {v:1, id:'dmg', kind:'login', title:'Black Hills', username:'me',
                   password:'pw', totp:'', notes:'', tags:[], folder:'', created:1, updated:1,
                   uris:['https://www.blackhillsenergy.com,https://blackhillsenergy.com/my-account/login']}),
    await mk('b', {v:1, id:'b', kind:'login', title:'Bank', username:'acct',
                   password:'short', totp:'', uris:['https://hsbc.co.uk'], notes:'', tags:[],
                   folder:'', created:1, updated:1700000000}),
  ];
  await window.PCVault.render();
  for(let i=0;i<80 && !document.querySelector('.pv-item');i++) await new Promise(r=>setTimeout(r,50));
  window.__ready = true;
})();
</script>
</body></html>"""

AUDIT = r"""(() => {
  const vw = window.innerWidth;
  const box = el => { const r = el.getBoundingClientRect();
                      return {x:r.x, y:r.y, w:r.width, h:r.height, bottom:r.bottom, right:r.right}; };
  const vis = el => !!(el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
  const onScreen = el => { if(!vis(el)) return false; const b = box(el);
                           return b.right > 0 && b.x < vw && b.bottom > 0 && b.y < window.innerHeight; };
  const out = { vw, overflow: document.documentElement.scrollWidth > vw + 1 };
  out.wrap = !!document.querySelector('.pv-wrap');
  out.items = document.querySelectorAll('.pv-item').length;
  out.listVisible = vis(document.querySelector('.pv-list'));
  out.editorVisible = vis(document.querySelector('.pv-editor'));
  out.back = vis(document.querySelector('.pv-back'));
  out.foldersOnScreen = onScreen(document.querySelector('.pv-folder[data-f]'));
  out.folderBtn = vis(document.querySelector('.pv-fbtn'));
  out.searchVisible = vis(document.querySelector('.pv-search'));
  const wrapEl = document.querySelector('.pv-wrap');
  out.wrapH = wrapEl ? Math.round(box(wrapEl).h) : 0;
  const listEl = document.querySelector('.pv-list');
  out.listH = (listEl && vis(listEl)) ? Math.round(box(listEl).h) : 0;
  out.wrapBottom = wrapEl ? box(wrapEl).bottom : 0;
  const nav = document.querySelector('.mobilenav');
  out.navTop = (nav && vis(nav)) ? box(nav).y : window.innerHeight;
  const ed = document.querySelector('.pv-ed-body');
  out.edBottom = (ed && vis(ed)) ? box(ed).bottom : 0;
  const small = [];
  for(const el of document.querySelectorAll('.pv-item, .pv-folder, .pv-link, .pv-row .mini, .pv-ed-head .btn, '
                            + '.pv-applist button')){
    if(!vis(el)) continue;
    const b = box(el);
    if(b.h < 32) small.push({sel: el.className, h: Math.round(b.h), text:(el.textContent||'').trim().slice(0,20)});
  }
  out.small = small;
  const zoomy = [];
  for(const el of document.querySelectorAll('.pv-wrap input, .pv-wrap textarea')){
    if(!vis(el)) continue;
    const fs = parseFloat(getComputedStyle(el).fontSize) || 0;
    if(fs < 16) zoomy.push({cls: el.className, fs});
  }
  out.zoomy = zoomy;
  // The password field's TYPE. `text` means the value is on screen for anyone looking at the phone.
  const pw = document.querySelector('.pv-pass');
  out.pwType = pw ? pw.type : '';
  out.pwHasValue = !!(pw && pw.value);
  out.clipped = [];
  for(const el of document.querySelectorAll('.pv-folder span, .pv-item b, .pv-list-head b, .pv-link')){
    if(!vis(el)) continue;
    const t = (el.textContent||'').trim();
    if(t && el.scrollWidth > el.clientWidth + 2)
      out.clipped.push({ text:t.slice(0,20), shown:Math.round(el.clientWidth), needs:Math.round(el.scrollWidth) });
  }
  /* A `white-space:nowrap` label does NOT report scrollWidth > clientWidth when it overruns — the
     text simply spills out of the box, so the check above saw nothing while a third button in the
     sidebar footer ran off the edge in front of the user. Measure what the content actually NEEDS
     (the real text width via a Range, plus the icon and the padding) against the box it was given. */
  for(const el of document.querySelectorAll('.pv-link, .btn')){
    if(!vis(el)) continue;
    const node = [...el.childNodes].find(n => n.nodeType === 3 && n.textContent.trim());
    if(!node) continue;
    const r = document.createRange(); r.selectNodeContents(node);
    const textW = r.getBoundingClientRect().width;
    const cs = getComputedStyle(el);
    let need = textW + parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight) +
               parseFloat(cs.borderLeftWidth) + parseFloat(cs.borderRightWidth);
    for(const kid of el.children){
      if(!vis(kid)) continue;
      need += kid.getBoundingClientRect().width + (parseFloat(cs.columnGap) || parseFloat(cs.gap) || 0);
    }
    const have = el.getBoundingClientRect().width;
    if(need > have + 2)
      out.clipped.push({ text:node.textContent.trim().slice(0,20), shown:Math.round(have),
                         needs:Math.round(need) });
  }
  return out;
})()"""

OPEN_ITEM = r"""(() => { const it = document.querySelector('.pv-item'); if(!it) return false;
                         it.click(); return true; })()"""

OPEN_DRAWER = r"""(() => { const b = document.querySelector('.pv-fbtn');
                           if(!b || !b.getClientRects().length) return false; b.click(); return true; })()"""
CLOSE_DRAWER = r"""(() => { const s = document.querySelector('.pv-scrim');
                            if(s) s.click(); return true; })()"""

# The one-time code has to actually appear, and count down. A stored secret with no code on screen is
# a 2FA feature that does nothing.
TOTP = r"""(async () => {
  const it = Array.from(document.querySelectorAll('.pv-item')).find(b => /GitHub/.test(b.textContent));
  if(!it) return {error:'seeded entry missing'};
  it.click();
  await new Promise(r => setTimeout(r, 400));
  const box = document.querySelector('.pv-code');
  const code = box && box.querySelector('.pv-otp');
  const left = box && box.querySelector('.pv-otp-left');
  return { code: code ? code.textContent.replace(/\s/g,'') : '',
           left: left ? left.textContent : '',
           digits: code ? /^\d{6}$/.test(code.textContent.replace(/\s/g,'')) : false };
})()"""

GENERATOR = r"""(async () => {
  const V = window.PCVaultCore;
  const out = [];
  for(let i=0;i<40;i++){
    const p = V.generate({length:16, lower:true, upper:true, digits:true, symbols:true});
    if(p.length !== 16 || !/[a-z]/.test(p) || !/[A-Z]/.test(p) || !/[0-9]/.test(p) || !/[^a-zA-Z0-9]/.test(p))
      out.push(p);
  }
  return { bad: out.length, sample: V.generate({length:24}) };
})()"""

# The automatic repair. An entry damaged by the old comma-joined-URI parsing has a host containing a
# comma, which cannot arise any other way — so it is fixed on load, without an import and without
# asking. Fourteen of 117 entries in a real vault were in this state, including most of the banks.
REPAIR = r"""(async () => {
  const snap = window.PCVault.snapshot().items;
  const it = snap.find(i => i.id === 'dmg');
  if(!it) return { error: 'the damaged entry is not in the library' };
  const V = window.PCVaultCore;
  return {
    uris: it.uris,
    mangled: it.uris.some(u => (V.hostOf(u) || '').includes(',')),
    matchesItsSite: V.matchLevel(it, 'https://www.blackhillsenergy.com/my-account/login'),
    // …and the repair must have been PUBLISHED, not just held in memory: another device has to see it.
    published: window.__events.some(e => {
      const d = ((e.tags||[]).find(t => t[0] === 'd') || [])[1] || '';
      return d === 'pcai:pw:dmg' && (e.created_at || 0) > 1700000000;
    }),
  };
})()"""

# A real import, through the real code. The dedupe key decides which incoming record UPDATES an
# existing entry and which creates one, and a key that is too loose destroys a password without
# saying anything. Measured while the key was title+username: two Amazon logins (.com and .co.uk,
# same name, same address, different passwords) collapsed into one, and a secure note called "Wifi"
# was overwritten by a login called "Wifi". The progress bar said "4 imported".
IMPORT_DEDUPE = r"""(async () => { try {
  const csv = [
    'folder,favorite,type,name,notes,fields,reprompt,archivedDate,login_uri,login_username,login_password,login_totp',
    ',,login,Amazon,,,,,https://amazon.com,me@x.com,pwUS,',
    ',,login,Amazon,,,,,https://amazon.co.uk,me@x.com,pwUK,',
    ',,note,Wifi,"ssid: home",,,,,,,',
    ',,login,Wifi,,,,,https://router.lan,,routerpw,',
  ].join('\n');
  const file = new File([csv], 'bw.csv', { type:'text/csv' });

  const runImport = async () => {
    document.querySelector('.pv-import').click();
    await new Promise(r => setTimeout(r, 200));
    const input = document.querySelector('#pi-file');
    const dt = new DataTransfer(); dt.items.add(file); input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles:true }));
    await new Promise(r => setTimeout(r, 1800));
    const close = document.querySelector('#pi-close'); if(close) close.click();
    await new Promise(r => setTimeout(r, 200));
  };

  /* THE MIGRATION. An entry imported before the multi-URI split was fixed holds one mangled URI
     whose host is `blackhillsenergy.com,https` — it matches nothing, which is how this was reported
     ("autofill not working on blackhillsenergy.com"). The fix only changes NEW imports, so the
     answer is to run the import again — and that must UPDATE the broken entry, not add a second
     copy beside it. Seeded here exactly as the old importer would have left it. */
  const broken = { v:1, id:'brk', kind:'login', title:'Amazon', username:'me@x.com',
                   password:'old', totp:'', notes:'', tags:[], folder:'', created:1, updated:1,
                   uris:['https://amazon.com,https://amazon.co.uk'] };
  await window.PCVault.__seed(broken);

  await runImport();
  const migrated = window.PCVault.snapshot().items.filter(i => i.id === 'brk')[0];
  const after1 = window.PCVault.snapshot().items
    .filter(i => /Amazon|Wifi/.test(i.title || ''))
    .map(i => [i.kind, i.title, i.username, i.password, (i.notes||'').slice(0,4)].join('/')).sort();

  // …and doing it AGAIN must update in place, not double everything.
  await runImport();
  const after2 = window.PCVault.snapshot().items
    .filter(i => /Amazon|Wifi/.test(i.title || '')).length;

  return { rows: after1, count: after1.length, afterSecond: after2,
           migratedUris: migrated ? migrated.uris : null,
           migratedPw: migrated ? migrated.password : null };
} catch(e) { return { error: String(e && e.message || e) }; } })()"""

# Pair a device. The code it produces is a one-shot value whose whole purpose is to be carried into
# another application, so Copy IS the step — it shipped as a hairline .mini tucked under a four-row
# textarea, which reads as an afterthought and is a poor target on the device you are pairing FROM.
PAIR = r"""(async () => { try {
  const open = document.querySelector('.pv-pair');
  if(!open) return {error:'no Pair a device control'};
  open.click();
  await new Promise(r => setTimeout(r, 250));
  const go = document.querySelector('#pv-pair-go');
  if(!go) return {error:'the pairing screen did not open'};
  go.click();
  await new Promise(r => setTimeout(r, 700));
  const btn = document.querySelector('#pv-code-copy'), ta = document.querySelector('#pv-code');
  if(!btn || !ta) return {error:'no pairing code was produced'};
  const r = btn.getBoundingClientRect();
  const box = (document.querySelector('#modal-root .modal') || document.body).getBoundingClientRect();
  const out = {code: (ta.value || '').length, h: Math.round(r.height), w: Math.round(r.width),
               boxW: Math.round(box.width), right: Math.round(r.right), vw: window.innerWidth,
               primary: /\bbtn-neon\b/.test(btn.className)};
  const close = document.querySelector('#pv-pair-close'); if(close) close.click();
  return out;
} catch(e) { return {error: String(e && e.message || e)}; } })()"""

# THE data-loss one. With the relay unreachable and no local key, opening the vault must FAIL rather
# than mint a second key — a new key makes every existing item permanently unreadable, and the screen
# would look like a working, empty vault.
NO_REMINT = r"""(async () => {
  await new Promise(r => setTimeout(r, 200));
  const minted = !!localStorage.getItem('pcaiVaultKey:me');
  const published = window.__events.some(e => ((e.tags||[]).find(t=>t[0]==='d')||[])[1] === 'pcai:pwkey');
  const said = !!document.querySelector('.pv-locked');
  return { minted, published, said };
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
                           {"width": w, "height": h, "deviceScaleFactor": 2 if phone else 1, "mobile": phone})
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
                    print(f"SKIP  {label}: the page never finished rendering the vault")
                    return 2

                r = await js(AUDIT)
                if r is None or not r["wrap"]:
                    problems.append((label, "missing-control", "the vault pane did not render"))
                    continue
                if r["overflow"]:
                    problems.append((label, "horizontal-overflow", "the page scrolls sideways"))
                if not r["items"]:
                    problems.append((label, "missing-control", "no entries rendered from the seeded vault"))
                if not r["searchVisible"]:
                    problems.append((label, "missing-control", "the search field is not on screen"))
                for c in r["clipped"]:
                    problems.append((label, "text-truncated",
                                     f"{c['text']!r} is cut off — {c['shown']}px shown, {c['needs']}px needed"))
                if phone:
                    for s in r["small"]:
                        problems.append((label, "tiny-tap-target", f"{s['text'] or s['sel']} is {s['h']}px tall"))
                    for z in r["zoomy"]:
                        problems.append((label, "ios-zoom-trap", f"{z['cls']} is {z['fs']}px"))
                    if r["foldersOnScreen"]:
                        problems.append((label, "folder-pane-hogs-screen",
                                         "the folder list is on screen at rest — it must be a drawer"))
                    if r["wrapH"] and r["listH"] < r["wrapH"] * 0.8:
                        problems.append((label, "folder-pane-hogs-screen",
                                         f"the item list gets only {r['listH']}px of {r['wrapH']}px"))
                    if not r["folderBtn"]:
                        problems.append((label, "folders-unreachable", "no control opens the folder list"))
                    elif await js(OPEN_DRAWER):
                        await asyncio.sleep(0.4)
                        rd = await js(AUDIT) or {}
                        if not rd.get("foldersOnScreen"):
                            problems.append((label, "folders-unreachable",
                                             "tapping the folder control did not show the folders"))
                        await js(CLOSE_DRAWER)
                        await asyncio.sleep(0.35)
                        if (await js(AUDIT) or {}).get("foldersOnScreen"):
                            problems.append((label, "folders-unreachable", "the drawer would not close"))
                    if r["wrapBottom"] > r["navTop"] + 1:
                        problems.append((label, "editor-under-nav",
                                         f"the pane's bottom ({round(r['wrapBottom'])}px) is under the nav"))
                else:
                    if not r["foldersOnScreen"]:
                        problems.append((label, "missing-control", "the folder sidebar is off screen at desktop width"))

                # Open an entry.
                if not await js(OPEN_ITEM):
                    problems.append((label, "missing-control", "could not open an entry"))
                    continue
                await asyncio.sleep(0.4)
                r2 = await js(AUDIT) or {}
                if phone and r2.get("listVisible") and r2.get("editorVisible"):
                    problems.append((label, "both-panes-visible",
                                     "the list and the entry are both on screen at phone width"))
                if not r2.get("editorVisible"):
                    problems.append((label, "missing-control", "opening an entry showed no editor"))
                if phone and not r2.get("back"):
                    problems.append((label, "no-way-back", "the entry is open with no back control"))
                if r2.get("overflow"):
                    problems.append((label, "horizontal-overflow", "the open entry scrolls sideways"))
                if phone and r2.get("edBottom", 0) > r2.get("navTop", 0) + 1:
                    problems.append((label, "editor-under-nav", "the entry runs under the bottom nav"))
                if phone:
                    for z in r2.get("zoomy", []):
                        problems.append((label, "ios-zoom-trap", f"{z['cls']} is {z['fs']}px"))
                    for s in r2.get("small", []):
                        problems.append((label, "tiny-tap-target", f"{s['text'] or s['sel']} is {s['h']}px tall"))
                if not r2.get("pwHasValue"):
                    problems.append((label, "missing-control", "the entry opened with no password loaded"))
                elif r2.get("pwType") != "password":
                    problems.append((label, "password-on-screen",
                                     "the password is rendered in clear when an entry opens"))

                t = await js(TOTP, awaited=True)
                if not t or t.get("error"):
                    problems.append((label, "totp-dead", f"could not run the code test ({(t or {}).get('error')})"))
                else:
                    if not t["digits"]:
                        problems.append((label, "totp-dead", f"no six-digit code on screen (got {t['code']!r})"))
                    if not t["left"]:
                        problems.append((label, "totp-dead", "no countdown beside the code"))

                g = await js(GENERATOR, awaited=True)
                if not g or g["bad"] or not g["sample"]:
                    problems.append((label, "generator-broken",
                                     f"{(g or {}).get('bad', '?')} of 40 generated passwords were wrong"))

                pr = await js(PAIR, awaited=True)
                if not pr or pr.get("error"):
                    problems.append((label, "pairing-broken",
                                     f"could not reach the pairing code ({(pr or {}).get('error')})"))
                else:
                    if not pr["code"]:
                        problems.append((label, "pairing-broken", "the pairing code came out empty"))
                    if pr["right"] > pr["vw"] + 1:
                        problems.append((label, "copy-code-unusable",
                                         f"the Copy button runs off the side ({pr['right']} of {pr['vw']})"))
                    # Height only on a phone: at desktop widths the client scales the whole page down
                    # (body{zoom}), so every button measures ~21px there and a floor would be noise.
                    if phone and pr["h"] < 38:
                        problems.append((label, "copy-code-unusable", f"Copy is {pr['h']}px tall"))
                    if not pr["primary"]:
                        problems.append((label, "copy-code-unusable",
                                         "Copy is not the primary action of the step"))
                    if phone and pr["w"] < pr["boxW"] * 0.5:
                        problems.append((label, "copy-code-unusable",
                                         f"Copy is {pr['w']}px of a {pr['boxW']}px sheet — the one button "
                                         "on this screen should be easy to hit"))


                if label == "390px":
                    rep = await js(REPAIR, awaited=True)
                    if not rep or rep.get("error"):
                        problems.append((label, "damaged-entry-not-repaired",
                                         f"could not run the repair test ({(rep or {}).get('error')})"))
                    else:
                        if rep["mangled"]:
                            problems.append((label, "damaged-entry-not-repaired",
                                             f"the entry still has a mangled host: {rep['uris']}"))
                        if rep["matchesItsSite"] != "exact":
                            problems.append((label, "damaged-entry-not-repaired",
                                             "the repaired entry still does not match its own site"))
                        if not rep["published"]:
                            problems.append((label, "damaged-entry-not-repaired",
                                             "the repair was not published — other devices would "
                                             "keep the broken copy"))

                    imp = await js(IMPORT_DEDUPE, awaited=True)
                    if not imp or imp.get("error"):
                        problems.append((label, "import-merged-entries",
                                         f"could not run the import test ({(imp or {}).get('error')})"))
                    else:
                        if imp["count"] != 4:
                            problems.append((label, "import-merged-entries",
                                             f"4 distinct entries imported as {imp['count']}: {imp['rows']}"))
                        # The mangled entry must have been UPDATED in place, not left behind
                        # while a duplicate was created next to it.
                        if imp.get("migratedUris") is None:
                            problems.append((label, "import-merged-entries",
                                             "the pre-fix entry was not updated by the re-import — "
                                             "it would sit there matching nothing, beside a duplicate"))
                        elif any("," in u for u in imp["migratedUris"]):
                            problems.append((label, "import-merged-entries",
                                             f"the re-import left the mangled URI in place, so the "
                                             f"entry still matches nothing: {imp['migratedUris']}"))
                        if imp["afterSecond"] != imp["count"]:
                            problems.append((label, "import-merged-entries",
                                             f"re-importing the same file changed the count "
                                             f"{imp['count']} -> {imp['afterSecond']}"))

                await call("Page.navigate", {"url": url + "?norelay=1"})
                for _ in range(80):
                    await asyncio.sleep(0.25)
                    if await js("window.__ready === true"):
                        break

                nk = await js(NO_REMINT, awaited=True)
                if not nk:
                    problems.append((label, "vault-key-reminted", "could not run the key test"))
                else:
                    if nk["minted"] or nk["published"]:
                        problems.append((label, "vault-key-reminted",
                                         "a new vault key was created while the relay was unreachable — "
                                         "that replaces the only key that can read the existing items"))
                    if not nk["said"]:
                        problems.append((label, "vault-key-reminted",
                                         "the vault failed to open and said nothing about it"))

                print(f"{label}: items={r['items']} overflow={r['overflow']} "
                      f"tiny={len(r['small'])} zoomy={len(r['zoomy'])} "
                      f"pw={r2.get('pwType')} totp={(t or {}).get('digits')}")

        if problems:
            print("\nREGRESSIONS")
            for label, kind, msg in problems:
                print(f"  [{label}] {kind}: {msg}")
            return 1
        print("OK  vault mobile checks passed")
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

    d = tempfile.mkdtemp()
    with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(PAGE)

    class H(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            path = path.split("?")[0]
            if path.startswith("/static/"):
                return os.path.join(ROOT, path.lstrip("/"))
            return os.path.join(d, path.lstrip("/") or "index.html")

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
