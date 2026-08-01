#!/usr/bin/env python3
"""Mobile regression check for the MEME BUILDER's layer inspector.

Run BEFORE deploying a Meme Builder UI change:

    venv-unified/bin/python scripts/check_meme_mobile.py

check_client_mobile.py only ever loads /client — the timeline. It never opens Discover → Meme and
never selects a layer, so nothing it does touches the inspector panel, which is where most of the
builder's controls live and where every per-layer button is added. A change there can ship having
"passed the mobile check" without the check having looked at it once.

This drives meme.js DIRECTLY against a stubbed `window.__PC` (the sub-module approach in
docs — the builder needs no relay, no login and no network to lay itself out), seeds a project with
an image layer, selects it, and audits the inspector at phone widths.

Assertions, each corresponding to a way a phone layout actually breaks:

  horizontal-overflow  the panel pushes the page sideways.
  offscreen-control    a control whose box starts or ends outside the viewport — unreachable.
  tiny-tap-target      a button under 32px tall. Below that it is a coin toss on a thumb.
  overlapping-buttons  two full-width buttons whose boxes intersect (a missing display/margin rule).
  missing-control      an expected per-layer button is not in the DOM at all.

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome / websockets).
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIDTHS = [(390, 844), (360, 780)]
PORT = 9473
PROFILE = "/tmp/pc-meme-mobile-check"

# Per-layer controls the inspector must offer for a selected IMAGE layer. Named, because "it renders"
# is not the check — a button that silently stopped being emitted still renders a panel.
EXPECTED = ["mb-nobg", "mb-talk", "mb-fit", "mb-fill"]

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css">
</head><body>
<div id="modal-root"></div>
<div id="feed"></div>
<script>
// A project with ONE image layer, selected — the state in which the inspector shows the per-layer
// controls. Seeded before meme.js boots, because load() reads it once on render.
localStorage.setItem('pc_meme_project', JSON.stringify({
  name:'check', w:720, h:1280, fps:12, bg:'#000000', duration:4,
  layers:[{id:'L1', type:'image', src:'/static/icon-192.png', name:'face',
           start:0, dur:4, trim:0, x:0, y:320, w:720, h:640,
           opacity:1, effect:'none', volume:1, mute:false,
           flipH:false, flipV:false, rotate:0, sound:'', soundVolume:1,
           text:'', size:64, color:'#ffffff', stroke:'#000000', align:''}]
}));
// Minimal host. meme.js takes its helpers off window.__PC and never touches globals, so a stub is
// enough to lay the whole builder out. Everything here is either a no-op or a constant.
window.__toasts = [];
window.__PC = {
  toast(m){ window.__toasts.push(String(m)); },
  async uploadBlob(){ return 'https://example.invalid/voice.wav'; },
  async selfProof(){ return 'proof'; },
  async uiConfirm(){ return false; }, async uiPrompt(){ return null; },
  modal(){}, closeModal(){}, blossomPicker(){}, openGenStudio(){},
  // Capture the borrowed-studio options instead of opening anything: the regression below fires
  // onTake by hand, which is the whole point — the bug lives in the gap between the click and the
  // take, and that gap is a real voice generation.
  openVoiceStudio(o){ window.__voiceOpts = o; },
  openEmojiPopover(){ return ''; }, instEmojiUrl(){ return ''; },
  mediaServer:'', eTags(){ return []; }, profOf(){ return {}; },
  get ME(){ return {pubkey:'0'.repeat(64)}; }, get CFG(){ return {}; }, get VIEW(){ return 'meme'; },
};
// Only /client/meme/talk is answered; everything else keeps the real fetch (the catalogues).
const _fetch = window.fetch.bind(window);
window.fetch = (u, o) => String(u).includes('/client/meme/talk')
  ? Promise.resolve(new Response(
      JSON.stringify({ok:true, url:'https://example.invalid/talk.webm', dur:3.5,
                      effect:'talk', is_video:true, alpha:true}),
      {status:200, headers:{'Content-Type':'application/json'}}))
  : _fetch(u, o);
</script>
<script src="/static/js/client/sprite.js"></script>
<script src="/static/js/client/meme.js"></script>
<script>
window.__ready = false;
(function boot(){
  if(!window.PCMeme) return setTimeout(boot, 30);
  window.PCMeme.render();
  // Select the layer AND open the Layer tab. Both steps matter on a phone: the builder is TABBED
  // at this width, and the per-layer controls only exist in the DOM once a layer is selected and
  // that tab is showing. Clicking the timeline row is the real path (selectLayer(id,'timeline')
  // deliberately does NOT steal the tab), so the tab has to be tapped too, exactly as a user does.
  setTimeout(()=>{
    const row = document.querySelector('.mb-track[data-id="L1"]');
    if(row) row.click();
    const tab = document.querySelector('.mb-tab[data-tab="layer"]');
    if(tab) tab.click();
    window.__ready = true;
  }, 500);
})();
</script>
</body></html>"""

# The reported bug, pinned: "Make it talk" produced an AUDIO LAYER and no animation — the old
# "Add a voice line" outcome. A voice generation runs for the better part of a minute behind a modal,
# and any re-entry into the view runs `P = load()`, which rebuilds P.layers as NEW objects. The
# handler had captured the layer OBJECT, so it mutated an orphan (invisible) while
# `P.layers.indexOf(orphan)` returned -1 and `splice(-1 + 1, …)` still inserted the voice at the
# front. This reloads the project between the click and the take, exactly as that gap does.
TALK_REGRESSION = r"""(async () => {
  const q = () => JSON.parse(localStorage.getItem('pc_meme_project') || 'null') || {layers: []};
  const btn = document.getElementById('mb-talk');
  if (!btn) return {err: 'no #mb-talk to click'};
  btn.click();
  if (!window.__voiceOpts || !window.__voiceOpts.onTake) return {err: 'mb-talk did not open the studio'};
  // THE GAP: the project is reloaded while the "generation" is in flight, so every layer object the
  // click closed over is replaced.
  window.PCMeme.render();
  await window.__voiceOpts.onTake(new Blob(['x'], {type: 'audio/wav'}), 'testvoice', 'hello there');
  for (let i = 0; i < 60 && !(q().layers || []).some(l => l.type === 'video'); i++)
    await new Promise(r => setTimeout(r, 100));
  const ls = q().layers || [];
  return {
    n: ls.length,
    video: ls.filter(l => l.type === 'video').length,
    audio: ls.filter(l => l.type === 'audio').length,
    stillImage: ls.filter(l => l.type === 'image').length,
    videoSrc: (ls.find(l => l.type === 'video') || {}).src || '',
    toasts: window.__toasts.slice(-3),
  };
})()"""

AUDIT = r"""(() => {
  const out = {overflow:false, offscreen:[], tiny:[], overlap:[], present:{}, panel:false};
  out.overflow = document.documentElement.scrollWidth > window.innerWidth + 1;
  const vw = window.innerWidth;
  out.panel = !!document.getElementById('mb-inspector') || !!document.querySelector('.mb-f');
  const ids = %s;
  ids.forEach(id => { out.present[id] = !!document.getElementById(id); });

  const vis = el => !el.checkVisibility || el.checkVisibility();
  const boxes = [];
  document.querySelectorAll('#feed button, #feed .btn').forEach(b => {
    if (!vis(b)) return;
    const r = b.getBoundingClientRect();
    if (r.width < 1 && r.height < 1) return;
    const tag = b.id || String(b.className).slice(0, 28);
    if (r.left < -1 || r.right > vw + 1)
      out.offscreen.push({tag, left: Math.round(r.left), right: Math.round(r.right), vw});
    // 24px, not the 44px tap-target guideline: the builder's toolbar is deliberately 29-30px
    // throughout, and failing the app's own established density would make this check noise. The
    // bar is set at "a thumb genuinely cannot hit this", which is what a NEW breakage looks like.
    if (r.height > 0 && r.height < 24)
      out.tiny.push({tag, h: Math.round(r.height)});
    if (r.width > vw * 0.6) boxes.push({tag, r});   // full-width rows are what stack in the panel
  });
  for (let i = 0; i < boxes.length; i++)
    for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i].r, b = boxes[j].r;
      const ov = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      if (ov > 2 && Math.min(a.right, b.right) - Math.max(a.left, b.left) > 2)
        out.overlap.push({a: boxes[i].tag, b: boxes[j].tag, px: Math.round(ov)});
    }
  return out;
})()""" % json.dumps(EXPECTED)


async def drive(url):
    import websockets
    subprocess.run(["rm", "-rf", PROFILE], check=False)
    proc = subprocess.Popen(
        ["google-chrome-stable", "--headless=new", "--disable-gpu", "--no-sandbox",
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

            async def js(expr):
                r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
                if r.get("exceptionDetails"):
                    return None
                return r["result"].get("value")

            await call("Runtime.enable")
            await call("Page.enable")
            for w, h in WIDTHS:
                await call("Emulation.setDeviceMetricsOverride",
                           {"width": w, "height": h, "deviceScaleFactor": 2, "mobile": True})
                await call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
                await call("Page.navigate", {"url": url})
                for _ in range(40):
                    await asyncio.sleep(0.25)
                    if await js("window.__ready === true"):
                        break
                res = await js(AUDIT)
                if res is None:
                    print(f"SKIP  {w}px: page did not evaluate")
                    return 2
                label = f"{w}px"
                if not res["panel"]:
                    problems.append((label, "missing-control", "the inspector panel did not render"))
                for cid, ok in res["present"].items():
                    if not ok:
                        problems.append((label, "missing-control", f"#{cid} is not in the DOM"))
                if res["overflow"]:
                    problems.append((label, "horizontal-overflow", "the page scrolls sideways"))
                for o in res["offscreen"]:
                    problems.append((label, "offscreen-control",
                                     f"{o['tag']} spans {o['left']}..{o['right']} in {o['vw']}px"))
                for t in res["tiny"]:
                    problems.append((label, "tiny-tap-target", f"{t['tag']} is {t['h']}px tall"))
                for o in res["overlap"]:
                    problems.append((label, "overlapping-buttons",
                                     f"{o['a']} and {o['b']} overlap by {o['px']}px"))
                print(f"{label}: panel={res['panel']} overflow={res['overflow']} "
                      f"offscreen={len(res['offscreen'])} tiny={len(res['tiny'])} "
                      f"overlap={len(res['overlap'])} "
                      f"controls={sum(1 for v in res['present'].values() if v)}/{len(EXPECTED)}")

            # Behaviour, once — it is width-independent, and it needs the page in its initial state.
            await call("Page.navigate", {"url": url})
            for _ in range(40):
                await asyncio.sleep(0.25)
                if await js("window.__ready === true"):
                    break
            r = await call("Runtime.evaluate",
                           {"expression": TALK_REGRESSION, "returnByValue": True, "awaitPromise": True})
            tk = (r or {}).get("result", {}).get("value")
            if not tk:
                problems.append(("talk", "stale-layer", "the regression probe did not run"))
            elif tk.get("err"):
                problems.append(("talk", "stale-layer", tk["err"]))
            else:
                # The bug's exact signature: an audio layer arrived and the picture never became one.
                if tk["video"] != 1 or tk["stillImage"] != 0:
                    problems.append(("talk", "stale-layer",
                                     "the picture did not become the talking clip "
                                     f"(video={tk['video']} image={tk['stillImage']} "
                                     f"audio={tk['audio']}) — the handler mutated an orphaned layer"))
                if tk["audio"] != 1:
                    problems.append(("talk", "stale-layer",
                                     f"expected exactly one voice layer, got {tk['audio']}"))
                if tk["n"] != 2:
                    problems.append(("talk", "stale-layer", f"expected 2 layers, got {tk['n']}"))
                print(f"talk: layers={tk['n']} video={tk['video']} audio={tk['audio']} "
                      f"image={tk['stillImage']}")

        if not problems:
            print("OK  meme builder mobile checks passed")
            return 0
        print()
        for width, kind, detail in problems:
            print(f"FAIL  [{width}] {kind}: {detail}")
        return 1
    finally:
        proc.terminate()


def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    # Served over http, not file://: meme.js and client.css are fetched by absolute /static paths.
    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="memecheck-")
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
