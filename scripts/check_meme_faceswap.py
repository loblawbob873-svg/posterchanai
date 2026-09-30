#!/usr/bin/env python3
"""Browser check for the Meme Builder's 🔄 FACE SWAP, at phone AND desktop size.

    venv-unified/bin/python scripts/check_meme_faceswap.py

Asked for: "Meme Builder -> If you can add a face swapping feature that would be cool, make sure UI good
on mobile and desktop". Every failure is quiet, so each is measured:

  missing-control   no Face swap button on an image layer's panel.
  boxes-wrong       the numbered faces are not drawn, or not ON the picture (they are positioned in %
                    over whatever size the dialog shows it).
  pick-broken       tapping faces does not choose them, or Swap is enabled without two faces.
  apply-broken      the request did not carry the layer's own url and the chosen faces, the picture
                    was not replaced, the dialog stayed open, or the way back (origSrc for ↺, the
                    builder's undo) was lost.
  paste-broken      "Use another face" does not offer the project's other pictures, or does not send
                    the source, its face and the chosen targets.
  layout            horizontal overflow, the picture or Swap off screen, tap targets too small.

Drives the SHIPPED meme.js against a stubbed `window.__PC` (the check_meme_magic_eraser.py harness);
the server is stubbed at fetch(), because the swap itself is covered on a real photo by
tests/test_faceswap.py.

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
PORT = int(os.environ.get("PC_CHECK_PORT") or 9483)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-meme-faceswap-check"
SIZES = [("phone", 390, 844, True), ("desktop", 1440, 900, False)]
SWAPPED = "/static/icon-512.png?swapped=1"

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css">
</head><body>
<div id="modal-root"></div>
<div id="feed"></div>
<script>
const LAYER = (id, src, y) => ({id, type:'image', src, name:id, start:0, dur:4, trim:0, x:0, y, w:720, h:640,
  opacity:1, effect:'none', volume:1, mute:false, flipH:false, flipV:false, rotate:0, sound:'', soundVolume:1,
  text:'', size:64, color:'#ffffff', stroke:'#000000', align:''});
localStorage.setItem('pc_meme_project', JSON.stringify({
  name:'check', w:720, h:1280, fps:12, bg:'#000000', duration:4,
  layers:[LAYER('L1', '/static/icon-192.png', 320), LAYER('L2', '/static/icon-512.png', 0)]}));
window.__toasts = []; window.__errs = [];
addEventListener('error', e => window.__errs.push(String(e.message)));
window.__PC = {
  isView(v){ return v === 'meme'; },
  toast(m){ window.__toasts.push(String(m)); },
  async uploadBlob(f){ window.__uploaded = (window.__uploaded||[]).concat([f && f.type]); return '/static/icon-512.png?uploaded=1'; },
  async selfProof(){ return 'proof'; },
  async uiConfirm(){ return false; }, async uiPrompt(){ return null; },
  modal(html, onMount){
    const bg = document.createElement('div'); bg.className = 'modal-bg';
    bg.innerHTML = '<div class="modal glass neon-border">' + html + '</div>';
    document.getElementById('modal-root').appendChild(bg);
    const box = bg.querySelector('.modal'); if (onMount) onMount(box);
  },
  closeModal(){ document.getElementById('modal-root').innerHTML = ''; },
  // 📁 Files: the picker answers with whatever __drivePick holds; an encrypted pick is decrypted
  // through encFileUrl and re-uploaded (uploadBlob), which is recorded.
  blossomPicker(ta, onPick){ window.__driveOpened = (window.__driveOpened||0) + 1; setTimeout(() => onPick(window.__drivePick), 30); },
  async encFileUrl(sha){ window.__decrypted = sha; return '/static/icon-512.png'; },
  openGenStudio(){}, openVoiceStudio(){},
  openEmojiPopover(){ return ''; }, instEmojiUrl(){ return ''; },
  mediaServer:'', eTags(){ return []; }, profOf(){ return {}; },
  get ME(){ return {pubkey:'0'.repeat(64)}; }, get CFG(){ return {}; }, get VIEW(){ return 'meme'; },
};
// Three faces in the layer's picture, one in the other picture; every request body is KEPT.
const _fetch = window.fetch.bind(window);
window.__bodies = [];
const J = o => new Promise(res => setTimeout(() => res(new Response(JSON.stringify(o),
  {status:200, headers:{'Content-Type':'application/json'}})), 150));
window.fetch = (u, o) => {
  const s = String(u), b = JSON.parse((o || {}).body || 'null');
  if (s.includes('/client/meme/faceswap')) {
    window.__bodies.push(['swap', b]);
    return J({ok:true, url:'%SWAPPED%', effect:'faceswap', is_video:false});
  }
  if (s.includes('/client/meme/faces')) {
    window.__bodies.push(['faces', b]);
    return J(b.url.includes('512') && !b.url.includes('swapped')
      ? {width:512, height:512, faces:[{x:0.3, y:0.25, w:0.4, h:0.45}]}
      : {width:192, height:192, faces:[{x:0.05, y:0.2, w:0.25, h:0.3}, {x:0.38, y:0.15, w:0.25, h:0.3}, {x:0.7, y:0.25, w:0.25, h:0.3}]});
  }
  return _fetch(u, o);
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
</body></html>""".replace("%SWAPPED%", SWAPPED)

PROBE = r"""(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const until = async (f, n = 80) => { for (let i = 0; i < n && !f(); i++) await sleep(50); return f(); };
  const layer = () => ((JSON.parse(localStorage.getItem('pc_meme_project')||'null')||{layers:[]})
                        .layers.find(l => l.id === 'L1') || {});
  const R = e => { const r = e.getBoundingClientRect(); return {l:r.left, t:r.top, r:r.right, b:r.bottom, w:r.width, h:r.height}; };
  const btn = document.getElementById('mb-faceswap');
  if (!btn) return {err:'missing-control', detail:'no #mb-faceswap on the image layer panel ' + JSON.stringify(window.__errs)};
  btn.scrollIntoView({block:'center'});
  const bb = R(btn);
  const before = layer().src;
  btn.click();
  if (!await until(() => document.querySelectorAll('#fs-boxes .mb-fs-face').length === 3))
    return {err:'boxes-wrong', detail:'the three faces were not drawn ' + JSON.stringify(window.__errs)};
  const img = document.getElementById('fs-img');
  await until(() => img.complete && img.naturalWidth > 0);
  await sleep(100);
  const pic = R(img);
  const boxes = [...document.querySelectorAll('#fs-boxes .mb-fs-face')].map(R);
  const onPic = boxes.every(b => b.l >= pic.l - 1 && b.r <= pic.r + 1 && b.t >= pic.t - 1 && b.b <= pic.b + 1);
  const say0 = document.getElementById('fs-say').textContent;
  // Swap mode is ONE picture: the other-face picker must not be on screen (a display rule once beat `hidden`).
  const sourceInSwap = document.getElementById('fs-source').getBoundingClientRect().height > 0;
  const go = document.getElementById('fs-go');
  const goOn0 = !go.disabled;
  const faceBtn = i => document.querySelectorAll('#fs-boxes .mb-fs-face')[i];
  faceBtn(0).click(); await sleep(50);                  // un-pick face 1: only one left
  const goOn1 = !go.disabled;
  faceBtn(2).click(); await sleep(50);                  // pick face 3: faces 2 and 3
  const say2 = document.getElementById('fs-say').textContent;
  const pressed = [...document.querySelectorAll('#fs-boxes .mb-fs-face')].map(b => b.getAttribute('aria-pressed'));
  const vw = innerWidth, vh = innerHeight;
  const layout = {overflow: document.documentElement.scrollWidth > vw + 1,
    pic: pic.t >= -1 && pic.b <= vh + 1 && pic.l >= -1 && pic.r <= vw + 1,
    go: (b => b.b <= vh + 1 && b.t >= -1 && b.l >= -1 && b.r <= vw + 1)(R(go)),
    small: ['fs-go','fs-cancel'].concat([...document.querySelectorAll('[data-fsmode]')].map((_, i) => 'tab' + i))
      .map(id => id.startsWith('tab') ? document.querySelectorAll('[data-fsmode]')[+id.slice(3)] : document.getElementById(id))
      .filter(e => R(e).h < (%MINH%)).length,
    faceMin: Math.min(...boxes.map(b => Math.min(b.w, b.h)))};
  go.click();
  await until(() => layer().src !== before);
  const swapBody = (window.__bodies.find(b => b[0] === 'swap') || [])[1] || {};
  const after = layer();
  const swapped = {src: after.src, origSrc: after.origSrc || '', type: after.type,
    stillOpen: !!document.getElementById('fs-img'), revert: !!document.getElementById('mb-fx-revert'),
    undo: !!document.getElementById('mb-undo') && !document.getElementById('mb-undo').disabled};

  // "Use another face": the project's other picture is offered, its face found, targets chosen.
  const btn2 = document.getElementById('mb-faceswap');
  btn2.click();
  await until(() => document.querySelectorAll('#fs-boxes .mb-fs-face').length === 3);
  document.querySelector('[data-fsmode="paste"]').click();
  await sleep(50);
  const sourceShown = !document.getElementById('fs-source').hidden;
  const thumbs = [...document.querySelectorAll('.mb-fs-thumb')].map(t => t.dataset.src);
  const goPaste0 = !document.getElementById('fs-go').disabled;
  const thumb = document.querySelector('.mb-fs-thumb');
  if (thumb) thumb.click();
  await until(() => document.querySelectorAll('#fs-srcboxes .mb-fs-face').length === 1);
  const pasteSay = document.getElementById('fs-say').textContent;
  document.querySelectorAll('#fs-boxes .mb-fs-face')[0].click(); await sleep(50);
  const src = R(document.getElementById('fs-srcimg'));
  const goP = document.getElementById('fs-go');
  const pasteLayout = {overflow: document.documentElement.scrollWidth > vw + 1,
    src: src.l >= -1 && src.r <= vw + 1, go: R(goP).b <= vh + 1 && R(goP).r <= vw + 1};
  const pasteState = {disabled: goP.disabled, say: document.getElementById('fs-say').textContent,
    targets: [...document.querySelectorAll('#fs-boxes .mb-fs-face')].map(b => b.getAttribute('aria-pressed'))};
  goP.click();
  await until(() => window.__bodies.filter(b => b[0] === 'swap').length === 2);
  const pasteBody = (window.__bodies.filter(b => b[0] === 'swap')[1] || [])[1] || {};
  // 📁 Files as the source: a plain drive picture is used as it is; an encrypted one is decrypted and
  // a copy uploaded, and THAT is what the swap reads.
  const faceUrls = () => window.__bodies.filter(b => b[0] === 'faces').map(b => b[1].url);
  // The paste above closes its dialog when its answer lands; opening before that lets the late close
  // take the new dialog with it.
  await until(() => !document.querySelector('.mb-fs-modal'));
  document.getElementById('mb-faceswap').click();
  await until(() => document.querySelectorAll('#fs-boxes .mb-fs-face').length === 3);
  document.querySelector('[data-fsmode="paste"]').click(); await sleep(50);
  const driveBtn = !!document.getElementById('fs-drive');
  window.__drivePick = {url:'/static/icon-512.png?plain=1', type:'image/png', sha:'', enc:false, name:'plain.png'};
  document.getElementById('fs-drive') && document.getElementById('fs-drive').click();
  await until(() => faceUrls().includes('/static/icon-512.png?plain=1'));
  const plainUsed = faceUrls().includes('/static/icon-512.png?plain=1');
  window.__afterPlain = {drive: !!document.getElementById('fs-drive'), dialogs: document.querySelectorAll('.mb-fs-modal').length, modalRoot: document.getElementById('modal-root').children.length};
  window.__drivePick = {url:'/blossom/ciphertext', type:'image/jpeg', sha:'e'.repeat(64), enc:true, name:'secret.jpg'};
  document.getElementById('fs-drive') && document.getElementById('fs-drive').click();
  await until(() => faceUrls().includes('/static/icon-512.png?uploaded=1'));
  const encUsed = faceUrls().includes('/static/icon-512.png?uploaded=1') && !faceUrls().includes('/blossom/ciphertext');
  const encDecrypted = window.__decrypted === 'e'.repeat(64), encUploadedType = (window.__uploaded || []).slice(-1)[0];
  const driveOpened = window.__driveOpened || 0, faceUrlsSeen = faceUrls(), afterPlain = window.__afterPlain;
  return {afterPlain, driveOpened, faceUrlsSeen, driveBtn, plainUsed, encUsed, encDecrypted, encUploadedType, btnOk: bb.w > 0 && bb.h >= (%MINH%) && bb.l >= -1 && bb.r <= vw + 1, btnBox:[bb.l, bb.t, bb.w, bb.h, vw],
          onPic, sourceInSwap, say0, goOn0, goOn1, say2, pressed, layout, before, swapBody, swapped,
          sourceShown, thumbs, goPaste0, pasteSay, pasteLayout, pasteBody, pasteState, errs: window.__errs, toasts: window.__toasts.slice(-2)};
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
                res = await js(PROBE.replace("%MINH%", "24" if mobile else "16"), awaited=True)
                lbl = f"{label}@{w}px"
                if not res:
                    problems.append((lbl, "apply-broken", "the probe did not run"))
                    continue
                if res.get("err"):
                    problems.append((lbl, res["err"], res.get("detail", "")))
                    continue
                P = lambda kind, why: problems.append((lbl, kind, why))
                if not res["btnOk"]:
                    P("layout", f"the Face swap button is off screen or too small {res['btnBox']}")
                if not res["onPic"]:
                    P("boxes-wrong", "a numbered face is not on the picture")
                if res["sourceInSwap"]:
                    P("layout", "the other-face picker is showing in Swap two faces")
                if not res["goOn0"] or "Faces 1 and 2" not in res["say0"]:
                    P("pick-broken", f"faces 1 and 2 are not picked by default ({res['say0']!r})")
                if res["goOn1"]:
                    P("pick-broken", "Swap is enabled with only one face picked")
                if "Faces 2 and 3" not in res["say2"] or res["pressed"] != ["false", "true", "true"]:
                    P("pick-broken", f"tapping did not pick faces 2 and 3 ({res['say2']!r}, {res['pressed']})")
                lay = res["layout"]
                if lay["overflow"] or not lay["pic"] or not lay["go"]:
                    P("layout", f"overflow={lay['overflow']} picture on screen={lay['pic']} Swap on screen={lay['go']}")
                if lay["small"]:
                    P("layout", f"{lay['small']} control(s) under {'24' if mobile else '16'}px tall")
                if lay["faceMin"] < 24:
                    P("layout", f"a face tap target is {lay['faceMin']:.0f}px")
                b = res["swapBody"]
                if (b.get("mode"), b.get("a"), b.get("b"), b.get("url")) != ("swap", 1, 2, res["before"]):
                    P("apply-broken", f"swap sent {b!r}")
                s = res["swapped"]
                if s["src"] != SWAPPED or s["type"] != "image":
                    P("apply-broken", f"the layer was not replaced ({s['src']!r})")
                if s["origSrc"] != res["before"] or not s["revert"] or not s["undo"]:
                    P("apply-broken", f"the way back was lost (origSrc={s['origSrc']!r} revert={s['revert']} undo={s['undo']})")
                if s["stillOpen"]:
                    P("apply-broken", "the dialog stayed open")
                if not res["sourceShown"] or res["thumbs"] != ["/static/icon-512.png"]:
                    P("paste-broken", f"the other picture is not offered ({res['thumbs']})")
                if res["goPaste0"]:
                    P("paste-broken", "Swap is enabled before a source picture was chosen")
                if "every face" not in res["pasteSay"]:
                    P("paste-broken", f"the face does not default to every face ({res['pasteSay']!r})")
                pl = res["pasteLayout"]
                if pl["overflow"] or not pl["src"] or not pl["go"]:
                    P("layout", f"paste mode: overflow={pl['overflow']} source on screen={pl['src']} Swap on screen={pl['go']}")
                pb = res["pasteBody"]
                if (pb.get("mode"), pb.get("source"), pb.get("source_face"), pb.get("targets"), pb.get("url")) != \
                        ("paste", "/static/icon-512.png", 0, [1, 2], SWAPPED):
                    P("paste-broken", f"paste sent {pb!r} (state before Swap: {res['pasteState']})")
                if not res["driveBtn"]:
                    P("paste-broken", "no 📁 Files source in Use another face")
                if not res["plainUsed"]:
                    P("paste-broken", "a drive picture was not used as the face's source")
                if not (res["encUsed"] and res["encDecrypted"] and res.get("encUploadedType") == "image/jpeg"):
                    P("paste-broken", f"an encrypted drive picture was not decrypted and re-uploaded before use "
                      f"(used={res['encUsed']} decrypted={res['encDecrypted']} uploaded={res.get('encUploadedType')!r} opened={res.get('driveOpened')} after={res.get('afterPlain')} urls={res.get('faceUrlsSeen')})")
                if res["errs"]:
                    P("apply-broken", f"page errors: {res['errs']}")
                print(f"{lbl}: swap={res['swapBody'].get('a')}+{res['swapBody'].get('b')} "
                      f"paste targets={res['pasteBody'].get('targets')} toast={res['toasts'][-1:]}")
        if not problems:
            print("OK  face swap checks passed")
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
    tmp = tempfile.mkdtemp(prefix="memefaceswap-")
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
