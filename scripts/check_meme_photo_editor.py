#!/usr/bin/env python3
"""MEME BUILDER AS A PHOTO EDITOR: exact layer sizes, image/canvas size, and an export that really is
the size it says.

    venv-unified/bin/python scripts/check_meme_photo_editor.py

Asked for as "resize images/layers a specified size like in Photo Editors … export images in the
desired size". Three features, each of which can look right and be wrong:

  layer-resize     Resize… on a layer sets EXACT pixels, "keep proportions" follows the other side,
                   percent scales from the size the dialog opened at, and the layer stays centred.
  canvas-size      "Canvas size" crops/extends around an ANCHOR without scaling a single layer (every
                   layer moves by the anchor offset); "Image size" resamples canvas AND layers together.
  export-size      🖼 Export image sends fmt/quality/out_w/out_h, saves through saveBlobAs (never a bare
                   <a download>, which the APK ignores), and — the part that matters — the edit list the
                   BUTTON built, pushed through the REAL renderer, produces a file of exactly that
                   format and pixel size. The payload is captured from the page, never rebuilt here
                   (project_meme_preview_render_contract: a harness with its own edit list tests a
                   render nobody can ask for).
  dialog-fits      every control in the three dialogs is on screen at 360px and 390px.

Drives the shipped meme.js against a stubbed window.__PC (the check_meme_mobile approach). Needs Chrome
and ffmpeg; exit 2 when either is missing.

Exit 0 = clean, 1 = regressions (printed), 2 = could not run.
"""
import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PORT = int(os.environ.get("PC_CHECK_PORT") or 9496)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-meme-photo-check"
WIDTHS = [(390, 844), (360, 780)]
SRC = "/static/icon-192.png"

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css">
</head><body>
<div id="modal-root"></div>
<div id="feed"></div>
<script>
localStorage.setItem('pc_meme_project', JSON.stringify({
  name:'photo check', w:720, h:1280, fps:12, bg:'#000000', duration:4,
  layers:[{id:'L1', type:'image', src:'%(src)s', name:'photo',
           start:0, dur:4, trim:0, x:0, y:320, w:720, h:640,
           opacity:1, effect:'none', volume:1, mute:false,
           flipH:false, flipV:false, rotate:0, sound:'', soundVolume:1,
           text:'', size:64, color:'#ffffff', stroke:'#000000', align:'', fit:'contain'}]
}));
window.__toasts = []; window.__saved = []; window.__renderBody = null;
window.__PC = {
  isView(v){ return v === 'meme'; },
  toast(m){ window.__toasts.push(String(m)); },
  async uploadBlob(){ return 'https://example.invalid/x.png'; },
  async selfProof(){ return 'proof'; },
  async uiConfirm(){ return false; }, async uiPrompt(){ return null; },
  async saveBlobAs(blob, name){ window.__saved.push({name, type: blob.type, size: blob.size}); return 'saved'; },
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
// /client/meme/render answers with an image of the REQUESTED size and type, drawn here — the real
// renderer is run on the captured body by the harness, in Python, against the real ffmpeg.
const _fetch = window.fetch.bind(window);
window.fetch = async (u, o) => {
  if (!String(u).includes('/client/meme/render')) return _fetch(u, o);
  const body = JSON.parse((o || {}).body || 'null'); window.__renderBody = body;
  const e = body.edit, W = e.out_w || e.w, H = e.out_h || e.h;
  const c = new OffscreenCanvas(W, H); c.getContext('2d').fillRect(0, 0, W, H);
  const type = e.fmt === 'jpeg' ? 'image/jpeg' : (e.fmt === 'webp' ? 'image/webp' : 'image/png');
  const blob = await c.convertToBlob({type});
  return new Response(blob, {status: 200, headers: {'Content-Type': type}});
};
</script>
<script src="/static/js/client/sprite.js"></script>
<script src="/static/js/client/meme.js"></script>
<script>
(function boot(){
  if(!window.PCMeme) return setTimeout(boot, 30);
  window.PCMeme.render();
  setTimeout(()=>{ window.__ready = true; }, 400);
})();
</script>
</body></html>""" % {"src": SRC}

HELPERS = r"""
window.__q = () => JSON.parse(localStorage.getItem('pc_meme_project') || 'null');
window.__L = id => (__q().layers || []).find(l => l.id === id);
window.__set = (id, v) => { const e = document.getElementById(id); e.value = String(v);
  e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true})); };
window.__wait = async (f, n) => { for (let i = 0; i < (n || 60); i++) { const v = f(); if (v) return v;
  await new Promise(r => setTimeout(r, 50)); } return null; };
window.__offscreen = () => {
  const vw = document.documentElement.clientWidth, bad = [];
  document.querySelectorAll('#modal-root button, #modal-root input, #modal-root select').forEach(el => {
    const cs = getComputedStyle(el); if (cs.display === 'none' || cs.visibility === 'hidden') return;
    if (el.closest('[hidden]')) return;
    const r = el.getBoundingClientRect(); if (!r.width) return;
    if (r.left < -0.5 || r.right > vw + 0.5) bad.push((el.id || el.textContent.trim().slice(0, 16)) +
      ' ' + Math.round(r.left) + '..' + Math.round(r.right) + ' of ' + vw);
  });
  return bad;
};
window.__openTab = async name => { const t = document.querySelector('.mb-tab[data-tab="' + name + '"]');
  if (t) t.click(); await new Promise(r => setTimeout(r, 80)); };
"""

LAYER_RESIZE = r"""(async () => {
  const out = {};
  const row = document.querySelector('.mb-track[data-id="L1"]'); if (row) row.click();
  await __openTab('layer');
  const b = document.getElementById('mb-resize');
  if (!b) return {err: 'no Resize… (#mb-resize) on a selected image layer'};
  b.click();
  if (!await __wait(() => document.getElementById('rz-w'))) return {err: 'Resize… opened no dialog'};
  out.offA = __offscreen();
  __set('rz-w', 360);
  out.followH = +document.getElementById('rz-h').value;       // 720x640 at 360 wide -> 320
  document.getElementById('rz-go').click();
  await new Promise(r => setTimeout(r, 120));
  const a = __L('L1'); out.a = [a.x, a.y, a.w, a.h];           // centred on (360,640) -> 180,480
  document.getElementById('mb-resize') ? document.getElementById('mb-resize').click()
                                       : (await __openTab('layer'), document.getElementById('mb-resize').click());
  await __wait(() => document.getElementById('rz-pct'));
  __set('rz-pct', 50);
  out.pctW = +document.getElementById('rz-w').value; out.pctH = +document.getElementById('rz-h').value;
  document.getElementById('rz-go').click();
  await new Promise(r => setTimeout(r, 120));
  const c = __L('L1'); out.b = [c.x, c.y, c.w, c.h];
  return out;
})()"""

CANVAS_SIZE = r"""(async () => {
  const out = {};
  await __openTab('canvas');
  const ext = document.getElementById('mb-cvsize'), res = document.getElementById('mb-imgsize');
  if (!ext || !res) return {err: 'the Canvas pane has no Image size… / Canvas size… buttons'};
  const before = __L('L1'); out.before = [before.x, before.y, before.w, before.h];
  ext.click();
  if (!await __wait(() => document.getElementById('cs-w'))) return {err: 'Canvas size… opened no dialog'};
  out.offA = __offscreen();
  out.lockedInExtend = document.getElementById('cs-lock').checked;
  __set('cs-w', 800); __set('cs-h', 1400);
  document.getElementById('cs-go').click();
  await new Promise(r => setTimeout(r, 150));
  let p = __q(), l = __L('L1');
  out.extend = {w: p.w, h: p.h, layer: [l.x, l.y, l.w, l.h]};
  await __openTab('canvas');
  document.getElementById('mb-imgsize').click();
  await __wait(() => document.getElementById('cs-pct'));
  __set('cs-pct', 50);
  document.getElementById('cs-go').click();
  await new Promise(r => setTimeout(r, 150));
  p = __q(); l = __L('L1');
  out.resample = {w: p.w, h: p.h, layer: [l.x, l.y, l.w, l.h]};
  return out;
})()"""

EXPORT = r"""(async () => {
  const out = {};
  await __openTab('canvas');
  const b = document.getElementById('mb-export-img');
  if (!b) return {err: 'the Canvas pane has no Export image… button'};
  b.click();
  if (!await __wait(() => document.getElementById('ex-w'))) return {err: 'Export image… opened no dialog'};
  out.offA = __offscreen();
  __set('ex-fmt', 'png');
  out.qHiddenForPng = getComputedStyle(document.getElementById('ex-q-wrap')).display === 'none';
  __set('ex-fmt', 'jpeg'); __set('ex-q', 70);
  out.qShown = getComputedStyle(document.getElementById('ex-q-wrap')).display !== 'none';
  document.getElementById('ex-lock').checked = false;
  document.getElementById('ex-lock').dispatchEvent(new Event('change'));
  __set('ex-w', 333); __set('ex-h', 517);
  window.__saved = []; window.__renderBody = null;
  document.getElementById('ex-go').click();
  await __wait(() => window.__saved.length, 100);
  out.saved = window.__saved.slice();
  out.body = window.__renderBody;
  out.toast = window.__toasts.slice(-1)[0] || '';
  out.dialogClosed = !document.getElementById('ex-w');
  return out;
})()"""


async def drive(url, chrome):
    import websockets
    td = PROFILE if os.environ.get("PC_CHECK_PROFILE") else tempfile.mkdtemp(prefix="pc-memephoto-")
    shutil.rmtree(td, ignore_errors=True)
    os.makedirs(td, exist_ok=True)
    proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                             "--remote-debugging-port=%d" % PORT, "--user-data-dir=%s" % td, "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bad = []
    try:
        tab = None
        for _ in range(60):
            try:
                tab = next(t for t in json.load(urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % PORT)) if t.get("type") == "page")
                break
            except Exception:
                await asyncio.sleep(0.25)
        if not tab:
            print("SKIP  Chrome never opened a debuggable tab")
            return 2, None
        async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=1 << 25) as ws:
            n = [0]

            async def call(method, params=None):
                n[0] += 1
                await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == n[0]:
                        return msg.get("result") or {}

            async def js(expr):
                r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                                    "awaitPromise": True})
                if r.get("exceptionDetails"):
                    d = (r["exceptionDetails"].get("exception") or {}).get("description")
                    return {"err": "threw: %s" % (d or r["exceptionDetails"].get("text"))}
                return (r.get("result") or {}).get("value")

            await call("Runtime.enable")
            body = None
            for w, h in WIDTHS:
                await call("Emulation.setDeviceMetricsOverride",
                           {"width": w, "height": h, "deviceScaleFactor": 2, "mobile": True})
                await call("Page.navigate", {"url": url})
                for _ in range(100):
                    if await js("!!window.__ready") is True:
                        break
                    await asyncio.sleep(0.1)
                else:
                    print("FAIL  the builder never booted at %dpx" % w)
                    return 1, None
                await js(HELPERS)
                tag = "%dpx" % w

                r = await js(LAYER_RESIZE)
                if r.get("err"):
                    bad.append(("layer-resize", "%s: %s" % (tag, r["err"])))
                else:
                    if r["followH"] != 320:
                        bad.append(("layer-resize", "%s: keep-proportions made 360 wide -> %s tall, not 320"
                                    % (tag, r["followH"])))
                    if r["a"] != [180, 480, 360, 320]:
                        bad.append(("layer-resize", "%s: resize to 360x320 left the layer at %r, expected "
                                    "[180, 480, 360, 320] (exact size, still centred)" % (tag, r["a"])))
                    if (r["pctW"], r["pctH"]) != (180, 160):
                        bad.append(("layer-resize", "%s: 50%% of 360x320 read %sx%s" % (tag, r["pctW"], r["pctH"])))
                    if r["b"][2:] != [180, 160]:
                        bad.append(("layer-resize", "%s: applying 50%% left the layer %r" % (tag, r["b"])))
                    for o in r["offA"]:
                        bad.append(("dialog-fits", "%s Resize layer: %s" % (tag, o)))

                r = await js(CANVAS_SIZE)
                if r.get("err"):
                    bad.append(("canvas-size", "%s: %s" % (tag, r["err"])))
                else:
                    x, y, lw, lh = r["before"]
                    if r["lockedInExtend"]:
                        bad.append(("canvas-size", "%s: Canvas size opened with proportions locked — "
                                    "extending one side is its main use" % tag))
                    want = {"w": 800, "h": 1400, "layer": [x + 40, y + 60, lw, lh]}
                    if r["extend"] != want:
                        bad.append(("canvas-size", "%s: extending 720x1280 -> 800x1400 around the centre "
                                    "gave %r, expected %r (layers moved, not scaled)" % (tag, r["extend"], want)))
                    ex = r["extend"]["layer"]
                    want = {"w": 400, "h": 700, "layer": [round(ex[0] / 2), round(ex[1] / 2),
                                                          round(ex[2] / 2), round(ex[3] / 2)]}
                    if r["resample"] != want:
                        bad.append(("canvas-size", "%s: Image size at 50%% gave %r, expected %r (canvas "
                                    "and every layer halved)" % (tag, r["resample"], want)))
                    for o in r["offA"]:
                        bad.append(("dialog-fits", "%s Canvas size: %s" % (tag, o)))

                r = await js(EXPORT)
                if r.get("err"):
                    bad.append(("export-size", "%s: %s" % (tag, r["err"])))
                    continue
                for o in r["offA"]:
                    bad.append(("dialog-fits", "%s Export image: %s" % (tag, o)))
                e = (r.get("body") or {}).get("edit") or {}
                if (e.get("fmt"), e.get("out_w"), e.get("out_h"), e.get("quality")) != ("jpeg", 333, 517, 70):
                    bad.append(("export-size", "%s: the export sent fmt/out_w/out_h/quality = %r"
                                % (tag, (e.get("fmt"), e.get("out_w"), e.get("out_h"), e.get("quality")))))
                if not r["qShown"]:
                    bad.append(("export-size", "%s: JPEG shows no quality control" % tag))
                if not r["qHiddenForPng"]:
                    bad.append(("export-size", "%s: PNG (lossless) still shows a quality slider that "
                                "does nothing" % tag))
                saved = r.get("saved") or []
                if len(saved) != 1 or not saved[0]["name"].endswith("-333x517.jpg") \
                        or saved[0]["type"] != "image/jpeg":
                    bad.append(("export-size", "%s: saveBlobAs got %r (expected one *-333x517.jpg image/jpeg)"
                                % (tag, saved)))
                if "⚠" in r["toast"] or not r["dialogClosed"]:
                    bad.append(("export-size", "%s: the export ended with %r (dialog closed: %s)"
                                % (tag, r["toast"], r["dialogClosed"])))
                body = r.get("body")
            return (1 if bad else 0), (bad, body)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def render_captured(body, bad):
    """The edit list the Export button built, through the REAL renderer: the file must be what the
    dialog said."""
    from PIL import Image
    from app.services import meme_builder_service as mb
    if not mb.media_service.resolve_ffmpeg():
        print("SKIP  no ffmpeg — the captured export could not be rendered")
        return 2
    edit = body["edit"]
    local = os.path.join(ROOT, SRC.lstrip("/"))
    sources = {l["src"]: local for l in edit["layers"] if l.get("src")}
    for fmt, ext_fmt in (("jpeg", "JPEG"), ("webp", "WEBP"), ("png", "PNG")):
        try:
            data, ctype = mb.render(dict(edit, fmt=fmt), sources)
            im = Image.open(io.BytesIO(data))
        except Exception as e:
            bad.append(("export-size", "the captured export did not render as %s: %s" % (fmt, e)))
            continue
        if (im.format, im.size) != (ext_fmt, (333, 517)):
            bad.append(("export-size", "the captured export rendered as %s %r for fmt=%s, expected %s 333x517"
                        % (im.format, im.size, fmt, ext_fmt)))
    return 0


def main():
    import importlib.util
    if importlib.util.find_spec("websockets") is None:
        print("SKIP  websockets not installed")
        return 2
    chrome = next((shutil.which(x) for x in ("google-chrome-stable", "chromium", "google-chrome",
                                             "chromium-browser") if shutil.which(x)), None)
    if not chrome:
        print("SKIP  no Chrome on this box")
        return 2
    import functools
    import http.server
    import threading

    class H(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] == "/":
                raw = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            return super().do_GET()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=ROOT))
    srv.handle_error = lambda *a: None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rc, res = asyncio.run(drive("http://127.0.0.1:%d/" % srv.server_address[1], chrome))
    finally:
        srv.shutdown()
    if rc == 2:
        return 2
    bad, body = res if res else ([], None)
    if body:
        if render_captured(body, bad) == 2 and not bad:
            return 2
    elif not bad:
        bad.append(("export-size", "no export request was captured"))
    # The result pane's Download must not be a bare anchor (the APK ignores it).
    src = open(os.path.join(ROOT, "static/js/client/meme.js"), encoding="utf-8").read()
    if 'download="${' in src:
        bad.append(("export-size", "meme.js still saves with a bare <a download> — use saveBlobAs"))
    if bad:
        print("FAIL  %d problem(s)" % len(bad))
        for rule, d in bad:
            print("  [%s] %s" % (rule, d))
        return 1
    print("OK  layer resize, image/canvas size and a 333x517 JPEG/WebP/PNG export all do what they say "
          "at %s px" % "/".join(str(w) for w, _ in WIDTHS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
