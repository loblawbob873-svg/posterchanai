#!/usr/bin/env python3
"""Mobile + behaviour check for NOTES.

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
  ios-zoom-trap        a text input under 16px: iOS Safari zooms the page on focus and never
                       zooms back out. Applies to the title, the body, search and the tag field.
  editor-under-nav     the editor's bottom is behind the fixed .mobilenav (~62px + safe area), i.e.
                       someone wrote 100vh instead of 100dvh, or forgot to reserve the nav.
  notes-cross-saved    Switching notes with a save still debounced wrote one note's fields onto
                       another. `.nt-editor` is ONE element whose innerHTML is replaced per note, so
                       a commit that looks its inputs up when it fires reads whichever note is on
                       screen then. The result is half of each note and looks entirely plausible.
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
WIDTHS = [(390, 844), (360, 780)]
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
  mdToHtml: s => '<p>'+enc(s)+'</p>',
  uploadEncFile: async () => 'sha'+(++_seq),
  encFileUrl: async () => 'data:text/plain,x',
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
  window.__events = [
    mk('pcai:notefolder:f1', {v:1, id:'f1', name:'Work', created:1, updated:1}),
    mk('pcai:note:n1', {v:1, id:'n1', title:'Quarterly plan', body:'line one\nline two', folder:'f1', tags:['work'], created:1, updated:1700000000, res:[]}),
    mk('pcai:note:n2', {v:1, id:'n2', title:'Groceries', body:'milk', folder:'', tags:[], created:1, updated:1699000000, res:[]}),
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
  for(const el of document.querySelectorAll('.nt-item, .nt-folder, .nt-side-head .btn, .nt-res-item')){
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
  return out;
})()"""

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

# The offline write. Types into the open editor with publishing failing, waits out the 700ms
# debounce, and reports whether the text survived anywhere it could be recovered from.
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
            for w, h in WIDTHS:
                label = f"{w}px"
                await call("Emulation.setDeviceMetricsOverride",
                           {"width": w, "height": h, "deviceScaleFactor": 2, "mobile": True})
                await call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
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
                for s in r["small"]:
                    problems.append((label, "tiny-tap-target",
                                     f"{s['text'] or s['sel']} is {s['h']}px tall"))
                for z in r["zoomy"]:
                    problems.append((label, "ios-zoom-trap",
                                     f"{z['cls']} is {z['fs']}px — iOS zooms the page on focus"))
                if r["wrapBottom"] > r["navTop"] + 1:
                    problems.append((label, "editor-under-nav",
                                     f"the pane's bottom ({round(r['wrapBottom'])}px) is under the nav "
                                     f"({round(r['navTop'])}px) — 100vh instead of 100dvh?"))

                # Open a note: on a phone that must REPLACE the list, not sit beside it.
                if not await js(OPEN_NOTE):
                    problems.append((label, "missing-control", "could not open a note"))
                    continue
                await asyncio.sleep(0.4)
                r2 = await js(AUDIT)
                if r2["listVisible"] and r2["editorVisible"]:
                    problems.append((label, "both-panes-visible",
                                     "the list and the editor are both on screen at phone width"))
                if not r2["editorVisible"]:
                    problems.append((label, "missing-control", "opening a note showed no editor"))
                if not r2["back"]:
                    problems.append((label, "no-way-back",
                                     "the editor is open with no back control"))
                if r2["overflow"]:
                    problems.append((label, "horizontal-overflow", "the open editor scrolls sideways"))
                if r2["bodyBottom"] > r2["navTop"] + 1:
                    problems.append((label, "editor-under-nav",
                                     "the text area runs under the bottom nav"))
                for z in r2["zoomy"]:
                    problems.append((label, "ios-zoom-trap",
                                     f"{z['cls']} is {z['fs']}px — iOS zooms the page on focus"))

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
