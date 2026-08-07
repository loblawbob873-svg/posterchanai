#!/usr/bin/env python3
"""Does the Meme Builder's stage show what the RENDERER will produce?

    venv-unified/bin/python scripts/check_meme_render_match.py
    venv-unified/bin/python scripts/check_meme_render_match.py --project saved.json [--at 2.5]

The second form runs a REAL project (the JSON in localStorage under `pc_meme_project`) instead of
the built-in probes, which is how a report of "my render doesn't match my preview" stops being a
description and becomes pixel numbers. It has no colour probes to key on, so it reports the diff
image instead: where the two disagree, and by how much. Layer media is fetched from wherever the
project points, so it needs network for a project built against a live instance.

Every other check in this repo audits ONE side. check_meme_mobile.py audits the builder's controls;
tests/test_meme_builder.py audits the filtergraph. Nothing compares them, and "preview and render are
way off" is precisely a disagreement BETWEEN them — each side looks perfectly reasonable on its own.

So this renders the same project twice:

  * the STAGE, screenshotted out of a real browser running the shipped meme.js/client.css and
    scaled to the project's pixel size;
  * the EXPORT, from meme_builder_service.render(fmt='png') — the actual ffmpeg filtergraph.

…then measures the same features in both (the bounding box of a distinctly-coloured layer, and the
bounding box of the caption's ink) and reports the difference in CANVAS pixels. A mismatch here is
exactly what the user sees: "I put it there and it came out somewhere else."

Colour, not shape recognition: every probe — layer AND caption — is a flat, unique, fully-saturated
hue on a black canvas, so finding it in either image is a threshold rather than a guess.

Exit 0 = clean, 1 = the preview disagrees with the export (printed), 2 = could not run.
"""
import asyncio
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PORT = 9476
PROFILE = "/tmp/pc-meme-match-check"

PROJ_W, PROJ_H = 720, 1280
# Desktop and tablet, i.e. both `body{zoom}` tiers, plus a phone (where _fitStage owns the box).
WIDTHS = [(1920, 1080), (1280, 720), (390, 844)]

# Canvas pixels. The stage is a few hundred screen pixels wide, so one screen pixel is ~3 canvas
# pixels and anti-aliasing costs a couple more — but a real disagreement is tens of pixels.
TOL_BOX = 12
TOL_TEXT = 16

# One project, several PROBES, each a unique flat hue so it can be found in either image by a
# threshold instead of by shape. They are chosen to cover the ways the two sides can disagree:
#
#   RED    a plain `cover` layer            — the baseline: if this is out, everything is
#   GREEN  a `contain` layer in a box of a different shape — letterbox geometry
#
# NO ERASE-MASK PROBE. The preview deliberately does not paint the erase (see _MASK_IN_PREVIEW in
# meme.js): a CSS mask does not degrade, it makes the element vanish, so the stage shows a masked
# layer WHOLE while the export shows it cut out. That is a known, chosen difference, and a probe for
# it would fail every run — which is how a check stops being read. When the erase is previewed by
# baking it into the pixels instead, put the probe back.
#   BLUE   a rotated layer                  — rotw()/roth() growth vs the CSS transform
#   ORANGE a layer with an EFFECT on it     — the per-layer FX dropdown
#   YELLOW a one-line caption               — font, size and the ink-vs-ascent lift
#   CYAN   a caption long enough to WRAP    — per-line spacing, which the two sides state separately
#   PINK   a centred caption                — (w-text_w)/2 vs the CSS centring
#
# Captions are stroked in the BACKGROUND colour so the outline (which the two sides draw at
# different widths, and on different sides of the glyph edge) cannot move the measured ink box.
#
# Layer sources are HALF colour, half black — deliberately asymmetric. A flat rectangle is identical
# to its own mirror image, so with a symmetric source a layer that the export mirrors (or flips, or
# pans) and the stage does not measures the SAME box on both sides and the check passes on a picture
# that is visibly wrong.
# Fully-saturated primaries and secondaries, matched within TOL_COLOUR. Anti-aliasing blends a
# probe towards the BLACK background, and every such blend keeps the probe's zero channels at zero
# while dropping its full channels — so it can never wander into another probe's neighbourhood. A
# gentler palette did: a cyan glyph's fringe was being counted as part of the blue layer, which put
# that layer's box 300px out and looked exactly like the bug being hunted.
PROBES = {
    "RED":    ((255, 0, 0),     "layer"),
    "GREEN":  ((0, 255, 0),     "layer"),
    "BLUE":   ((0, 0, 255),     "layer"),
    "ORANGE": ((255, 128, 0),   "layer"),
    "YELLOW": ((255, 255, 0),   "text"),
    "CYAN":   ((0, 255, 255),   "text"),
    "PINK":   ((255, 0, 255),   "text"),
}
TOL_COLOUR = 46

# The playhead the MOTION scenario is sampled at. Deliberately not 0: zoom/spin/pulse/shake are all
# identity (or near it) on frame 0, so a check that only ever looked at t=0 would pass on a preview
# that never moves at all — which is precisely the bug the effect preview was written to fix.
MOTION_AT = 2.0

EDIT = {
    "w": PROJ_W, "h": PROJ_H, "fps": 12, "bg": "#000000", "duration": 4,
    "fmt": "png", "still": 0,
    "layers": [
        {"id": "RED", "type": "image", "src": "SRC:RED", "name": "cover",
         "start": 0, "dur": 4, "trim": 0, "x": 120, "y": 300, "w": 360, "h": 300,
         "fit": "cover", "opacity": 1, "effect": "none", "flipH": False, "flipV": False,
         "rotate": 0, "align": ""},
        {"id": "GREEN", "type": "image", "src": "SRC:GREEN", "name": "contain+erased",
         "start": 0, "dur": 4, "trim": 0, "x": 420, "y": 40, "w": 260, "h": 200,
         "fit": "contain", "opacity": 1, "effect": "none", "flipH": False, "flipV": False,
         "rotate": 0, "align": ""},
        {"id": "ORANGE", "type": "image", "src": "SRC:ORANGE", "name": "fx-mirror",
         "start": 0, "dur": 4, "trim": 0, "x": 430, "y": 300, "w": 250, "h": 190,
         "fit": "cover", "opacity": 1, "effect": "flip", "flipH": False, "flipV": False,
         "rotate": 0, "align": ""},
        {"id": "BLUE", "type": "image", "src": "SRC:BLUE", "name": "rot",
         "start": 0, "dur": 4, "trim": 0, "x": 60, "y": 1020, "w": 240, "h": 180,
         "fit": "cover", "opacity": 1, "effect": "none", "flipH": False, "flipV": False,
         "rotate": 20, "align": ""},
        {"id": "YELLOW", "type": "text", "src": "", "name": "one-line",
         "start": 0, "dur": 4, "x": 60, "y": 80, "text": "ALIGN", "size": 88,
         "color": "#ffff00", "stroke": "#000000", "align": "", "effect": "none"},
        {"id": "CYAN", "type": "text", "src": "", "name": "wrapped",
         "start": 0, "dur": 4, "x": 40, "y": 680,
         "text": "this caption is long enough that it has to wrap onto several lines",
         "size": 64, "color": "#00ffff", "stroke": "#000000", "align": "", "effect": "none"},
        {"id": "PINK", "type": "text", "src": "", "name": "centred",
         "start": 0, "dur": 4, "x": 0, "y": 900, "text": "CENTRED", "size": 72,
         "color": "#ff00ff", "stroke": "#000000", "align": "center", "effect": "none"},
    ],
}

# Every effect whose geometry MOVES with time, one per probe, each in its own corner so a bug that
# drags a layer sideways cannot hide inside a neighbour's box. `spin` and `zoom` are also the two the
# renderer crops back to the layer box, so these double as the check that the preview clips.
EDIT_MOTION = {
    "w": PROJ_W, "h": PROJ_H, "fps": 12, "bg": "#000000", "duration": 6,
    "fmt": "png", "still": MOTION_AT,
    "layers": [
        {"id": "RED", "type": "image", "src": "SRC:RED", "name": "fx-zoom",
         "start": 0, "dur": 6, "trim": 0, "x": 40, "y": 60, "w": 300, "h": 240,
         "fit": "cover", "opacity": 1, "effect": "zoom", "flipH": False, "flipV": False,
         "rotate": 0, "align": ""},
        {"id": "GREEN", "type": "image", "src": "SRC:GREEN", "name": "fx-spin",
         "start": 0, "dur": 6, "trim": 0, "x": 390, "y": 60, "w": 290, "h": 240,
         "fit": "cover", "opacity": 1, "effect": "spin", "flipH": False, "flipV": False,
         "rotate": 0, "align": ""},
        {"id": "BLUE", "type": "image", "src": "SRC:BLUE", "name": "fx-pulse",
         "start": 0, "dur": 6, "trim": 0, "x": 40, "y": 420, "w": 300, "h": 240,
         "fit": "cover", "opacity": 1, "effect": "pulse", "flipH": False, "flipV": False,
         "rotate": 0, "align": ""},
        {"id": "ORANGE", "type": "image", "src": "SRC:ORANGE", "name": "fx-shake",
         "start": 0, "dur": 6, "trim": 0, "x": 390, "y": 420, "w": 290, "h": 240,
         "fit": "cover", "opacity": 1, "effect": "shake", "flipH": False, "flipV": False,
         "rotate": 0, "align": ""},
    ],
}

# An ODD canvas, which is what "⇲ Canvas to this photo" produces from an ordinary photo — and a path
# nothing tested. ffmpeg forces even output dimensions (h264 requires them), so a 474x265 project
# renders 474x264 while the stage lays every layer out in a 265-tall space. Both the canvas AND each
# layer box are rounded down independently, so this checks that the drift stays sub-pixel rather than
# accumulating into a visible offset. Width is odd too (475), to catch the same on the other axis.
EDIT_ODD = {
    "w": 475, "h": 265, "fps": 12, "bg": "#000000", "duration": 4,
    "fmt": "png", "still": 0,
    "layers": [
        {"id": "RED", "type": "image", "src": "SRC:RED", "name": "full-canvas",
         "start": 0, "dur": 4, "trim": 0, "x": 0, "y": 0, "w": 475, "h": 265,
         "fit": "contain", "opacity": 1, "effect": "none", "flipH": False, "flipV": False,
         "rotate": 0, "align": ""},
        {"id": "BLUE", "type": "image", "src": "SRC:BLUE", "name": "odd-box",
         "start": 0, "dur": 4, "trim": 0, "x": 157, "y": 57, "w": 221, "h": 125,
         "fit": "cover", "opacity": 1, "effect": "none", "flipH": False, "flipV": False,
         "rotate": 0, "align": ""},
        {"id": "YELLOW", "type": "text", "src": "", "name": "cap",
         "start": 0, "dur": 4, "x": 31, "y": 25, "text": "ODD", "size": 41,
         "color": "#ffff00", "stroke": "#000000", "align": "", "effect": "none"},
    ],
}

SCENARIOS = [("static", EDIT, 0.0), ("motion", EDIT_MOTION, MOTION_AT), ("odd-canvas", EDIT_ODD, 0.0)]


def _page(src_base, edit, at, mask_url, rewrite=True):
    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/css/client.css">
<style>
  /* The stage must be screenshotted with NOTHING of the chrome inside its box. The builder draws no
     overlay on it, but the selection outline and the snap guides are one stray class away from
     landing in the comparison, so they are neutralised explicitly. */
  .mb-item.sel{outline:none !important}
  .mb-h,.mb-guide{display:none !important}
  .mb-stage{border-color:transparent !important}
</style>
</head><body>
<div id="modal-root"></div>
<!-- The REAL shell (templates/client.html): .main is `height:100vh;display:flex` and .feed is
     `flex:1;min-height:0`. On a phone the builder is `height:100%` and _fitStage measures the box it
     is left with, so a bare <div id="feed"> gives the stage no height at all and there is nothing to
     screenshot. The chrome around it is irrelevant — the box it hands the builder is not. -->
<main class="main">
  <header class="topbar glass"><h2 id="view-title">Meme</h2></header>
  <div id="feed" class="feed"></div>
</main>
<script>
localStorage.setItem('pc_meme_project', JSON.stringify(__PROJECT__));
// Capture the render payload instead of posting it. Rebuilding that object here would be a SECOND
// copy of the client's payload builder, and a copy that drifted is precisely how a preview and an
// export come apart — the caption's WRAPPING, for one, is computed in the payload and nowhere else,
// so a harness that made up its own edit list would compare the stage against a render nobody can
// actually ask for. This is the real button, the real payload, one fetch short of the real server.
window.__renderEdit = null;
const _fetch = window.fetch.bind(window);
window.fetch = (u, o) => {
  if (!String(u).includes('/client/meme/render')) return _fetch(u, o);
  try { window.__renderEdit = JSON.parse((o || {}).body || 'null').edit; } catch (_) { }
  // A never-settling promise would leave the button stuck; an error is caught and shown, which is
  // harmless here and keeps the builder in a sane state for the screenshot.
  return Promise.resolve(new Response('{"detail":"captured"}',
    {status: 503, headers: {'Content-Type': 'application/json'}}));
};
window.__PC = {
  toast(){}, async uploadBlob(){ return ''; }, async selfProof(){ return 'p'; },
  async uiConfirm(){ return false; }, async uiPrompt(){ return null; },
  modal(){}, closeModal(){}, blossomPicker(){}, openGenStudio(){}, openVoiceStudio(){},
  openEmojiPopover(){ return ''; }, instEmojiUrl(){ return ''; },
  mediaServer:'', eTags(){ return []; }, profOf(){ return {}; },
  get ME(){ return {pubkey:'0'.repeat(64)}; }, get CFG(){ return {}; }, get VIEW(){ return 'meme'; },
};
</script>
<script src="/static/js/client/sprite.js"></script>
<script src="/static/js/client/meme.js"></script>
<script>
window.__ready = false;
(function boot(){
  if(!window.PCMeme) return setTimeout(boot, 30);
  window.PCMeme.render();
  // Wait for the layer's own picture AND the caption's webfont: a screenshot taken before either has
  // landed compares an empty stage (or a fallback face) against a finished export, which reads as a
  // huge misalignment and is really a race in the harness.
  const done = () => {
    // Park the playhead where the export will be sampled. Through the real scrub input, so this goes
    // down the same seek() path a user's drag does — and the Render button reads its value for
    // `still`, so BOTH sides end up at the same instant without the harness stating it twice.
    const sc = document.getElementById('mb-scrub');
    if (sc) { sc.value = String(__AT__); sc.dispatchEvent(new Event('input', {bubbles: true})); }
    setTimeout(() => { window.__ready = true; }, 300);
  };
  const imgs = [...document.querySelectorAll('.mb-stage img')];
  Promise.all([
    document.fonts ? document.fonts.ready : Promise.resolve(),
    ...imgs.map(im => im.complete ? Promise.resolve()
                                  : new Promise(r => { im.onload = r; im.onerror = r; })),
  ]).then(() => setTimeout(done, 500));
})();
</script>
</body></html>""".replace("__AT__", json.dumps(at)).replace("__PROJECT__", json.dumps(edit if not rewrite else dict(edit, name="match", layers=[
        dict(l, src=(src_base + l["id"] + ".png" if l.get("src") else ""),
             mask=(mask_url if l.get("mask") else ""),
             # The editor's own defaults for the fields the render payload does not carry.
             trim=l.get("trim", 0), opacity=l.get("opacity", 1), volume=1, mute=False,
             sound="", soundVolume=1, speed=1)
        for l in edit["layers"]], _healed=1)))


MEASURE_STAGE = r"""(() => {
  const st = document.getElementById('mb-stage');
  if(!st) return null;
  const r = st.getBoundingClientRect();
  const cs = getComputedStyle(st);
  const bl = parseFloat(cs.borderLeftWidth)||0, bt = parseFloat(cs.borderTopWidth)||0;
  const br = parseFloat(cs.borderRightWidth)||0, bb = parseFloat(cs.borderBottomWidth)||0;
  // The rect is in VISUAL pixels — `body{zoom}` already applied — which is the same space the
  // rendered screenshot is in. So the shot is taken WHOLE and cropped with these numbers, rather
  // than passing a clip: captureScreenshot's clip is interpreted in the page's own CSS pixels, and
  // under a body zoom the two spaces differ by exactly the factor that makes the crop land on
  // nothing (measured: an empty frame on both desktop tiers, a correct one at zoom 1 — i.e. the
  // harness would have "passed" on a phone and reported the desktop stage as blank).
  return {x:r.left+bl, y:r.top+bt, w:r.width-bl-br, h:r.height-bt-bb,
          zoom: parseFloat(getComputedStyle(document.body).zoom) || 1,
          dpr: window.devicePixelRatio || 1};
})()"""


def _near(rgb, want, tol=TOL_COLOUR):
    return all(abs(a - b) <= tol for a, b in zip(rgb[:3], want))


def _boxes(img):
    """Every probe's bounding box in one pass. One pass, not six: a 720x1280 image is a million
    pixels and this runs once per width plus once for the export."""
    px = img.load()
    w, h = img.size
    acc = {k: [10 ** 9, 10 ** 9, -1, -1] for k in PROBES}
    for y in range(h):
        for x in range(w):
            p = px[x, y]
            if p[0] < 40 and p[1] < 40 and p[2] < 40:
                continue                      # background, by far the commonest case
            for k, (want, _kind) in PROBES.items():
                if _near(p, want):
                    a = acc[k]
                    if x < a[0]:
                        a[0] = x
                    if y < a[1]:
                        a[1] = y
                    if x > a[2]:
                        a[2] = x
                    if y > a[3]:
                        a[3] = y
                    break
    return {k: (tuple(v) if v[2] >= 0 else None) for k, v in acc.items()}


async def drive(url, shots, payload):
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
                           {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": w < 821})
                await call("Page.navigate", {"url": url})
                ok = False
                for _ in range(60):
                    await asyncio.sleep(0.25)
                    if await js("window.__ready === true"):
                        ok = True
                        break
                if not ok:
                    print(f"SKIP  {w}px: the builder never became ready")
                    return 2
                box = await js(MEASURE_STAGE)
                if not box or box["w"] < 8:
                    print(f"SKIP  {w}px: the stage has no measurable box")
                    return 2
                # Re-shoot at a device scale that puts the stage at roughly the PROJECT's own
                # resolution. The stage is only a few hundred CSS pixels wide, so a dsf-1 shot has to
                # be upscaled ~3x to compare — and the interpolation invents intermediate colours all
                # along every edge, which is noise on exactly the boundaries being measured. Raising
                # dsf makes the browser RENDER more pixels instead of inventing them. Layout is
                # untouched by dsf, so the measured box is still valid; only its scale changes.
                dsf = max(1, min(6, round(PROJ_W / box["w"])))
                if dsf != 1:
                    await call("Emulation.setDeviceMetricsOverride",
                               {"width": w, "height": h, "deviceScaleFactor": dsf,
                                "mobile": w < 821})
                    await asyncio.sleep(0.5)
                # WHOLE viewport, cropped afterwards — see MEASURE_STAGE for why a clip cannot be
                # used here.
                r = await call("Page.captureScreenshot",
                               {"format": "png", "captureBeyondViewport": False})
                shots[f"{w}x{h}"] = (base64.b64decode(r["data"]), box, dsf)

            # The payload, from the real button. Done last and once — it is width-independent, and
            # the click leaves the builder showing an error pane that would spoil a screenshot.
            await js("document.getElementById('mb-render').click()")
            for _ in range(40):
                await asyncio.sleep(0.25)
                if await js("window.__renderEdit !== null"):
                    break
            payload["edit"] = await js("window.__renderEdit")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def _load_project(path, at):
    """A saved project -> the (edit, at) pair the harness drives. The project IS the edit list
    (render() builds its payload straight off it), so the only thing to force is the export format:
    a still is what can be compared against a screenshot."""
    with open(path, encoding="utf-8") as fh:
        P = json.load(fh)
    if not isinstance(P, dict) or not P.get("layers"):
        raise SystemExit(f"{path} is not a Meme Builder project (no `layers`)")
    P = dict(P, fmt="png", still=at)
    return P, at


def _diff_report(exp, got, label):
    """No colour probes in a real project, so report WHERE the two images disagree. A per-row/column
    profile of the differing pixels localises it to a layer far better than one number: a caption
    that is 40px low shows as a tight band, a layer that is scaled shows as two bands at its edges."""
    from PIL import ImageChops
    d = ImageChops.difference(exp.convert("RGB"), got.convert("RGB")).convert("L")
    px = d.load()
    w, h = d.size
    rows, cols, n = [0] * h, [0] * w, 0
    for y in range(h):
        for x in range(w):
            if px[x, y] > 40:            # well above JPEG-ish noise and anti-aliasing
                rows[y] += 1
                cols[x] += 1
                n += 1
    pct = 100.0 * n / (w * h)
    print(f"{label}: {n} differing pixels ({pct:.2f}% of the frame)")
    if not n:
        return 0.0, d
    bands = [(i, c) for i, c in enumerate(rows) if c]
    colb = [(i, c) for i, c in enumerate(cols) if c]
    print(f"    rows {bands[0][0]}..{bands[-1][0]}   cols {colb[0][0]}..{colb[-1][0]}")
    worst = sorted(bands, key=lambda t: -t[1])[:5]
    print("    heaviest rows: " + ", ".join(f"y={i} ({c}px)" for i, c in worst))
    return pct, d


def main():
    if "--project" in sys.argv:
        return _run_project(sys.argv[sys.argv.index("--project") + 1],
                            float(sys.argv[sys.argv.index("--at") + 1]) if "--at" in sys.argv else 0.0)
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    try:
        from PIL import Image
    except ImportError:
        print("SKIP  Pillow not installed")
        return 2
    from app.services import meme_builder_service, media_service
    if not media_service.resolve_ffmpeg():
        print("SKIP  no ffmpeg on this node")
        return 2

    import http.server
    import threading
    tmp = tempfile.mkdtemp(prefix="mememtch-")
    # One flat source per LAYER probe. Deliberately not square (400x300): a source whose shape
    # matches the layer box makes `contain` and `cover` produce the same picture, which is the one
    # thing the GREEN probe is there to tell apart.
    src_paths = {}
    for name, (rgb, kind) in PROBES.items():
        if kind != "layer":
            continue
        p = os.path.join(tmp, f"{name}.png")
        im = Image.new("RGB", (400, 300), (0, 0, 0))
        im.paste(Image.new("RGB", (200, 300), rgb), (0, 0))   # left half only — see PROBES
        im.save(p)
        src_paths[name] = p
    # The erase mask: opaque = KEEP, transparent = erased. Painted in the SOURCE's space (400x300),
    # erasing its bottom third — a horizontal cut, so a mask seated with the wrong aspect handling
    # shows up as the cut landing at the wrong height rather than as a subtle edge.
    mk = Image.new("RGBA", (400, 300), (255, 255, 255, 255))
    mk.paste(Image.new("RGBA", (400, 100), (0, 0, 0, 0)), (0, 200))
    mask_path = os.path.join(tmp, "mask.png")
    mk.save(mask_path)
    src_paths["mask"] = mask_path

    class H(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            path = path.split("?")[0].split("#")[0]
            if path.startswith("/static/"):
                return os.path.join(ROOT, path.lstrip("/"))
            # The caption's webfont IS the font ffmpeg draws with, and the comparison is meaningless
            # without it — a fallback face is a different width at the same size.
            if path == "/client/meme-font.ttf":
                from app.services.effects_service._common import _meme_font_path
                return _meme_font_path() or os.path.join(tmp, "missing.ttf")
            if path.startswith("/src/"):
                return os.path.join(tmp, os.path.basename(path))
            if path == "/mask.png":
                return mask_path
            return os.path.join(tmp, path.lstrip("/") or "index.html")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    problems, rc = [], 0
    try:
        for sname, sedit, at in SCENARIOS:
            with open(os.path.join(tmp, "index.html"), "w") as fh:
                fh.write(_page(f"{base}/src/", sedit, at, f"{base}/mask.png"))
            shots, payload = {}, {}
            rc = asyncio.run(drive(f"{base}/index.html", shots, payload))
            if rc:
                return rc
            edit = payload.get("edit")
            if not edit or not edit.get("layers"):
                print(f"SKIP  [{sname}] the Render button produced no payload")
                return 2

            # The EXPORT, from the real filtergraph, driven by the payload the client just built.
            sources = {}
            for l in edit["layers"]:
                u = l.get("src") or ""
                if u:
                    sources[u] = src_paths[os.path.basename(u).rsplit(".", 1)[0]]
                mu = l.get("mask") or ""
                if mu:
                    sources[mu] = src_paths["mask"]
            png, ctype = meme_builder_service.render(edit, sources)
            if ctype != "image/png":
                print(f"SKIP  [{sname}] the renderer returned {ctype}")
                return 2
            exp = Image.open(io.BytesIO(png)).convert("RGB")
            # PC_MEME_DUMP=<dir> writes both sides out. A bounding box says THAT two shapes differ;
            # when they differ in area rather than position, only the pictures say why.
            if os.environ.get("PC_MEME_DUMP"):
                exp.save(os.path.join(os.environ["PC_MEME_DUMP"], f"{sname}-export.png"))
            want = _boxes(exp)
            live = {l["id"] for l in sedit["layers"]}
            missing = [k for k, v in want.items() if not v and k in live]
            if missing:
                print(f"SKIP  [{sname}] the export is missing probes: {', '.join(missing)}")
                return 2
            keys = [k for k in PROBES if k in live]
            print(f"[{sname} @{at}s] export    " + "  ".join(f"{k}={want[k]}" for k in keys))

            for label, (data, box, dsf) in sorted(shots.items()):
                img = Image.open(io.BytesIO(data)).convert("RGB")
                img = img.crop((int(round(box["x"] * dsf)), int(round(box["y"] * dsf)),
                                int(round((box["x"] + box["w"]) * dsf)),
                                int(round((box["y"] + box["h"]) * dsf))))
                # The EXPORT's size, not the project's: ffmpeg forces even dimensions (h264 requires
                # them), so a 474x265 project renders 474x264. Resizing the stage to the project size
                # instead would introduce a one-pixel scale error of the harness's own making and then
                # report it as a finding.
                if img.size != exp.size:
                    img = img.resize(exp.size, Image.LANCZOS)
                if os.environ.get("PC_MEME_DUMP"):
                    img.save(os.path.join(os.environ["PC_MEME_DUMP"], f"{sname}-stage-{label}.png"))
                got = _boxes(img)
                print(f"[{sname} @{at}s] {label:<9} " + "  ".join(f"{k}={got[k]}" for k in keys))
                for k in keys:
                    tol = TOL_BOX if PROBES[k][1] == "layer" else TOL_TEXT
                    g, wn = got[k], want[k]
                    if not g:
                        problems.append((sname, label, k, "the stage does not show it at all"))
                        continue
                    d = [a - b for a, b in zip(g, wn)]
                    if max(abs(v) for v in d) > tol:
                        problems.append((sname, label, k,
                                         f"stage {g} vs export {wn} — left/top/right/bottom out by "
                                         f"{d[0]:+d}/{d[1]:+d}/{d[2]:+d}/{d[3]:+d} canvas px"))
    finally:
        srv.shutdown()

    if problems:
        print("\nPREVIEW DISAGREES WITH THE EXPORT:")
        for sname, label, k, msg in problems:
            print(f"  [{sname}/{label}] {k} ({PROBES[k][1]}): {msg}")
        return 1
    print("\nOK — the stage matches the export in every scenario, at every width")
    return 0


def _run_project(path, at):
    """Drive a REAL saved project: screenshot its stage, render the payload its own Render button
    builds, and diff the two."""
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2
    try:
        from PIL import Image
    except ImportError:
        print("SKIP  Pillow not installed")
        return 2
    from app.services import meme_builder_service, media_service
    if not media_service.resolve_ffmpeg():
        print("SKIP  no ffmpeg on this node")
        return 2

    import http.server
    import threading
    import urllib.parse
    proj, at = _load_project(path, at)
    W, H = int(proj.get("w") or 720), int(proj.get("h") or 1280)
    print(f"project: canvas {W}x{H}, {len(proj['layers'])} layers, still at {at}s")

    tmp = tempfile.mkdtemp(prefix="memeproj-")

    class H_(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, p2):
            p2 = p2.split("?")[0].split("#")[0]
            if p2.startswith("/static/"):
                return os.path.join(ROOT, p2.lstrip("/"))
            if p2 == "/client/meme-font.ttf":
                from app.services.effects_service._common import _meme_font_path
                return _meme_font_path() or os.path.join(tmp, "missing.ttf")
            return os.path.join(tmp, p2.lstrip("/") or "index.html")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H_)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    # rewrite=False: _page remaps src/mask onto the harness's own files for the synthetic probes, and
    # a real project's URLs are already absolute and must reach the browser untouched.
    page = _page("", proj, at, "", rewrite=False)
    with open(os.path.join(tmp, "index.html"), "w") as fh:
        fh.write(page)

    shots, payload = {}, {}
    try:
        rc = asyncio.run(drive(f"{base}/index.html", shots, payload))
    finally:
        srv.shutdown()
    if rc:
        return rc
    edit = payload.get("edit")
    if not edit:
        print("SKIP  the Render button produced no payload")
        return 2

    # Fetch every layer source the way the SERVER would (the client already has them; the renderer
    # takes local paths). Anything unreachable is reported rather than silently rendered as a gap.
    import urllib.request as _u
    sources, failed = {}, []
    for l in edit.get("layers") or []:
        for key in ("src", "mask"):
            u = l.get(key) or ""
            if not u or u in sources:
                continue
            try:
                with _u.urlopen(u, timeout=30) as r:
                    data = r.read()
                ext = os.path.splitext(urllib.parse.urlparse(u).path)[1][:6] or ".bin"
                fp = os.path.join(tmp, f"s{len(sources)}{ext}")
                open(fp, "wb").write(data)
                sources[u] = fp
            except Exception as e:
                failed.append(f"{key}={u} ({e})")
    if failed:
        print("COULD NOT FETCH (these layers will be missing from the export, not from the stage):")
        for f in failed:
            print("   " + f)

    png, ctype = meme_builder_service.render(dict(edit, fmt="png", still=at), sources)
    if ctype != "image/png":
        print(f"SKIP  the renderer returned {ctype}")
        return 2
    exp = Image.open(io.BytesIO(png)).convert("RGB")
    out = os.environ.get("PC_MEME_DUMP") or tmp
    exp.save(os.path.join(out, "project-export.png"))

    worst = 0.0
    for label, (data, box, dsf) in sorted(shots.items()):
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img = img.crop((int(round(box["x"] * dsf)), int(round(box["y"] * dsf)),
                        int(round((box["x"] + box["w"]) * dsf)),
                        int(round((box["y"] + box["h"]) * dsf))))
        if img.size != exp.size:
            img = img.resize(exp.size, Image.LANCZOS)
        img.save(os.path.join(out, f"project-stage-{label}.png"))
        pct, d = _diff_report(exp, img, f"[{label}]")
        d.save(os.path.join(out, f"project-diff-{label}.png"))
        worst = max(worst, pct)
    print(f"\nimages written to {out}")
    # A real project has anti-aliasing, video frame timing and font hinting between the two sides, so
    # a few percent is normal. A misplaced LAYER is tens of percent and lands in a band.
    return 1 if worst > 8.0 else 0


if __name__ == "__main__":
    sys.exit(main())
