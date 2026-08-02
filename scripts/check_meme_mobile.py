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
  mouth-misplaced      the "where is the mouth?" marker does not land where you put it.

The last one runs at TABLET width on purpose. Between 821px and 1920px the app is scaled with
`body{zoom}`, and that is a second coordinate system: getBoundingClientRect() reports viewport
pixels (zoom already applied) while a px written to `style.left` is a layout pixel. A phone is at
zoom 1, so a control that mixes the two is exactly right at every width this file used to check
and wrong on a tablet only.

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
# Phone (zoom 1) and tablet (`body{zoom:.67}` — the 821-1366px tier in client.css). See the
# mouth-misplaced note in the docstring: only the pair proves anything.
MOUTH_WIDTHS = [(390, 844), (1024, 768)]
PORT = 9473
PROFILE = "/tmp/pc-meme-mobile-check"

# Per-layer controls the inspector must offer for a selected IMAGE layer. Named, because "it renders"
# is not the check — a button that silently stopped being emitted still renders a panel.
EXPECTED = ["mb-nobg", "mb-talk", "mb-fit", "mb-fill", "mb-split", "mb-cutall", "mb-erase"]

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
  // A real-enough modal: the builder's own dialogs (the mouth placement control) have to be
  // mountable, and a no-op stub silently rendered nothing while every assertion still "passed".
  modal(html, onMount){
    const bg = document.createElement('div'); bg.className = 'modal-bg';
    bg.innerHTML = '<div class="modal glass neon-border">' + html + '</div>';
    document.getElementById('modal-root').appendChild(bg);
    const box = bg.querySelector('.modal'); if (onMount) onMount(box);
  },
  closeModal(){ document.getElementById('modal-root').innerHTML = ''; },
  blossomPicker(){}, openGenStudio(){},
  // Capture the borrowed-studio options instead of opening anything: the regression below fires
  // onTake by hand, which is the whole point — the bug lives in the gap between the click and the
  // take, and that gap is a real voice generation.
  openVoiceStudio(o){ window.__voiceOpts = o; },
  openEmojiPopover(){ return ''; }, instEmojiUrl(){ return ''; },
  mediaServer:'', eTags(){ return []; }, profOf(){ return {}; },
  get ME(){ return {pubkey:'0'.repeat(64)}; }, get CFG(){ return {}; }, get VIEW(){ return 'meme'; },
};
// Only /client/meme/talk is answered; everything else keeps the real fetch (the catalogues, and
// the pose ARTWORK the mouth picker shows — that one has to be a real request, because "the
// picture loaded" is half of what the pose check is asserting). The request BODY is kept: what a
// dragged marker is worth is decided by whether it reaches the render.
const _fetch = window.fetch.bind(window);
window.__talkBody = null;
window.fetch = (u, o) => {
  if (!String(u).includes('/client/meme/talk')) return _fetch(u, o);
  try { window.__talkBody = JSON.parse((o || {}).body || 'null'); } catch (_) { }
  return Promise.resolve(new Response(
    JSON.stringify({ok:true, url:'https://example.invalid/talk.webm', dur:3.5,
                    effect:'talk', is_video:true, alpha:true}),
    {status:200, headers:{'Content-Type':'application/json'}}));
};
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
  // The mouth PLACEMENT control comes first now — deliberately before the voice, so getting the
  // marker wrong costs a drag rather than another minute of GPU. Accept its default and go on.
  for (let i = 0; i < 40 && !document.getElementById('mm-go'); i++)
    await new Promise(r => setTimeout(r, 50));
  const go = document.getElementById('mm-go');
  if (!go) return {err: 'mb-talk did not open the mouth picker'};
  go.click();
  for (let i = 0; i < 40 && !window.__voiceOpts; i++) await new Promise(r => setTimeout(r, 50));
  if (!window.__voiceOpts || !window.__voiceOpts.onTake) return {err: 'the picker did not open the studio'};
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

# A CHARACTER POSE layer (the `carl` alpha effect, on the timeline as its rendered clip) must reach
# the SAME placement control. It briefly did not — "the mouth selector never shows" — and the two
# ways that path breaks are both invisible from the outside: the picker showing the layer's own src
# (a webm, which an <img> renders as nothing, leaving a dialog you cannot use), and the placement
# being collected and then dropped from the request, which silently falls back to auto-detect.
POSE_TALK = r"""(async () => {
  const P = JSON.parse(localStorage.getItem('pc_meme_project'));
  P.layers.push({id:'L2', type:'video', src:'https://example.invalid/carl.webm', name:'Carl',
                 fxPose:'carl', start:0, dur:6, trim:0, x:0, y:0, w:720, h:1280,
                 opacity:1, effect:'none', volume:1, mute:false, flipH:false, flipV:false,
                 rotate:0, sound:'', soundVolume:1, text:'', size:64,
                 color:'#ffffff', stroke:'#000000', align:''});
  localStorage.setItem('pc_meme_project', JSON.stringify(P));
  window.PCMeme.render();
  await new Promise(r => setTimeout(r, 200));
  const row = document.querySelector('.mb-track[data-id="L2"]'); if (row) row.click();
  const tab = document.querySelector('.mb-tab[data-tab="layer"]'); if (tab) tab.click();
  await new Promise(r => setTimeout(r, 200));
  const btn = document.getElementById('mb-talk');
  if (!btn) return {err: 'a character pose offers no #mb-talk'};
  btn.click();
  for (let i = 0; i < 40 && !document.getElementById('mm-go'); i++)
    await new Promise(r => setTimeout(r, 50));
  if (!document.getElementById('mm-go')) return {err: 'a character pose never opened the mouth picker'};
  const img = document.getElementById('mm-img');
  for (let i = 0; i < 60 && img && !img.complete; i++) await new Promise(r => setTimeout(r, 50));
  const shown = img ? img.getAttribute('src') : '';
  // Drag the marker somewhere unmistakable, so "it arrived" cannot be confused with the default.
  const w = document.getElementById('mm-wrap'), r = img.getBoundingClientRect();
  w.dispatchEvent(new PointerEvent('pointerdown', {bubbles:true, clientX:r.left + r.width*0.25,
                                                   clientY:r.top + r.height*0.75}));
  w.dispatchEvent(new PointerEvent('pointerup', {bubbles:true}));
  document.getElementById('mm-go').click();
  for (let i = 0; i < 40 && !window.__voiceOpts; i++) await new Promise(r => setTimeout(r, 50));
  if (!window.__voiceOpts || !window.__voiceOpts.onTake) return {err: 'the picker did not open the studio'};
  await window.__voiceOpts.onTake(new Blob(['x'], {type:'audio/wav'}), 'testvoice', 'hello there');
  for (let i = 0; i < 60 && !window.__talkBody; i++) await new Promise(r => setTimeout(r, 100));
  const b = window.__talkBody || {};
  return {shown, loaded: !!(img && img.naturalWidth > 0),
          character: b.character || '', mouth: b.mouth || null};
})()"""

# The marker in "Where is the mouth?" must land where you put it, at EVERY width. Drag it to a known
# fraction of the picture, then read back both halves of the control: where the pin is PAINTED (what
# you aim with) and what actually reaches the render (what the mouth is warped from). They were
# painted from the client rect and dragged from the client rect, which agree at zoom 1 and do not
# agree under the desktop/tablet `body{zoom}` tiers — so on a tablet the pin crawled to 0.67 of the
# picture and stopped: the right and bottom of the image were unreachable, and a mouth lined up by
# eye was sent ~1.5x too far across and 1.5x too wide. That is "it doesn't align on anime": a photo
# is seeded by the detector and never dragged, so only hand placement showed it.
MOUTH_PLACEMENT = r"""(async () => {
  const TX = 0.85, TY = 0.88, TW = 0.30;
  const btn = document.getElementById('mb-talk');
  if (!btn) return {err: 'no #mb-talk to click'};
  btn.click();
  for (let i = 0; i < 40 && !document.getElementById('mm-go'); i++)
    await new Promise(r => setTimeout(r, 50));
  const img = document.getElementById('mm-img'), wrap = document.getElementById('mm-wrap');
  const pin = document.getElementById('mm-pin');
  if (!img || !wrap || !pin) return {err: 'the mouth picker did not open'};
  for (let i = 0; i < 60 && !(img.complete && img.naturalWidth); i++)
    await new Promise(r => setTimeout(r, 50));
  if (!img.naturalWidth) return {err: 'the picture never loaded, so there is nothing to aim at'};
  const r = img.getBoundingClientRect();
  wrap.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true,
    clientX: r.left + TX * r.width, clientY: r.top + TY * r.height}));
  wrap.dispatchEvent(new PointerEvent('pointerup', {bubbles: true}));
  const rng = document.getElementById('mm-w');
  rng.value = Math.round(TW * 100); rng.dispatchEvent(new Event('input'));
  const p = pin.getBoundingClientRect();
  const shown = {x: (p.left + p.width / 2 - r.left) / r.width,
                 y: (p.top + p.height / 2 - r.top) / r.height,
                 w: p.width / r.width};
  document.getElementById('mm-go').click();
  for (let i = 0; i < 40 && !window.__voiceOpts; i++) await new Promise(r => setTimeout(r, 50));
  if (!window.__voiceOpts || !window.__voiceOpts.onTake) return {err: 'the picker did not open the studio'};
  await window.__voiceOpts.onTake(new Blob(['x'], {type: 'audio/wav'}), 'testvoice', 'hello there');
  for (let i = 0; i < 60 && !window.__talkBody; i++) await new Promise(r => setTimeout(r, 100));
  return {zoom: getComputedStyle(document.body).zoom || '1', want: {x: TX, y: TY, w: TW},
          shown, sent: (window.__talkBody || {}).mouth || null};
})()"""

# ✂ CUT (split at the playhead). The arithmetic is the whole feature and none of it is visible on a
# screenshot: the second half has to start where the first now ends, the two together have to still fill
# exactly the span the original filled (so no other clip moves), and — the part that is easy to get wrong
# — its IN-POINT has to be converted through the clip's SPEED, because the source is walked at that rate
# (a 2x clip cut one second in resumes two seconds into the footage). A wrong conversion looks fine on the
# timeline and plays the wrong frames.
#
# Also pinned here: the join becomes a HARD cut (the crossfade ramps belong to the outer edges, where the
# neighbours still are), a one-shot sound does not come along to fire a second time at the cut, and the
# minimum-piece guard refuses a cut that would leave a sliver instead of making one.
CUT_SPLIT = r"""(async () => {
  const q = () => (JSON.parse(localStorage.getItem('pc_meme_project') || 'null') || {layers: []}).layers;
  const P = JSON.parse(localStorage.getItem('pc_meme_project'));
  P.xfade = 0.5;
  P.layers = [{id:'V1', type:'video', src:'https://example.invalid/a.webm', name:'take',
               start:2, dur:4, trim:1.5, speed:2, sound:'boing', soundVolume:1,
               xin:0.5, xout:0.5, x:0, y:0, w:720, h:1280, opacity:1, effect:'none',
               volume:1, mute:false, flipH:false, flipV:false, rotate:0,
               text:'', size:64, color:'#ffffff', stroke:'#000000', align:''},
              {id:'T1', type:'text', src:'', name:'', start:2, dur:4, trim:0,
               x:40, y:40, w:0, h:0, opacity:1, effect:'none', volume:1, mute:false,
               flipH:false, flipV:false, rotate:0, text:'caption', size:64,
               color:'#ffffff', stroke:'#000000', align:''}];
  localStorage.setItem('pc_meme_project', JSON.stringify(P));
  window.PCMeme.render();
  await new Promise(r => setTimeout(r, 200));
  const endBefore = Math.max(...q().map(l => l.start + l.dur));
  const seek = (t) => { const s = document.getElementById('mb-scrub');
                        s.value = String(t); s.dispatchEvent(new Event('input')); };
  const pick = async (id) => {
    const row = document.querySelector('.mb-track[data-id="' + id + '"]'); if (row) row.click();
    const tab = document.querySelector('.mb-tab[data-tab="layer"]'); if (tab) tab.click();
    await new Promise(r => setTimeout(r, 150));
  };

  // ---- the guard: a cut 0.02s in would leave a sliver, so it must be refused, not made.
  await pick('V1');
  seek(2.02);
  const guardBtn = document.getElementById('mb-split');
  if (!guardBtn) return {err: 'no #mb-split in the layer panel'};
  guardBtn.click();
  await new Promise(r => setTimeout(r, 150));
  const guardKept = q().length;

  // ---- the cut itself, one second into a 2x clip.
  await pick('V1');
  seek(3);
  document.getElementById('mb-split').click();
  await new Promise(r => setTimeout(r, 250));
  const ls = q();
  const a = ls.find(l => l.id === 'V1');
  const b = ls.find(l => l.type === 'video' && l.id !== 'V1');
  if (!a || !b) return {err: 'the cut did not produce two halves (' + ls.length + ' layers)'};
  const selId = (document.querySelector('.mb-track.sel') || {dataset: {}}).dataset.id || '';

  // ---- ✂ Cut here: everything standing under the playhead, in one step. At 4.5s that is the second
  // half of the clip and the caption — the FIRST half has already ended, so it must be left alone.
  const beforeAll = q().length;
  seek(4.5);
  document.getElementById('mb-cutall').click();
  await new Promise(r => setTimeout(r, 250));
  const after = q();
  return {
    guardKept, n: ls.length,
    a: {start: a.start, dur: a.dur, trim: a.trim, xin: a.xin, xout: a.xout, sound: a.sound},
    b: {start: b.start, dur: b.dur, trim: b.trim, xin: b.xin, xout: b.xout, sound: b.sound},
    endBefore, endAfter: Math.max(...ls.map(l => l.start + l.dur)),
    selIsSecondHalf: selId === b.id,
    cutAllAdded: after.length - beforeAll,
    endAfterAll: Math.max(...after.map(l => l.start + l.dur)),
    toasts: window.__toasts.slice(-2),
  };
})()"""

# ✂ Erase parts, at phone width. The eraser is the one control here you DRAW on, which makes it the one
# that fails in ways a layout audit cannot see:
#
#   * the pointer -> mask mapping — clientX/rect are viewport pixels, the mask canvas is source pixels.
#     Get it wrong and the erase appears somewhere other than where the finger went.
#   * the picture scrolling off — you cannot draw on what you cannot see, and Apply has to be reachable.
#   * `touch-action` — without it the browser claims the drag as a scroll and the stroke never lands, so
#     the tool is dead to a finger and perfect to a mouse.
#
# The stroke is real PointerEvents and the assertion SAMPLES the scrim canvas, so what is checked is "the
# pixels under the finger are the pixels that got erased", not "a function was called".
#
# `touch-action` is asserted from the COMPUTED STYLE, deliberately, and not left to the stroke: a
# synthetic PointerEvent is dispatched straight to the element and never goes through the gesture
# recogniser, so the drag lands perfectly with the property removed. Verified by deleting it and watching
# this file still pass — which is exactly the kind of coverage that reads as green and is not there.
ERASE_PROBE = r"""(async () => {
  const layer = () => ((JSON.parse(localStorage.getItem('pc_meme_project')||'null')||{layers:[]})
                        .layers.find(l => l.id === 'L1') || {});
  document.getElementById('mb-erase').click();
  const art = document.getElementById('er-src'), ov = document.getElementById('er-ov');
  if (!art || !ov) return {err: 'the eraser did not open'};
  for (let i = 0; i < 40 && !ov.width; i++) await new Promise(r => setTimeout(r, 50));
  if (!ov.width) return {err: 'the mask never sized itself (the picture did not load)'};

  const r = art.getBoundingClientRect();
  const pt = (fx, fy) => ({clientX: r.left + r.width * fx, clientY: r.top + r.height * fy});
  const fire = (t, p) => art.parentElement.dispatchEvent(new PointerEvent(t, {
    ...p, pointerId: 1, pointerType: 'touch', isPrimary: true, bubbles: true, cancelable: true}));
  // A stroke straight across the middle. Several moves, because one is indistinguishable from a tap.
  fire('pointerdown', pt(0.2, 0.5));
  for (let i = 3; i <= 8; i++) fire('pointermove', pt(i / 10, 0.5));
  fire('pointerup', pt(0.8, 0.5));

  // The scrim is drawn ONLY where the mask has been rubbed away, so its alpha is the erased region.
  const c = ov.getContext('2d');
  const alphaAt = (fx, fy) => c.getImageData(Math.round(ov.width * fx), Math.round(ov.height * fy), 1, 1).data[3];
  const painted = alphaAt(0.5, 0.5), corner = alphaAt(0.04, 0.04);

  // Everything a thumb has to reach, with the picture on screen.
  const vw = innerWidth, vh = innerHeight;
  const box = id => { const e = document.getElementById(id); return e ? e.getBoundingClientRect() : null; };
  const go = box('er-go'), pic = art.getBoundingClientRect();
  const small = ['er-rub','er-put','er-undo','er-all','er-go','er-cancel']
    .map(id => ({id, h: Math.round((box(id)||{height:0}).height)})).filter(x => x.h < 24);
  // Read BEFORE Apply: it closes the dialog, and getComputedStyle on the detached element then
  // returns "" for everything — which reads as "touch-action is unset" and fails on a correct build.
  const touchAction = getComputedStyle(art.parentElement).touchAction;

  document.getElementById('er-go').click();
  for (let i = 0; i < 40 && !layer().mask; i++) await new Promise(r => setTimeout(r, 50));

  return {
    painted, corner,
    touchAction,
    maskW: ov.width, maskH: ov.height,
    overflow: document.documentElement.scrollWidth > vw + 1,
    picOnScreen: pic.top >= -1 && pic.bottom <= vh + 1 && pic.left >= -1 && pic.right <= vw + 1,
    applyOnScreen: !!go && go.bottom <= vh + 1 && go.top >= -1,
    small,
    mask: layer().mask || '',
    stillOpen: !!document.getElementById('er-src'),
  };
})()"""

AUDIT = r"""(() => {
  const out = {overflow:false, offscreen:[], tiny:[], overlap:[], present:{}, panel:false};
  out.overflow = document.documentElement.scrollWidth > window.innerWidth + 1;
  const vw = window.innerWidth;
  out.panel = !!document.getElementById('mb-inspector') || !!document.querySelector('.mb-f');
  const ids = %s;
  ids.forEach(id => { out.present[id] = !!document.getElementById(id); });
  // EXACTLY one, not merely present. #mb-talk is emitted from two mutually exclusive branches (an
  // image layer, and a character-pose layer, which is a video) — if that exclusivity ever breaks,
  // two elements share an id and only the first is ever wired up.
  out.dupes = ids.filter(id => document.querySelectorAll('#' + id).length > 1);

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
                for dup in res.get("dupes") or []:
                    problems.append((label, "duplicate-id", f"#{dup} is in the DOM more than once"))
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

            # ✂ ERASE, at the NARROWEST width — the modal has to hold a picture, four tools, a slider and
            # Apply on the smallest screen the app supports, and the stroke has to land where the finger did.
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": WIDTHS[-1][0], "height": WIDTHS[-1][1],
                        "deviceScaleFactor": 2, "mobile": True})
            await call("Page.navigate", {"url": url})
            for _ in range(40):
                await asyncio.sleep(0.25)
                if await js("window.__ready === true"):
                    break
            r = await call("Runtime.evaluate",
                           {"expression": ERASE_PROBE, "returnByValue": True, "awaitPromise": True})
            er = (r or {}).get("result", {}).get("value")
            lbl = f"erase@{WIDTHS[-1][0]}px"
            if not er:
                problems.append((lbl, "erase-broken", "the probe did not run"))
            elif er.get("err"):
                problems.append((lbl, "erase-broken", er["err"]))
            else:
                # The stroke went across the middle, so the middle must be rubbed out and the corner
                # must not. Both halves matter: an erase that covers EVERYTHING also "painted".
                if not er["painted"]:
                    problems.append((lbl, "erase-broken",
                                     "the stroke did not erase anything — the pointer never reached the "
                                     "canvas (touch-action?) or the mapping is wrong"))
                if er["corner"]:
                    problems.append((lbl, "erase-broken",
                                     "a corner the stroke never touched was erased too"))
                if er.get("touchAction") != "none":
                    problems.append((lbl, "erase-broken",
                                     f"the drawing surface has touch-action:{er.get('touchAction')} — a "
                                     "finger drag will scroll the page instead of drawing"))
                if not er["picOnScreen"]:
                    problems.append((lbl, "offscreen-control", "the picture is not fully on screen"))
                if not er["applyOnScreen"]:
                    problems.append((lbl, "offscreen-control", "Apply is below the fold"))
                if er["overflow"]:
                    problems.append((lbl, "horizontal-overflow", "the eraser scrolls the page sideways"))
                for s in er["small"]:
                    problems.append((lbl, "tiny-tap-target", f"#{s['id']} is {s['h']}px tall"))
                if not er["mask"]:
                    problems.append((lbl, "erase-broken", "Apply did not put a mask on the layer"))
                if er["stillOpen"]:
                    problems.append((lbl, "erase-broken", "Apply left the dialog open"))
                print(f"{lbl}: mask={er['maskW']}x{er['maskH']} painted={er['painted']} "
                      f"corner={er['corner']} saved={'yes' if er['mask'] else 'NO'}")

            # ✂ CUT. Arithmetic, so it is checked against exact numbers rather than "it changed".
            await call("Page.navigate", {"url": url})
            for _ in range(40):
                await asyncio.sleep(0.25)
                if await js("window.__ready === true"):
                    break
            r = await call("Runtime.evaluate",
                           {"expression": CUT_SPLIT, "returnByValue": True, "awaitPromise": True})
            cut = (r or {}).get("result", {}).get("value")
            if not cut:
                problems.append(("cut", "bad-cut", "the cut probe did not run"))
            elif cut.get("err"):
                problems.append(("cut", "bad-cut", cut["err"]))
            else:
                # start/dur/trim/xin/xout/sound of each half, and the pair of totals that says no other
                # clip moved. trim 3.5 is the speed conversion: 1.5 already skipped + 1s of slot at 2x.
                want_a = {"start": 2, "dur": 1, "trim": 1.5, "xin": 0.5, "xout": 0, "sound": "boing"}
                want_b = {"start": 3, "dur": 3, "trim": 3.5, "xin": 0, "xout": 0.5, "sound": ""}
                for half, want in (("a", want_a), ("b", want_b)):
                    for k, v in want.items():
                        got = cut[half].get(k)
                        if isinstance(v, str):
                            ok = got == v
                        else:
                            ok = got is not None and abs(float(got) - v) < 0.01
                        if not ok:
                            problems.append(("cut", "bad-cut",
                                             f"half {half}: {k} is {got!r}, expected {v!r}"))
                if cut["n"] != 3:
                    problems.append(("cut", "bad-cut", f"expected 3 layers after the cut, got {cut['n']}"))
                if cut["guardKept"] != 2:
                    problems.append(("cut", "bad-cut",
                                     "a cut 0.02s into the clip was made anyway — it must be refused "
                                     f"rather than leave a sliver (layers={cut['guardKept']})"))
                if abs(cut["endAfter"] - cut["endBefore"]) > 0.01:
                    problems.append(("cut", "bad-cut",
                                     f"the meme changed length: {cut['endBefore']}s → {cut['endAfter']}s"))
                if not cut["selIsSecondHalf"]:
                    problems.append(("cut", "bad-cut", "the second half is not left selected"))
                if cut["cutAllAdded"] != 2:
                    problems.append(("cut", "bad-cut",
                                     "✂ Cut here split "
                                     f"{cut['cutAllAdded']} layers, expected the 2 under the playhead"))
                if abs(cut["endAfterAll"] - cut["endBefore"]) > 0.01:
                    problems.append(("cut", "bad-cut",
                                     f"✂ Cut here changed the length: {cut['endAfterAll']}s"))
                print(f"cut: halves={cut['a']['dur']}s+{cut['b']['dur']}s trim={cut['b']['trim']} "
                      f"end={cut['endAfter']}s cutAll+{cut['cutAllAdded']} guard={cut['guardKept']}")

            # Mouth placement at BOTH scales. A phone is zoom 1 and a tablet is not, and the whole
            # class of bug here is a control that is exact in one coordinate system and skewed in the
            # other — checking either width alone proves nothing about the other.
            for w, h in MOUTH_WIDTHS:
                await call("Emulation.setDeviceMetricsOverride",
                           {"width": w, "height": h, "deviceScaleFactor": 2, "mobile": True})
                await call("Page.navigate", {"url": url})
                for _ in range(40):
                    await asyncio.sleep(0.25)
                    if await js("window.__ready === true"):
                        break
                r = await call("Runtime.evaluate",
                               {"expression": MOUTH_PLACEMENT, "returnByValue": True,
                                "awaitPromise": True})
                mp = (r or {}).get("result", {}).get("value")
                label = f"mouth@{w}px"
                if not mp:
                    problems.append((label, "mouth-misplaced", "the placement probe did not run"))
                    continue
                if mp.get("err"):
                    problems.append((label, "mouth-misplaced", mp["err"]))
                    continue
                want, shown, sent = mp["want"], mp["shown"], mp.get("sent")
                # 0.02 of the picture. The pin is a 3px rule with a transform on it, so its measured
                # centre is a pixel or so off by construction; the bug this guards against is off by
                # a THIRD.
                for k in ("x", "y", "w"):
                    if abs(shown[k] - want[k]) > 0.02:
                        problems.append((label, "mouth-misplaced",
                                         f"the marker is painted at {k}={shown[k]:.3f} when it was "
                                         f"put at {want[k]:.2f} (body zoom {mp['zoom']}) — you "
                                         "cannot aim with it"))
                if not isinstance(sent, dict):
                    problems.append((label, "mouth-misplaced",
                                     "the placement never reached the render"))
                else:
                    for k in ("x", "y", "w"):
                        if abs(float(sent.get(k, -9)) - want[k]) > 0.02:
                            problems.append((label, "mouth-misplaced",
                                             f"the render was sent {k}={sent.get(k)} for a marker "
                                             f"put at {want[k]:.2f}"))
                print(f"{label}: zoom={mp['zoom']} painted="
                      f"({shown['x']:.3f},{shown['y']:.3f},w={shown['w']:.3f}) sent={sent}")

            await call("Page.navigate", {"url": url})
            for _ in range(40):
                await asyncio.sleep(0.25)
                if await js("window.__ready === true"):
                    break
            r = await call("Runtime.evaluate",
                           {"expression": POSE_TALK, "returnByValue": True, "awaitPromise": True})
            pz = (r or {}).get("result", {}).get("value")
            if not pz:
                problems.append(("pose", "no-mouth-picker", "the pose probe did not run"))
            elif pz.get("err"):
                problems.append(("pose", "no-mouth-picker", pz["err"]))
            else:
                if "/client/meme/character/carl" not in (pz.get("shown") or ""):
                    problems.append(("pose", "no-mouth-picker",
                                     "the picker showed the layer's clip, not the pose artwork "
                                     f"({pz.get('shown')!r}) — an <img> renders that as nothing"))
                elif not pz.get("loaded"):
                    problems.append(("pose", "no-mouth-picker",
                                     "the pose artwork did not load — the marker has no picture"))
                if pz.get("character") != "carl":
                    problems.append(("pose", "no-mouth-picker",
                                     f"the render was asked for character={pz.get('character')!r}"))
                if not isinstance(pz.get("mouth"), dict):
                    problems.append(("pose", "no-mouth-picker",
                                     "the placement never reached the render — it silently "
                                     "falls back to auto-detect, which is the bug"))
                print(f"pose: shown={pz.get('shown')} loaded={pz.get('loaded')} "
                      f"character={pz.get('character')!r} mouth={pz.get('mouth')}")

        if not problems:
            print("OK  meme builder mobile checks passed")
            return 0
        print()
        for width, kind, detail in problems:
            print(f"FAIL  [{width}] {kind}: {detail}")
        return 1
    finally:
        proc.terminate()
        # Take the profile away again — /tmp is a tmpfs here, so a ~130 MB Chrome profile left
        # behind is 130 MB of RAM held until reboot. The pre-run rm above only stopped it from
        # accumulating ACROSS runs; it still left one profile resident after every run.
        try:
            proc.wait(timeout=10)          # let it release the profile before we delete it
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(PROFILE, ignore_errors=True)


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
            # The mouth picker's picture for a CHARACTER POSE. The app serves this from the pose
            # catalogue (GET /client/meme/character/<name>, see _pose_art_path, which is what the
            # unit tests pin); here it only has to be the same BYTES, so the <img> either renders
            # or doesn't. Nothing else in this harness needs the app running, and this must not be
            # what changes that.
            if path.startswith("/client/meme/character/"):
                name = os.path.basename(path).rsplit(".", 1)[0]
                return os.path.join(ROOT, "assets", "characters", f"{name}.png")
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
