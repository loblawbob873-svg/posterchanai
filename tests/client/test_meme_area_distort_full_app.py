"""Meme Builder Distort -> An area: pick part of a photo, drag its corners, see it on the stage AND in the export.

Asked 2026-10-27: "Meme Builder: distort does not let you transform an area in the photo like photo editors let you".
Real input (touch on a phone, mouse in the PosterChanOS window): Distort -> "An area", drag across the red square
in the middle of a green photo, drag the area's top-left corner out, move the area by its centre handle, then the
REAL export. Where the corner was pulled to, the export is red (it was green); the photo away from the area is still
green; the stage drew the same thing (a screenshot of the stage at that point is red too).
"""
import asyncio
import base64
import io
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from tests.client.test_meme_drawing_full_app import (CHROME, PHOTO_H, PHOTO_W, Input, _MEDIA, _export, _near, _open,
                                                     _px, media)  # noqa: F401 (fixture)

GREEN, RED = (0, 150, 0), (230, 20, 20)
LAYER = "((JSON.parse(localStorage.getItem('pc_meme_project')||'{}').layers||[]).find(x=>x.type==='image')||{})"
RECT = "(()=>{const r=document.querySelector('.mb-item.mb-warping').getBoundingClientRect();return [r.left,r.top,r.width,r.height]})()"


def _target(m):
    im = Image.new("RGB", (PHOTO_W, PHOTO_H), GREEN)
    ImageDraw.Draw(im).rectangle([int(PHOTO_W * .4), int(PHOTO_H * .4), int(PHOTO_W * .6) - 1, int(PHOTO_H * .6) - 1], fill=RED)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    m.blobs["/target.png"] = (buf.getvalue(), "image/png")


async def _drag(b, inp, x0, y0, x1, y1):
    await inp.down(x0, y0)
    for i in range(1, 11):
        await inp.move(x0 + (x1 - x0) * i / 10, y0 + (y1 - y0) * i / 10)
        await asyncio.sleep(.02)
    await inp.up(x1, y1)
    await asyncio.sleep(.3)


async def _screen_px(b, x, y):
    shot = await b.call("Page.captureScreenshot", {"format": "png"})
    im = Image.open(io.BytesIO(base64.b64decode(shot["data"]))).convert("RGB")
    dpr = await b.js("devicePixelRatio")
    return im.getpixel((int(x * dpr), int(y * dpr)))


async def _area(b, touch):
    media = _MEDIA[0]
    _target(media)
    inp = Input(b, touch)
    await b.js("PCMeme.reset()")
    assert await b.js(f"PCMeme.addMedia({json.dumps(media.base + '/target.png')},'image/png')")
    await b.until("(()=>{const i=document.querySelector('.mb-item img');return !!i&&i.complete&&i.naturalWidth>0})()")
    await b.until("!!document.getElementById('mb-warp')")
    await inp.tap('#mb-warp')
    await b.until("!!document.getElementById('mb-warp-area')")
    await inp.tap('#mb-warp-area')
    await b.until("!!document.querySelector('.mb-apick')")
    got = {"tip": await b.js("(document.querySelector('.mb-apick-tip')||{}).textContent||''")}
    # Pick: drag across the red square (40%-60%).
    r = await b.js(RECT)
    P = lambda fx, fy: (r[0] + r[2] * fx, r[1] + r[3] * fy)
    await _drag(b, inp, *P(.4, .4), *P(.6, .6))
    await b.until("document.querySelectorAll('.mb-ah').length===4 && !!document.querySelector('.mb-am')")
    got["picked"] = await b.js(LAYER + ".warpArea||null")
    # One corner: top-left pulled out to 15%.
    # The handle sits outside its corner: drag it by the corner's travel (40% -> 15%).
    x0, y0 = await inp.center('.mb-ah[data-ac="0"]')
    cx0, cy0 = P(.4, .4)
    tx, ty = P(.15, .15)
    # Each corner's own handle is what the finger lands on (they used to lie on top of each other on a phone).
    got["under"] = await b.js(f"(()=>{{const e=document.elementFromPoint({x0},{y0});return e&&e.dataset?e.dataset.ac:null}})()")
    got["apart"] = await b.js("(()=>{const r=[...document.querySelectorAll('.mb-ah')].map(h=>h.getBoundingClientRect());"
                              "for(let i=0;i<r.length;i++)for(let j=i+1;j<r.length;j++){const a=r[i],c=r[j];"
                              "if(a.left<c.right-1&&c.left<a.right-1&&a.top<c.bottom-1&&c.top<a.bottom-1)return false}return true})()")
    await _drag(b, inp, x0, y0, x0 + tx - cx0, y0 + ty - cy0)
    got["cornered"] = await b.js(LAYER + ".warpArea||null")
    got["stage_tf"] = await b.js("(document.querySelector('.mb-acv')||{}).style?.transform||''")
    got["stage_px"] = await _screen_px(b, *P(.3, .3))
    # Clear of the area AND of every corner handle (each sits outside its corner; on a phone they are 40% of the photo).
    got["stage_far"] = await _screen_px(b, *P(.5, .06))
    # Move it all: the centre handle dragged 10% right.
    mx, my = await inp.center('.mb-am')
    await _drag(b, inp, mx, my, mx + r[2] * .1, my)
    got["moved"] = await b.js(LAYER + ".warpArea||null")
    box = await b.js("(()=>{const l=" + LAYER + ";return [l.x,l.y,l.w,l.h]})()")
    png = await _export(b, inp, media)
    B = lambda fx, fy: (int(box[0] + fx * box[2]), int(box[1] + fy * box[3]))
    got["out_pulled"], _ = _px(png, *B(.4, .3))       # inside the moved quad, outside the original square
    got["out_far"], _ = _px(png, *B(.1, .85))         # nowhere near the area
    got["out_orig"], _ = _px(png, *B(.45, .45))       # the original square, under the moved copy
    # After the export: back to the layer's controls, then the mode switch and the two area buttons, each pressed.
    if await b.js("!!document.querySelector('.mb-tab[data-tab=\"layer\"]')"):
        await inp.tap('.mb-tab[data-tab="layer"]')
    await b.until("!!document.getElementById('mb-warp-whole')")
    # The mode switch and the two area buttons, each pressed for real.
    await inp.tap('#mb-warp-whole')                       # back to the whole picture's four corners
    await b.until("document.querySelectorAll('.mb-wh:not(.mb-ah)').length===4 && !document.querySelector('.mb-ah')")
    got["whole_keeps_area"] = await b.js(LAYER + ".warpArea||null")
    await inp.tap('#mb-warp-area')
    await b.until("document.querySelectorAll('.mb-ah').length===4")
    await inp.tap('#mb-area-new')                         # pick a different part: the old area is dropped
    await b.until("!!document.querySelector('.mb-apick')")
    got["after_new"] = await b.js(LAYER + ".warpArea||null")
    r2 = await b.js(RECT)
    await _drag(b, inp, r2[0] + r2[2] * .1, r2[1] + r2[3] * .1, r2[0] + r2[2] * .3, r2[1] + r2[3] * .3)
    await b.until("document.querySelectorAll('.mb-ah').length===4")
    got["repicked"] = await b.js(LAYER + ".warpArea||null")
    await inp.tap('#mb-area-reset')                       # Undo area
    await b.until("!!document.querySelector('.mb-apick')")
    got["after_undo"] = await b.js(LAYER + ".warpArea||null")
    got["errors"] = await b.js("(window.__errors||[]).slice(-5)")
    return got


def _check(got):
    assert "Drag across" in got["tip"], got
    p = got["picked"]
    assert p and all(abs(a - b) < .04 for a, b in zip(p["r"], [.4, .4, .2, .2])), ("the area is not what was dragged across", got)
    assert got["apart"], ("the area's corner handles lie on top of each other", got)
    assert got["under"] == "0", ("the finger on the top-left handle is on another corner's handle", got)
    c = got["cornered"]["q"]
    assert abs(c[0] - .15) < .04 and abs(c[1] - .15) < .04, ("the corner did not go where it was dragged", got)
    assert got["cornered"]["q"][2:] == p["q"][2:], ("dragging one corner moved the others", got)
    assert "matrix3d" in got["stage_tf"], ("the stage does not draw the moved area", got)
    assert _near(got["stage_px"], RED, tol=60), ("the stage does not show the area where its corner went", got)
    assert _near(got["stage_far"], GREEN, tol=60), ("the stage changed the photo away from the area", got)
    mv, cq = got["moved"]["q"], got["cornered"]["q"]
    assert all(abs((mv[i] - cq[i]) - (.1 if i % 2 == 0 else 0)) < .03 for i in range(8)), ("the centre handle did not move the area", got)
    assert _near(got["out_pulled"], RED, tol=50), ("the export does not move the area", got)
    assert _near(got["out_far"], GREEN, tol=50), ("the export changed the photo away from the area", got)
    assert _near(got["out_orig"], RED, tol=50), got
    assert got["whole_keeps_area"] == got["moved"], ("switching to Whole picture lost the area", got)
    assert got["after_new"] is None, ("New area kept the old one", got)
    assert got["repicked"] and all(abs(a - b) < .04 for a, b in zip(got["repicked"]["r"], [.1, .1, .2, .2])), got
    assert got["after_undo"] is None, ("Undo area left the area", got)
    assert not got["errors"], got["errors"]


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_distort_an_area_with_a_finger(media):
    got = {}

    async def check(b):
        got.update(await _area(b, True))
    asyncio.run(_open(390, 844, True, '', media, check))
    _check(got)


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_distort_an_area_with_a_mouse_in_the_posterchanos_window(media):
    got = {}

    async def check(b):
        got.update(await _area(b, False))
    asyncio.run(_open(1280, 800, False, '?pcwin=meme', media, check))
    _check(got)
