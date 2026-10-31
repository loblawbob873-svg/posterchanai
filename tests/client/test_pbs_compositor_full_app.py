"""PBS COMPOSITES WHAT YOU PUT IN IT -- read back from the output canvas's own pixels, in the real bundled client.

Run: venv-unified/bin/python -m pytest tests/client/test_pbs_compositor_full_app.py

Every check reads `#pbs-canvas` -- the canvas captureStream() broadcasts -- never PBS's own state, so a layer that
is in the list and not on the broadcast fails here. Real mouse events through CDP for every click and drag.

  text overlay   content, colour, outline/shadow off, size; the font changes the pixels; DRAGGED on the preview
  image overlay  added from a file (the upload refused -> kept in this device's IndexedDB, an `idb:` reference,
                 never a data: URI in the document); resized by its handle; opacity 0 hides it, 50% blends it
  camera         the camera's (green) frames are on the canvas
  screen         the screen picker was asked (getDisplayMedia, with audio) and its (blue) frames are drawn under
                 everything; "Change what's shared" asks again
  layer order    a full-canvas camera on top hides the text; moved to the bottom, the text is back
  hide           a hidden layer leaves the canvas
  output         1080p resizes the output to 1920x1080; 60 fps is picked; each is a sliding segmented control
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import pbs_fixture as F
from tests.client import test_desktop_offline_full_app as desktop

RED, YELLOW, GREEN, BLUE = (255, 0, 0), (255, 255, 0), (0, 255, 0), (0, 0, 255)


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _set(b, sel, value, ev='input'):
    await b.js("(()=>{const e=document.querySelector(%s);e.value=%s;e.dispatchEvent(new Event(%s,{bubbles:true}));return true})()"
               % (json.dumps(sel), json.dumps(value), json.dumps(ev)))
    await asyncio.sleep(.25)


async def _check(b, tmp):
    got = {}
    await F.open_pbs(b, 1280)
    # ---- text overlay
    await F.click(b, '#pbs-add-text')
    await b.until("!!document.querySelector('#pbs-p-text')")
    await _set(b, '#pbs-p-text', 'PBS')
    await _set(b, '#pbs-p-color', '#ff0000')
    await b.js("['#pbs-p-outline','#pbs-p-shadow'].forEach(s=>{const e=document.querySelector(s);if(e.checked)e.click()});true")
    await _set(b, '#pbs-p-size', '120')
    await asyncio.sleep(.3)
    got['text'] = await F.count(b, RED, None, 30)
    await F.click(b, '.pbs-seg[data-seg="font"] .pbs-chip[data-v="mono"]')
    await asyncio.sleep(.3)
    got['text_mono'] = await F.count(b, RED, None, 30)
    # drag it from where it is drawn to the lower middle
    x0, y0 = await F.canvas_to_page(b, got['text_mono']['cx'], got['text_mono']['cy'])
    x1, y1 = await F.canvas_to_page(b, got['text_mono']['cx'] + .3, got['text_mono']['cy'] + .45)
    await F.drag(b, x0, y0, x1, y1)
    await asyncio.sleep(.4)
    got['text_moved'] = await F.count(b, RED, None, 30)
    # ---- image overlay (upload refused -> IndexedDB)
    p = tmp / 'yellow.png'
    p.write_bytes(F.png(YELLOW, 80, 80))
    await F.set_file(b, '#pbs-img-file', p)
    await b.until("[...document.querySelectorAll('.pbs-layer-n')].some(n=>n.textContent.includes('yellow'))")
    await asyncio.sleep(.6)
    got['img_layers'] = await b.js("document.querySelectorAll('.pbs-layer [data-a=sel] .ic use[href=\"#i-image\"]').length")
    got['img'] = await F.count(b, YELLOW, None, 30)
    await b.js("document.querySelector('#pbs-canvas').scrollIntoView({block:'center'});true")
    await asyncio.sleep(.15)
    hx, hy = await b.js("(()=>{const r=document.querySelector('#pbs-sel .pbs-handle').getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()")
    await F.drag(b, hx, hy, hx + 120, hy + 60)
    await asyncio.sleep(.4)
    got['img_big'] = await F.count(b, YELLOW, None, 30)
    await _set(b, '#pbs-p-opacity', '50')
    got['img_half'] = await F.count(b, (128, 128, 0), None, 20)
    await _set(b, '#pbs-p-opacity', '0')
    got['img_zero'] = await F.count(b, YELLOW, None, 30)
    await _set(b, '#pbs-p-opacity', '100')
    got['doc_local'] = await b.js("JSON.stringify(Object.entries(localStorage).filter(([k])=>k.startsWith('pc_pbs_')).map(([,v])=>v))")
    # ---- camera, then layer order + hide
    await F.click(b, '#pbs-add-camera')
    await b.until("[...document.querySelectorAll('.pbs-layer-n')].some(n=>n.textContent==='Camera')")
    await asyncio.sleep(.6)
    got['cam'] = await F.count(b, GREEN, None, 30)
    await F.click(b, '#pbs-p-fit')                     # camera fills the canvas, on top of everything
    await asyncio.sleep(.4)
    got['cam_top_red'] = await F.count(b, RED, None, 30)
    for _ in range(2):
        await F.click(b, '.pbs-layer.on [data-a="down"]')
        await asyncio.sleep(.2)
    await asyncio.sleep(.3)
    got['cam_bottom_red'] = await F.count(b, RED, None, 30)
    got['order'] = await b.js("[...document.querySelectorAll('.pbs-layer .pbs-layer-n')].map(n=>n.textContent)")
    await F.click(b, '.pbs-layer.on [data-a="vis"]')
    await asyncio.sleep(.4)
    got['cam_hidden'] = await F.count(b, GREEN, None, 30)
    # ---- screen
    await F.click(b, '#pbs-add-screen')
    await b.until("[...document.querySelectorAll('.pbs-layer-n')].some(n=>n.textContent==='Screen')")
    await asyncio.sleep(.6)
    got['screen'] = await F.count(b, BLUE, None, 30)
    got['screen_red'] = await F.count(b, RED, None, 30)       # drawn UNDER the text
    await F.click(b, '#pbs-p-repick')
    await asyncio.sleep(.4)
    got['gdm'] = await b.js('__gdm')
    # ---- output
    await F.click(b, '.pbs-seg[data-seg="res"] .pbs-chip[data-v="1080"]')
    await F.click(b, '.pbs-seg[data-seg="fps"] .pbs-chip[data-v="60"]')
    await asyncio.sleep(.4)
    got['res'] = await b.js("(()=>{const c=document.querySelector('#pbs-canvas');return [c.width,c.height]})()")
    got['segs'] = await b.js("""[...document.querySelectorAll('.pbs-seg')].map(s=>{const th=s.querySelector('.pbs-seg-thumb'),on=s.querySelector('.pbs-chip.on');
        const a=th.getBoundingClientRect(),c=on.getBoundingClientRect();return [s.dataset.seg,on.dataset.v,Math.abs((a.left+a.width/2)-(c.left+c.width/2))<4]})""")
    got['errors'] = await b.js('__errors')
    await F.shot(b, 'compositor-1280.png')
    return got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_pbs_composites_text_image_camera_and_screen_in_order(tmp_path):
    got = {}

    async def check(b):
        got.update(await _check(b, tmp_path))
    asyncio.run(desktop.with_browser('online', '', check, F.init(__uploadFails=True)))
    assert got['errors'] == [], got['errors']
    assert got['text']['n'] > 1500, ('the text overlay is not on the output', got['text'])
    assert got['text_mono']['n'] != got['text']['n'], ('changing the font changed nothing on the output', got)
    t0, t1 = got['text_mono'], got['text_moved']
    assert t1['cx'] - t0['cx'] > .2 and t1['cy'] - t0['cy'] > .3, ('dragging the text did not move it', t0, t1)
    assert got['img_layers'] == 1, ('one file chosen, one image layer', got['img_layers'])
    assert got['img']['n'] > 300, ('the image overlay is not on the output', got['img'])
    assert got['img_big']['n'] > got['img']['n'] * 1.3, ('the resize handle did not resize the image', got['img'], got['img_big'])
    assert got['img_half']['n'] > 300 and got['img_zero']['n'] == 0, ('opacity is not applied', got['img_half'], got['img_zero'])
    assert 'idb:' in got['doc_local'] and 'data:image' not in got['doc_local'], 'the image must be referenced, never inlined'
    assert got['cam']['n'] > 1000, ('the camera is not on the output', got['cam'])
    assert got['cam_top_red']['n'] == 0, ('a full-canvas camera on top should hide the text', got['cam_top_red'])
    assert got['cam_bottom_red']['n'] > 1000 and got['order'][-1] == 'Camera', ('moving the camera down did not reorder', got['order'])
    assert got['cam_hidden']['n'] == 0, ('a hidden layer is still broadcast', got['cam_hidden'])
    assert got['screen']['n'] > 100000 and got['screen_red']['n'] > 1000, ('the screen is not under the overlays', got['screen'])
    assert got['gdm'] == 2, ('"Change what\'s shared" did not open the picker again', got['gdm'])
    assert got['res'] == [1920, 1080], got['res']
    assert all(ok for _, _, ok in got['segs']), ('a segmented thumb is not under its pick', got['segs'])
    assert ['fps', '60', True] in got['segs'] and ['res', '1080', True] in got['segs'], got['segs']
