"""Meme Builder Distort: dragging a picture's corner warps it on the stage AND in the export.

Asked for 2026-10-09: "Meme builder also needs those standard transform/warp features to images". Real
input on the corner handle (touch on a phone, mouse in the PosterChanOS window), then the REAL export: where
the corner was pulled away the canvas background shows, the middle of the photo is still the photo, and
the stage drew the same thing (the corner pixel of the stage preview is no longer the photo either).
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client.test_meme_drawing_full_app import (CHROME, PHOTO_RGB, Input, _MEDIA, _export, _near, _open,
                                                     _px, media)  # noqa: F401 (fixture)

BOX = "(()=>{const l=JSON.parse(localStorage.getItem('pc_meme_project')||'{}').layers.find(x=>x.type==='image');return l?[l.x,l.y,l.w,l.h]:null})()"


async def _distort(b, touch):
    media = _MEDIA[0]
    inp = Input(b, touch)
    await b.js("PCMeme.reset()")
    assert await b.js(f"PCMeme.addMedia({json.dumps(media.base + '/photo.png')},'image/png')")
    await b.until("(()=>{const i=document.querySelector('.mb-item img');return !!i&&i.complete&&i.naturalWidth>0})()")
    await b.until("!!document.getElementById('mb-warp')")
    await inp.tap('#mb-warp')
    await b.until("document.querySelectorAll('.mb-wh').length===4")
    # Drag the top-left corner well into the picture (not to the exact centre: on a box that IS the
    # straight line from the top-right to the bottom-left corner, i.e. a triangle nobody can draw).
    x0, y0 = await inp.center('.mb-wh[data-wc="0"]')
    r = await b.js("(()=>{const r=document.querySelector('.mb-item.mb-warping').getBoundingClientRect();return [r.left,r.top,r.width,r.height]})()")
    x1, y1 = r[0] + r[2] * 0.35, r[1] + r[3] * 0.3
    await inp.down(x0, y0)
    for i in range(1, 11):
        await inp.move(x0 + (x1 - x0) * i / 10, y0 + (y1 - y0) * i / 10)
        await asyncio.sleep(.02)
    await inp.up(x1, y1)
    await asyncio.sleep(.4)
    warp = await b.js("(JSON.parse(localStorage.getItem('pc_meme_project')||'{}').layers.find(x=>x.type==='image')||{}).warp||null")
    stage_tf = await b.js("(document.querySelector('.mb-item.mb-warping .mb-mk')||{}).style?.transform||''")
    box = await b.js(BOX)
    png = await _export(b, inp, media)
    corner, _ = _px(png, int(box[0] + 0.12 * box[2]), int(box[1] + 0.12 * box[3]))
    middle, _ = _px(png, int(box[0] + 0.75 * box[2]), int(box[1] + 0.75 * box[3]))
    # Now try to FOLD it: drag the same corner on to the exact centre, i.e. on to the straight line from
    # the top-right to the bottom-left corner. The editor must stop it short of that line.
    x0, y0 = await inp.center('.mb-wh[data-wc="0"]')
    x2, y2 = r[0] + r[2] * 0.5, r[1] + r[3] * 0.5
    await inp.down(x0, y0)
    for i in range(1, 11):
        await inp.move(x0 + (x2 - x0) * i / 10, y0 + (y2 - y0) * i / 10)
        await asyncio.sleep(.02)
    await inp.up(x2, y2)
    await asyncio.sleep(.4)
    folded = await b.js("(JSON.parse(localStorage.getItem('pc_meme_project')||'{}').layers.find(x=>x.type==='image')||{}).warp||null")
    return {"warp": warp, "stage_tf": stage_tf, "corner": corner, "middle": middle, "folded": folded}


def _check(got):
    w = got["warp"]
    assert w and abs(w[0] - 0.35) < 0.1 and abs(w[1] - 0.3) < 0.1, ("the corner did not move where it was dragged", got)
    assert "matrix3d" in got["stage_tf"], ("the stage does not draw the warp", got)
    assert not _near(got["corner"], PHOTO_RGB, tol=25), ("the export still has the photo where the corner was pulled away", got)
    assert _near(got["middle"], PHOTO_RGB, tol=25), ("the warped photo is missing from the export", got)
    f = got["folded"]
    # Top-left is at or before the TR-BL diagonal (x/W + y/H < 1 on this box), with a margin.
    assert f and f[0] + f[1] < 0.97, ("a corner could be dragged until the picture folded", got)


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_distort_with_a_finger(media):
    got = {}

    async def check(b):
        got.update(await _distort(b, True))
    asyncio.run(_open(390, 844, True, '', media, check))
    _check(got)


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_distort_with_a_mouse_in_the_posterchanos_window(media):
    got = {}

    async def check(b):
        got.update(await _distort(b, False))
    asyncio.run(_open(1280, 800, False, '?pcwin=meme', media, check))
    _check(got)
