#!/usr/bin/env python3
"""Browser check for the Meme Builder's ✨ MAGIC ERASER, at phone AND desktop size.

    venv-unified/bin/python scripts/check_meme_magic_eraser.py

The Magic Eraser borrows the ✂ Erase-parts dialog with the brush's meaning flipped: the mask starts
EMPTY and brushing ADDS "fill this in" to it, and Apply sends it to /client/meme/magic-erase and
swaps the layer's picture for the answer. Every failure here is quiet, so each is measured:

  missing-control   the Magic Eraser button is not on an image layer's panel.
  brush-broken      a real touch/mouse stroke does not mark where it went (or marks a corner it
                    never touched) — sampled from the scrim canvas, not inferred from a call.
  wrong-polarity    the mask sent to the server is Erase-parts' KEEP mask (opaque everywhere but
                    the stroke) — the server would fill the whole picture EXCEPT the object.
  apply-broken      Apply did not send the layer's own url, did not swap in the answer, left the
                    dialog open, or lost the way back (origSrc for ↺, the builder's undo).
  layout            horizontal overflow, the picture or Remove-it off screen, tap targets < 24px,
                    touch-action that lets the browser steal the drag as a scroll.

Drives the SHIPPED meme.js against a stubbed `window.__PC` (the same harness shape as
check_meme_mobile.py); the server's answer is stubbed at fetch(), because the fill itself is
covered pixel-for-pixel by tests/test_magic_eraser.py.

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
PORT = int(os.environ.get("PC_CHECK_PORT") or 9481)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-meme-magic-check"
SIZES = [("phone", 390, 844, True), ("desktop", 1440, 900, False)]
FILLED = "/static/icon-512.png?filled=1"

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css">
</head><body>
<div id="modal-root"></div>
<div id="feed"></div>
<script>
localStorage.setItem('pc_meme_project', JSON.stringify({
  name:'check', w:720, h:1280, fps:12, bg:'#000000', duration:4,
  layers:[{id:'L1', type:'image', src:'/static/icon-192.png', name:'face',
           start:0, dur:4, trim:0, x:0, y:320, w:720, h:640,
           opacity:1, effect:'none', volume:1, mute:false,
           flipH:false, flipV:false, rotate:0, sound:'', soundVolume:1,
           text:'', size:64, color:'#ffffff', stroke:'#000000', align:''}]
}));
window.__toasts = [];
// Uncaught errors are REPORTED: a handler that throws leaves a button that simply does nothing.
window.__errs = [];
addEventListener('error', e => window.__errs.push(String(e.message)));
window.__PC = {
  isView(v){ return v === 'meme'; },
  toast(m){ window.__toasts.push(String(m)); },
  async uploadBlob(){ return 'https://example.invalid/x.png'; },
  async selfProof(){ return 'proof'; },
  async uiConfirm(){ return false; }, async uiPrompt(){ return null; },
  modal(html, onMount){
    const bg = document.createElement('div'); bg.className = 'modal-bg';
    bg.innerHTML = '<div class="modal glass neon-border">' + html + '</div>';
    document.getElementById('modal-root').appendChild(bg);
    const box = bg.querySelector('.modal'); if (onMount) onMount(box);
  },
  closeModal(){ document.getElementById('modal-root').innerHTML = ''; },
  blossomPicker(){}, openGenStudio(){}, openVoiceStudio(){},
  openEmojiPopover(){ return ''; }, instEmojiUrl(){ return ''; },
  mediaServer:'', eTags(){ return []; }, profOf(){ return {}; },
  get ME(){ return {pubkey:'0'.repeat(64)}; }, get CFG(){ return {}; }, get VIEW(){ return 'meme'; },
};
// Only the magic-erase endpoint is answered here; the body is KEPT, because what the brush is worth
// is decided by the mask that reaches the server.
const _fetch = window.fetch.bind(window);
window.__magicBody = null;
window.fetch = (u, o) => {
  if (!String(u).includes('/client/meme/magic-erase')) return _fetch(u, o);
  try { window.__magicBody = JSON.parse((o || {}).body || 'null'); } catch (_) { }
  return new Promise(res => setTimeout(() => res(new Response(
    JSON.stringify({ok:true, url:'%FILLED%', effect:'magic-erase', is_video:false,
                    method:'lama', model:'ready'}),
    {status:200, headers:{'Content-Type':'application/json'}})), 300));
};
</script>
<script src="/static/js/client/sprite.js"></script>
<script src="/static/js/client/meme.js"></script>
<script>
window.__ready = false;
(function boot(){
  if(!window.PCMeme) return setTimeout(boot, 30);
  window.PCMeme.render();
  setTimeout(()=>{
    const row = document.querySelector('.mb-track[data-id="L1"]');
    if(row) row.click();
    const tab = document.querySelector('.mb-tab[data-tab="layer"]');
    if(tab) tab.click();
    window.__ready = true;
  }, 500);
})();
</script>
</body></html>""".replace("%FILLED%", FILLED)

PROBE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const layer = () => ((JSON.parse(localStorage.getItem('pc_meme_project')||'null')||{layers:[]})
                        .layers.find(l => l.id === 'L1') || {});
  const btn = document.getElementById('mb-magic');
  if (!btn) return {err: 'missing-control', detail: 'no #mb-magic on the image layer panel'};
  btn.scrollIntoView({block: 'center'});
  const bb = btn.getBoundingClientRect();
  // 24px is a THUMB target; a desktop mouse gets the same size as its Erase-parts neighbour.
  const btnOk = bb.width > 0 && bb.height >= (%MINH%) && bb.left >= -1 && bb.right <= innerWidth + 1;
  const btnBox = [Math.round(bb.left), Math.round(bb.top), Math.round(bb.width), Math.round(bb.height), innerWidth];
  const eraseBox = (() => { const e = document.getElementById("mb-erase").getBoundingClientRect(); return [Math.round(e.left), Math.round(e.top), Math.round(e.width), Math.round(e.height)]; })();
  const before = layer().src;
  btn.click();
  const art = document.getElementById('er-src'), ov = document.getElementById('er-ov');
  if (!art || !ov) return {err: 'apply-broken', detail: 'the Magic Eraser dialog did not open ' + JSON.stringify(window.__errs)};
  for (let i = 0; i < 60 && !ov.width; i++) await sleep(50);
  if (!ov.width) return {err: 'brush-broken', detail: 'the mask never sized itself (picture did not load)'};
  const title = (document.querySelector('.modal h3') || {}).textContent || '';
  const go = document.getElementById('er-go');
  const disabledBefore = go.disabled;

  const r = art.getBoundingClientRect();
  const pt = (fx, fy) => ({clientX: r.left + r.width * fx, clientY: r.top + r.height * fy});
  const fire = (t, p) => art.parentElement.dispatchEvent(new PointerEvent(t, {
    ...p, pointerId: 1, pointerType: '%PTYPE%', isPrimary: true, bubbles: true, cancelable: true}));
  fire('pointerdown', pt(0.3, 0.5));
  for (let i = 4; i <= 7; i++) fire('pointermove', pt(i / 10, 0.5));
  fire('pointerup', pt(0.7, 0.5));

  const c = ov.getContext('2d');
  const alphaAt = (fx, fy) => c.getImageData(Math.round(ov.width * fx), Math.round(ov.height * fy), 1, 1).data[3];
  const painted = alphaAt(0.5, 0.5), corner = alphaAt(0.04, 0.04);
  const vw = innerWidth, vh = innerHeight;
  const box = id => { const e = document.getElementById(id); return e ? e.getBoundingClientRect() : null; };
  const gb = box('er-go'), pic = art.getBoundingClientRect();
  const small = ['er-rub','er-put','er-undo','er-all','er-go','er-cancel']
    .map(id => ({id, h: Math.round((box(id)||{height:0}).height)})).filter(x => x.h < (%MINH%));
  const touchAction = getComputedStyle(art.parentElement).touchAction;
  const overflow = document.documentElement.scrollWidth > vw + 1;
  const disabledAfter = go.disabled;

  go.click();
  const progress = [];
  for (let i = 0; i < 80 && layer().src === before; i++) {
    const h = document.getElementById('er-hint'); if (h) progress.push(h.textContent);
    await sleep(50);
  }
  // Decode the mask that was SENT and sample it: brushed middle must be marked, an untouched corner not.
  const body = window.__magicBody || {};
  let mMid = -1, mCorner = -1;
  if (body.mask) {
    const im = new Image(); im.src = body.mask;
    await new Promise(res => { im.onload = res; im.onerror = res; });
    const cv = document.createElement('canvas'); cv.width = im.naturalWidth; cv.height = im.naturalHeight;
    const cc = cv.getContext('2d'); cc.drawImage(im, 0, 0);
    const px = (fx, fy) => { const d = cc.getImageData(Math.round(cv.width*fx), Math.round(cv.height*fy), 1, 1).data;
                             return d[3] > 127 && (d[0] + d[1] + d[2]) / 3 > 127 ? 1 : 0; };
    mMid = px(0.5, 0.5); mCorner = px(0.04, 0.04);
  }
  const after = layer();
  const undoBtn = document.getElementById('mb-undo');
  return {
    btnOk, btnBox, eraseBox, title, disabledBefore, disabledAfter, painted, corner, small, touchAction, overflow,
    picOnScreen: pic.top >= -1 && pic.bottom <= vh + 1 && pic.left >= -1 && pic.right <= vw + 1,
    applyOnScreen: !!gb && gb.bottom <= vh + 1 && gb.top >= -1 && gb.left >= -1 && gb.right <= vw + 1,
    sentUrl: body.url || '', before, mMid, mCorner,
    progressSeen: progress.some(t => /Filling/.test(t)),
    src: after.src || '', origSrc: after.origSrc || '', type: after.type,
    stillOpen: !!document.getElementById('er-src'),
    revertBtn: !!document.getElementById('mb-fx-revert'),
    undoEnabled: !!undoBtn && !undoBtn.disabled,
    toasts: window.__toasts.slice(-2),
  };
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
                r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                                    "awaitPromise": awaited})
                if not r or r.get("exceptionDetails"):
                    return None
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            for label, w, h, mobile in SIZES:
                await call("Emulation.setDeviceMetricsOverride",
                           {"width": w, "height": h, "deviceScaleFactor": 2 if mobile else 1, "mobile": mobile})
                await call("Emulation.setTouchEmulationEnabled", {"enabled": mobile, "maxTouchPoints": 5})
                await call("Page.navigate", {"url": url})
                for _ in range(40):
                    await asyncio.sleep(0.25)
                    if await js("window.__ready === true"):
                        break
                res = await js(PROBE.replace("%PTYPE%", "touch" if mobile else "mouse")
                               .replace("%MINH%", "24" if mobile else "16"), awaited=True)
                lbl = f"{label}@{w}px"
                if not res:
                    problems.append((lbl, "apply-broken", "the probe did not run"))
                    continue
                if res.get("err"):
                    problems.append((lbl, res["err"], res.get("detail", "")))
                    continue
                if not res["btnOk"]:
                    problems.append((lbl, "layout", f"the Magic Eraser button is off screen or too small {res['btnBox']} (erase: {res['eraseBox']})"))
                if "Magic Eraser" not in res["title"]:
                    problems.append((lbl, "missing-control", f"the dialog is titled {res['title']!r}"))
                if not res["disabledBefore"]:
                    problems.append((lbl, "apply-broken", "Remove it is enabled before anything was brushed"))
                if res["disabledAfter"]:
                    problems.append((lbl, "apply-broken", "Remove it stayed disabled after a stroke"))
                if not res["painted"]:
                    problems.append((lbl, "brush-broken", "the stroke did not mark the picture"))
                if res["corner"]:
                    problems.append((lbl, "brush-broken", "a corner the stroke never touched is marked"))
                if res["mMid"] != 1 or res["mCorner"] != 0:
                    problems.append((lbl, "wrong-polarity",
                                     f"sent mask: brushed middle={res['mMid']} untouched corner={res['mCorner']} "
                                     "(want 1 and 0 — WHITE is what gets filled)"))
                if res["touchAction"] != "none":
                    problems.append((lbl, "layout", f"drawing surface has touch-action:{res['touchAction']}"))
                if res["overflow"]:
                    problems.append((lbl, "layout", "the dialog scrolls the page sideways"))
                if not res["picOnScreen"]:
                    problems.append((lbl, "layout", "the picture is not fully on screen"))
                if not res["applyOnScreen"]:
                    problems.append((lbl, "layout", "Remove it is off screen"))
                for s in res["small"]:
                    problems.append((lbl, "layout", f"#{s['id']} is {s['h']}px tall"))
                if res["sentUrl"] != res["before"]:
                    problems.append((lbl, "apply-broken",
                                     f"sent url {res['sentUrl']!r}, the layer's picture is {res['before']!r}"))
                if not res["progressSeen"]:
                    problems.append((lbl, "apply-broken", "no progress was shown while it worked"))
                if res["src"] != FILLED:
                    problems.append((lbl, "apply-broken", f"the layer was not replaced (src={res['src']!r})"))
                if res["type"] != "image":
                    problems.append((lbl, "apply-broken", f"the layer became a {res['type']!r}"))
                if res["origSrc"] != res["before"]:
                    problems.append((lbl, "apply-broken", "origSrc was not kept — ↺ cannot put the photo back"))
                if not res["revertBtn"]:
                    problems.append((lbl, "apply-broken", "no ↺ Undo-the-effect button after the fill"))
                if not res["undoEnabled"]:
                    problems.append((lbl, "apply-broken", "the builder's Undo is not armed after the fill"))
                if res["stillOpen"]:
                    problems.append((lbl, "apply-broken", "the dialog stayed open"))
                print(f"{lbl}: painted={res['painted']} corner={res['corner']} mask(mid={res['mMid']},"
                      f"corner={res['mCorner']}) src={'replaced' if res['src'] == FILLED else 'SAME'} "
                      f"undo={'yes' if res['undoEnabled'] else 'NO'} toast={res['toasts'][-1:]}")
        if not problems:
            print("OK  magic eraser checks passed")
            return 0
        print()
        for where, kind, detail in problems:
            print(f"FAIL  [{where}] {kind}: {detail}")
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(PROFILE, ignore_errors=True)


def main():
    import importlib.util
    if importlib.util.find_spec("websockets") is None:
        print("SKIP  websockets not installed")
        return 2
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="mememagic-")
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
    try:
        return asyncio.run(drive(f"http://127.0.0.1:{srv.server_port}/index.html"))
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
