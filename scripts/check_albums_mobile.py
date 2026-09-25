#!/usr/bin/env python3
"""Layout + behaviour check for PROFILE → ALBUMS (NIP-51 picture sets), at phone AND desktop widths.

    venv-unified/bin/python scripts/check_albums_mobile.py

Drives the real static/js/client/albums.js and static/css/client.css against a stub relay/host (no
server, no login): a seeded profile with albums of real PNG photos. Every assertion is a way this
screen fails a person holding a phone:

  horizontal-overflow   the page scrolls sideways at 360/390px.
  album-grid            album cards not two across on a phone (one giant card, or four postage stamps),
                        or a cover that is not square.
  photo-grid            photos not three across on a phone, or not square.
  title-overflow        a long album name spilling out of its card instead of ending in an ellipsis.
  tiny-tap-target       a button under 40px tall.
  ios-zoom-trap         a form input under 16px (iOS zooms the page on focus and never zooms back).
  modal-overflow        the New album form wider than the screen.
  lightbox              tapping the 3rd photo does not open the lightbox on the 3rd photo.
  create                New album does not publish a kind-30006 set with the typed name.
  add-device            photos picked from the phone are not uploaded, posted as kind 20 WITH an imeta
                        url, and appended to the album with every photo already in it kept.
  add-files             a photo picked from Files is not added the same way; an ENCRYPTED drive file is
                        (it is unreadable to everybody else).
  incomplete-read       the album is saved on top of a relay read that did not complete -- the
                        replaceable-list wipe.
  remove                removing a photo drops only that photo.
  profile-tabs          the profile's tab row (six tabs since Albums joined) clips a label or pushes the
                        page sideways on a phone. Its markup is lifted from the SHIPPED profile.js.

Exit 0 = clean, 1 = problems (printed), 2 = could not run.
PC_ALBUMS_SHOTS=<dir> also saves phone screenshots of each screen, for a person to LOOK at.
"""
import asyncio
import base64
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("PC_CHECK_PORT") or 9483)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-albums-mobile-check"
ME = "a" * 64


def png(w, h, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css">
</head><body>
<main class="main"><div id="feed" class="feed">__TABS__<div id="prof-list"><div id="prof-albums" class="alb-root"></div></div></div></main>
<div id="modal-root"></div>
<script src="/static/js/client/sprite.js"></script>
<script>
const $  = (s,r)=> (r||document).querySelector(s);
const $$ = (s,r)=> Array.from((r||document).querySelectorAll(s));
const enc = s => String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ME = '__ME__';
let _seq = 0;
const hex = n => (n.toString(16)).padStart(64, '0');
window.__events = [];          // what "the relay" holds
window.__published = [];       // every event the client published, in order
window.__complete = true;      // false → every read comes back INCOMPLETE (a relay that never EOSE'd)
window.__toasts = [];
window.__lightbox = null;
const _d = e => ((e.tags||[]).find(t=>t[0]==='d')||[])[1]||'';
function matches(e, f){
  if(f.ids && !f.ids.includes(e.id)) return false;
  if(f.kinds && !f.kinds.includes(e.kind)) return false;
  if(f.authors && !f.authors.includes(e.pubkey)) return false;
  if(f['#d'] && !f['#d'].includes(_d(e))) return false;
  return true;
}
window.Relay = {
  ready: async () => true,
  query: async (filters) => {
    const got = window.__complete ? window.__events.filter(e => filters.some(f => matches(e, f))) : [];
    Object.defineProperty(got, 'complete', { value: window.__complete, enumerable: false });
    return got;
  },
};
window.Store = { _m: new Map(), get(id){ return this._m.get(id); }, saveEvent(e){ this._m.set(e.id, e); }, removeEvent(id){ this._m.delete(id); } };
function add(ev){
  if(ev.kind === 30006) window.__events = window.__events.filter(e => !(e.kind===30006 && e.pubkey===ev.pubkey && _d(e)===_d(ev)));
  window.__events.push(ev);
}
async function publish(kind, content, tags){
  const ev = { id: hex(++_seq + 1000), pubkey: ME, kind, content, tags, created_at: 1790000000 + _seq };
  window.__published.push(ev);
  if(kind === 5){ const a=(tags.find(t=>t[0]==='a')||[])[1]; window.__events = window.__events.filter(e => `${e.kind}:${e.pubkey}:${_d(e)}` !== a); }
  else add(ev);
  return { ok: true, ev };
}
let _up = 0;
const modalRoot = () => document.getElementById('modal-root');
function modal(html, onMount){
  const bg = document.createElement('div'); bg.className = 'modal-bg';
  bg.innerHTML = `<div class="modal glass neon-border">${html}</div>`;
  modalRoot().appendChild(bg); document.body.classList.add('modal-open');
  if(onMount) onMount(bg.querySelector('.modal'));
}
function closeModal(){ const m = modalRoot().lastElementChild; if(m) m.remove(); document.body.classList.remove('modal-open'); }
window.__pick = null;   // what the stub Files picker "chooses"
window.__deps = ({
  state: { get ME(){ return { pubkey: ME }; } },
  $, $$, enc, modal, closeModal,
  toast: m => window.__toasts.push(m),
  uiConfirm: async () => true,
  publish,
  uploadBlob: async (f) => `${location.origin}/up/${++_up}.png`,
  imetaTagsFor: (u) => [['imeta', 'url ' + u, 'dim 640x480']],
  blossomPicker: (ta, onPick) => { if(window.__pick) onPick(window.__pick); },
  openLightbox: (src, kind, group) => { window.__lightbox = { src, i: group && group.i, n: group && group.items.length }; },
  _firstImage: () => null,
});
// ---- seed: three albums, the first with five photos ----
function pic(n){ const e = { id: hex(n), pubkey: ME, kind: 20, content: '', created_at: 1780000000 + n,
  tags: [['imeta', `url ${location.origin}/img/${(n % 4) + 1}.png`, 'm image/png']] }; add(e); return e.id; }
const ids = [1,2,3,4,5].map(pic);
add({ id: hex(900), pubkey: ME, kind: 30006, content: '', created_at: 1789000000,
      tags: [['d','trip'], ['title','A very long album name that must not spill out of its little card on a phone'], ...ids.map(i=>['e', i])] });
add({ id: hex(901), pubkey: ME, kind: 30006, content: '', created_at: 1789000001,
      tags: [['d','cats'], ['title','Cats'], ['description','Every cat'], ['e', pic(6)]] });
add({ id: hex(902), pubkey: ME, kind: 30006, content: '', created_at: 1789000002, tags: [['d','empty'], ['title','Empty']] });
</script>
<script src="/static/js/client/albums.js"></script>
<script>
window.PCAlbums = PCAlbumsFactory(window.__deps || {});
</script>
</body></html>"""


AUDIT = r"""((phone) => {
  const out = [];
  if (document.documentElement.scrollWidth > innerWidth + 1) out.push(['horizontal-overflow', document.documentElement.scrollWidth + 'px']);
  const vis = el => el.getBoundingClientRect().width > 0;
  // Tap targets are for FINGERS: at desktop width the app's page zoom (body{zoom:~.7}) scales a 42px
  // button to ~30px on purpose, and a mouse does not need 40.
  if (phone) document.querySelectorAll('#prof-albums .btn, #prof-albums .mini, .modal .btn').forEach(b => {
    if (!vis(b)) return; const h = b.getBoundingClientRect().height;
    if (h < 39.5) out.push(['tiny-tap-target', (b.textContent||'').trim().slice(0,30) + ' ' + h.toFixed(0) + 'px']);
  });
  // Every icon actually drawn: a glyph missing from the font (the first build's ＋ and ⋯) shows as an
  // empty box, and a <use> naming a missing sprite symbol draws 0x0.
  document.querySelectorAll('#prof-albums svg.ic, .modal svg.ic').forEach(sv => {
    const r = sv.getBoundingClientRect(); if (r.width < 4 || r.height < 4) out.push(['zero-sized-icon', (sv.querySelector('use')||{}).getAttribute && sv.querySelector('use').getAttribute('href')]);
    const u = sv.querySelector('use'); const id = u && (u.getAttribute('href')||'').slice(1);
    if (id && !document.getElementById(id)) out.push(['missing-icon', id]);
  });
  document.querySelectorAll('#prof-albums button, .modal button').forEach(b => {
    if (/[＋⋯]/.test(b.textContent)) out.push(['font-missing-glyph', b.textContent.trim().slice(0, 30)]);
  });
  return out;
})(PHONE)"""


async def drive(url):
    import websockets
    shutil.rmtree(PROFILE, ignore_errors=True)
    chrome = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        print("SKIP  no Chrome")
        return 2
    proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                             f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    problems = []
    try:
        page = None
        for _ in range(60):
            try:
                page = [t for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list")) if t["type"] == "page"][0]
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
                        return msg.get("result") or {}

            shots = os.environ.get("PC_ALBUMS_SHOTS")

            async def shot(name):
                if shots:
                    os.makedirs(shots, exist_ok=True)
                    r = await call("Page.captureScreenshot", {"format": "png"})
                    with open(os.path.join(shots, name + ".png"), "wb") as fh:
                        fh.write(base64.b64decode(r["data"]))

            async def js(expr):
                r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
                if r.get("exceptionDetails"):
                    raise RuntimeError((r["exceptionDetails"].get("exception") or {}).get("description") or str(r["exceptionDetails"]))
                return (r.get("result") or {}).get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            # The tab row alone at 320px (the narrowest phones in use): six labels there is where a
            # row of equal-width tabs actually runs out of room.
            await call("Emulation.setDeviceMetricsOverride", {"width": 320, "height": 640, "deviceScaleFactor": 2, "mobile": True})
            await call("Page.navigate", {"url": url})
            await asyncio.sleep(1.2)
            narrow = await js("""(() => { const t = [...document.querySelectorAll('.prof-tab')];
              const textW = b => { const r = document.createRange(); r.selectNodeContents(b); return r.getBoundingClientRect().width; };
              const innerW = b => { const cs = getComputedStyle(b); return b.getBoundingClientRect().width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight); };
              // A flex tab never shrinks below its label, so the real failure is not a clipped label: the
              // ROW grows past the screen and the last tabs sit where nobody can reach them (the feed
              // clips them, so the page does not even scroll sideways to say so). Every tab must be on
              // screen, or the row must be a scroller.
              const row = document.querySelector('.prof-tabs'), ox = getComputedStyle(row).overflowX;
              const scrolls = (ox === 'auto' || ox === 'scroll') && row.scrollWidth > row.clientWidth;
              return { clipped: t.filter(b => textW(b) > innerW(b) + 0.5).map(b => b.textContent),
                       unreachable: scrolls ? [] : t.filter(b => b.getBoundingClientRect().right > innerWidth + 1).map(b => b.textContent),
                       overflow: document.documentElement.scrollWidth > innerWidth + 1 }; })()""")
            if narrow["clipped"] or narrow["unreachable"] or narrow["overflow"]:
                problems.append(("320px", "profile-tabs", f"clipped {narrow['clipped']} / off-screen and not scrollable {narrow['unreachable']} / page overflow {narrow['overflow']}"))
            for w, h, phone in [(390, 844, True), (360, 780, True), (1280, 900, False)]:
                label = f"{w}px"
                await call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 2, "mobile": phone})
                await call("Page.navigate", {"url": url})
                await asyncio.sleep(1.5)
                if not await js("typeof PCAlbums === 'object' && !!PCAlbums.mount"):
                    print(f"SKIP  {label}: albums.js did not load")
                    return 2
                await js("PCAlbums.mount(document.getElementById('prof-albums'), ME)")
                await asyncio.sleep(0.6)
                problems += [(label, k, v) for k, v in await js(AUDIT.replace('PHONE', 'true' if phone else 'false'))]
                if w == 390:
                    await shot("1-albums")
                grid = await js("""(() => { const c = [...document.querySelectorAll('.alb-card')].map(e => e.getBoundingClientRect());
                  const cov = [...document.querySelectorAll('.alb-cover')].map(e => e.getBoundingClientRect());
                  const t = document.querySelector('.alb-card .alb-title');
                  return { n: c.length, perRow: c.filter(r => Math.abs(r.top - c[0].top) < 2).length,
                           squareCovers: cov.every(r => Math.abs(r.width - r.height) < 2),
                           titleFits: t.scrollWidth > t.clientWidth ? getComputedStyle(t).textOverflow === 'ellipsis' : true,
                           titleInside: t.getBoundingClientRect().right <= t.closest('.alb-card').getBoundingClientRect().right + 1 }; })()""")
                nb = await js("(() => { const b = document.querySelector('.alb-new'); return b.getBoundingClientRect().width / b.parentElement.getBoundingClientRect().width; })()")
                if nb < 0.95:
                    problems.append((label, "album-bar", f"New album spans {nb:.0%} of its bar"))
                if grid["n"] != 3:
                    problems.append((label, "album-grid", f"{grid['n']} album cards, expected 3"))
                if phone and grid["perRow"] != 2:
                    problems.append((label, "album-grid", f"{grid['perRow']} album cards per row on a phone, expected 2"))
                if not grid["squareCovers"]:
                    problems.append((label, "album-grid", "a cover is not square"))
                if not (grid["titleFits"] and grid["titleInside"]):
                    problems.append((label, "title-overflow", "the long album name spills out of its card"))

                # open the five-photo album
                await js("document.querySelector('.alb-card[data-d=\"trip\"]').click()")
                await asyncio.sleep(0.6)
                problems += [(label, k, v) for k, v in await js(AUDIT.replace('PHONE', 'true' if phone else 'false'))]
                ph = await js("""(() => { const p = [...document.querySelectorAll('.alb-photo')].map(e => e.getBoundingClientRect());
                  return { n: p.length, perRow: p.filter(r => Math.abs(r.top - p[0].top) < 2).length,
                           square: p.every(r => Math.abs(r.width - r.height) < 2), back: !!document.querySelector('.alb-back') }; })()""")
                if ph["n"] != 5:
                    problems.append((label, "photo-grid", f"{ph['n']} photos shown, expected 5"))
                if phone and ph["perRow"] != 3:
                    problems.append((label, "photo-grid", f"{ph['perRow']} photos per row on a phone, expected 3"))
                if not ph["square"]:
                    problems.append((label, "photo-grid", "a photo tile is not square"))
                if not ph["back"]:
                    problems.append((label, "photo-grid", "no way back to the album list"))
                bar = await js("""(() => { const a = document.querySelector('.alb-add'), m = document.querySelector('.alb-more');
                  const row = document.querySelector('.alb-bar-album').getBoundingClientRect(), ar = a.getBoundingClientRect(), mr = m.getBoundingClientRect();
                  return { oneRow: Math.abs(ar.top - mr.top) < 2, addFills: ar.width + mr.width >= row.width - 24, menuW: mr.width }; })()""")
                if not bar["oneRow"]:
                    problems.append((label, "album-bar", "Add photos and the album menu are not one row"))
                if not bar["addFills"]:
                    problems.append((label, "album-bar", "Add photos does not fill its row"))
                if phone and bar["menuW"] < 44:
                    problems.append((label, "album-bar", f"the album menu button is {bar['menuW']:.0f}px wide"))
                if w == 390:
                    await shot("2-album")
                lb = await js("document.querySelectorAll('.alb-photo img')[2].click(), window.__lightbox")
                if not lb or lb.get("i") != 2 or lb.get("n") != 5:
                    problems.append((label, "lightbox", f"tapping the 3rd photo opened {lb}"))

                tabs = await js("""(() => { const row = document.querySelector('.prof-tabs');
                  const t = [...row.querySelectorAll('.prof-tab')];
                  // The TEXT's own width against the button's inner width: a button does not report
                  // overflowing text in scrollWidth, so that measure passed a label wider than its tab.
                  const textW = b => { const r = document.createRange(); r.selectNodeContents(b); return r.getBoundingClientRect().width; };
                  const innerW = b => { const cs = getComputedStyle(b); return b.getBoundingClientRect().width - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight); };
                  return { n: t.length, clipped: t.filter(b => textW(b) > innerW(b) + 0.5).map(b => b.textContent + ' ' + textW(b).toFixed(0) + '>' + innerW(b).toFixed(0)),
                           albums: t.some(b => b.dataset.tab === 'albums'), rowRight: row.getBoundingClientRect().right }; })()""")
                if not tabs["albums"]:
                    problems.append((label, "profile-tabs", "no Albums tab in the profile's tab row"))
                if tabs["clipped"]:
                    problems.append((label, "profile-tabs", f"clipped tab labels: {tabs['clipped']}"))
                if tabs["rowRight"] > w + 1:
                    problems.append((label, "profile-tabs", f"the tab row reaches {tabs['rowRight']:.0f}px"))

                if w != 390:
                    continue
                # ---- behaviour, once, at phone width ----
                # add from the device: two files, appended after the five already there
                before = await js("window.__published.length")
                await js("document.querySelector('.alb-add').click()")
                await asyncio.sleep(0.3)
                problems += [(label, k, v) for k, v in await js(AUDIT.replace('PHONE', 'true'))]
                await shot("4-add-photos")
                sheet = await js("[...document.querySelectorAll('.alb-menu .btn')].map(b => b.getBoundingClientRect().width / b.parentElement.getBoundingClientRect().width)")
                if any(x < 0.95 for x in sheet):
                    problems.append((label, "sheet", f"the Add photos sheet's buttons are not full width: {sheet}"))
                await js("document.querySelector('.alb-add-dev').click()")   # opens the file chooser (headless: nothing)
                await js("""(() => { const i = document.querySelector('.alb-file'); const dt = new DataTransfer();
                  dt.items.add(new File([new Uint8Array([1])], 'a.png', {type:'image/png'}));
                  dt.items.add(new File([new Uint8Array([2])], 'b.png', {type:'image/png'}));
                  i.files = dt.files; i.dispatchEvent(new Event('change')); })()""")
                await asyncio.sleep(1.2)
                pub = await js(f"window.__published.slice({before})")
                pics = [e for e in pub if e["kind"] == 20]
                sets = [e for e in pub if e["kind"] == 30006]
                if len(pics) != 2 or not all(any(t[0] == "imeta" and any(p.startswith("url http") for p in t[1:]) for t in e["tags"]) for e in pics):
                    problems.append((label, "add-device", f"expected 2 kind-20 posts with an imeta url, got {[e['kind'] for e in pub]}"))
                elif not sets:
                    problems.append((label, "add-device", "the album was not updated"))
                else:
                    es = [t[1] for t in sets[-1]["tags"] if t[0] == "e"]
                    if len(es) != 7 or es[5:] != [e["id"] for e in pics]:
                        problems.append((label, "add-device", f"album now lists {len(es)} photos; the 5 old ones must stay, the 2 new appended"))
                    if await js("document.querySelectorAll('.alb-photo').length") != 7:
                        problems.append((label, "add-device", "the grid does not show the added photos"))

                # add from Files: a plain image goes in; an encrypted drive file is refused
                before = await js("window.__published.length")
                await js("window.__pick = {url: location.origin + '/img/2.png', type:'image/png', sha:'ab'.repeat(32), enc:true}; document.querySelector('.alb-add').click(); document.querySelector('.alb-add-files').click()")
                await asyncio.sleep(0.4)
                if await js(f"window.__published.length") != before:
                    problems.append((label, "add-files", "an ENCRYPTED drive file was published as a picture"))
                await js("window.__pick = {url: location.origin + '/img/3.png', type:'image/png', sha:'cd'.repeat(32), enc:false}; document.querySelector('.alb-add').click(); document.querySelector('.alb-add-files').click()")
                await asyncio.sleep(1.0)
                pub = await js(f"window.__published.slice({before})")
                x_ok = any(e["kind"] == 20 and ["x", "cd" * 32] in e["tags"] for e in pub)
                if not x_ok or not any(e["kind"] == 30006 for e in pub):
                    problems.append((label, "add-files", f"a Files pick was not posted (with its hash) and added: {[e['kind'] for e in pub]}"))

                # remove one photo: only that one goes
                cur = await js("window.__events.filter(e=>e.kind===30006&&e.tags.some(t=>t[0]==='d'&&t[1]==='trip'))[0].tags.filter(t=>t[0]==='e').map(t=>t[1])")
                await js("document.querySelectorAll('.alb-photo-menu')[1].click()")
                await asyncio.sleep(0.3)
                await js("document.querySelector('.alb-remove').click()")
                await asyncio.sleep(0.8)
                now = await js("window.__events.filter(e=>e.kind===30006&&e.tags.some(t=>t[0]==='d'&&t[1]==='trip'))[0].tags.filter(t=>t[0]==='e').map(t=>t[1])")
                if now != [cur[0]] + cur[2:]:
                    problems.append((label, "remove", f"removing photo 2 left {len(now)} of {len(cur)}"))

                # an incomplete read must NOT be written over
                before = await js("window.__published.length")
                await js("window.__complete = false")
                await js("""(() => { const i = document.querySelector('.alb-file'); const dt = new DataTransfer();
                  dt.items.add(new File([new Uint8Array([3])], 'c.png', {type:'image/png'}));
                  i.files = dt.files; i.dispatchEvent(new Event('change')); })()""")
                await asyncio.sleep(1.0)
                sets = [e for e in await js(f"window.__published.slice({before})") if e["kind"] == 30006]
                if sets:
                    problems.append((label, "incomplete-read", "the album was saved on top of a relay read that did not complete"))
                await js("window.__complete = true")

                # create an album through the form
                await js("document.querySelector('.alb-back').click()")
                await asyncio.sleep(0.6)
                await js("document.querySelector('.alb-new').click()")
                await asyncio.sleep(0.3)
                problems += [(label, k, v) for k, v in await js(AUDIT.replace('PHONE', 'true' if phone else 'false'))]
                await shot("3-new-album")
                form = await js("""(() => { const m = document.querySelector('.modal'); const r = m.getBoundingClientRect();
                  return { right: r.right, fonts: [...m.querySelectorAll('.input')].map(i => parseFloat(getComputedStyle(i).fontSize)) }; })()""")
                if form["right"] > 390 + 1:
                    problems.append((label, "modal-overflow", f"the form reaches {form['right']:.0f}px"))
                if any(f < 16 for f in form["fonts"]):
                    problems.append((label, "ios-zoom-trap", f"form input font sizes {form['fonts']}"))
                before = await js("window.__published.length")
                await js("document.querySelector('.alb-f-title').value = 'Holiday'; document.querySelector('.alb-form').requestSubmit()")
                await asyncio.sleep(0.8)
                made = [e for e in await js(f"window.__published.slice({before})") if e["kind"] == 30006]
                if not made or ["title", "Holiday"] not in made[0]["tags"]:
                    problems.append((label, "create", "New album did not publish a kind-30006 set titled Holiday"))
                elif not await js("[...document.querySelectorAll('.alb-title')].some(t => t.textContent === 'Holiday')"):
                    problems.append((label, "create", "the new album does not appear in the grid"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(PROFILE, ignore_errors=True)

    for label, kind, detail in problems:
        print(f"FAIL  {label:6} {kind:20} {detail}")
    if problems:
        return 1
    print("OK    albums: grids, tap targets, lightbox, add (device/Files), remove, create, incomplete-read guard")
    return 0


def tabs_markup():
    """The profile tab row exactly as profile.js writes it -- never a copy that can drift."""
    src = open(os.path.join(ROOT, "static/js/client/profile.js"), encoding="utf-8").read()
    start = src.index('<div class="prof-tabs">')
    return src[start:src.index("</div>", start) + len("</div>")]


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="albumscheck-")
    try:
        with open(os.path.join(tmp, "index.html"), "w") as fh:
            fh.write(PAGE.replace("__ME__", ME).replace("__TABS__", tabs_markup()))
        os.makedirs(os.path.join(tmp, "img"))
        os.makedirs(os.path.join(tmp, "up"))
        for i, rgb in enumerate([(200, 60, 60), (60, 160, 90), (60, 90, 200), (220, 180, 40)], 1):
            with open(os.path.join(tmp, "img", f"{i}.png"), "wb") as fh:
                fh.write(png(64, 48, rgb))
        for i in range(1, 20):
            with open(os.path.join(tmp, "up", f"{i}.png"), "wb") as fh:
                fh.write(png(32, 32, (120, 120, 120)))

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
        try:
            return asyncio.run(drive(f"http://127.0.0.1:{srv.server_port}/index.html"))
        finally:
            srv.shutdown()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
