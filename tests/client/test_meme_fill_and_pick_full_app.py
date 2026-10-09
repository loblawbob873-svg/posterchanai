"""Meme Builder Draw: the fill bucket fills an enclosed area and the colour picker picks what you see.

Asked for 2026-10-09: "Meme builder -> Draw -> Missing the fill bucket and color grabber that paint
programs usually have". Real input (touch at phone size, mouse in the PosterChanOS window) and the REAL
export: the fill must be in the rendered meme inside the box it was poured into and nowhere outside it,
and Pick must take the colour of the photo under the finger and hand the previous tool back.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client.test_meme_drawing_full_app import (CHROME, PHOTO_RGB, Input, _MEDIA, _draw_mode, _export,
                                                     _hex, _near, _open, _px, media)  # noqa: F401 (fixture)


async def _drag(b, inp, a, z, steps=10):
    r = await b.js("(()=>{const r=document.getElementById('mb-stage').getBoundingClientRect();return [r.left,r.top,r.width,r.height]})()")
    pt = lambda f: (r[0] + r[2] * f[0], r[1] + r[3] * f[1])
    await inp.down(*pt(a))
    for i in range(1, steps + 1):
        await inp.move(*pt((a[0] + (z[0] - a[0]) * i / steps, a[1] + (z[1] - a[1]) * i / steps)))
        await asyncio.sleep(.01)
    await inp.up(*pt(z))
    await asyncio.sleep(.25)


async def _tap_stage(b, inp, f):
    r = await b.js("(()=>{const r=document.getElementById('mb-stage').getBoundingClientRect();return [r.left,r.top,r.width,r.height]})()")
    x, y = r[0] + r[2] * f[0], r[1] + r[3] * f[1]
    await inp.down(x, y)
    await inp.up(x, y)
    await asyncio.sleep(.6)


async def _fill_and_pick(b, touch):
    media = _MEDIA[0]
    inp = Input(b, touch)
    await b.js("PCMeme.reset()")
    assert await b.js(f"PCMeme.addMedia({json.dumps(media.base + '/photo.png')},'image/png')")
    await b.until("(()=>{const i=document.querySelector('.mb-item img');return !!i&&i.complete&&i.naturalWidth>0})()")
    await asyncio.sleep(.4)
    await _draw_mode(b, inp)

    # A closed box in red, then a yellow fill inside it.
    await inp.tap('#mb-drawbar .mb-sw[aria-label="Red"]')
    await inp.tap('#mb-drawbar [data-dtool="rect"]')
    await _drag(b, inp, (0.3, 0.3), (0.7, 0.6))
    await inp.tap('#mb-drawbar .mb-sw[aria-label="Yellow"]')
    yellow = await b.js("document.querySelector('#mb-drawbar .mb-sw[aria-label=\"Yellow\"]').dataset.color")
    await inp.tap('#mb-drawbar [data-dtool="fill"]')
    await _tap_stage(b, inp, (0.5, 0.45))

    # Pick: the photo's colour, then the Fill tool comes back.
    await inp.tap('#mb-drawbar [data-dtool="pick"]')
    # On the photo (a 720x640 layer centred on 720x1280 covers 25-75% of the height), outside the box.
    await _tap_stage(b, inp, (0.15, 0.4))
    picked = await b.js("document.getElementById('mb-dcolor').value")
    tool_after = await b.js("(document.querySelector('#mb-drawbar [data-dtool][aria-pressed=\"true\"]')||{}).dataset?.dtool||''")

    await inp.tap('#mb-ddone')
    png = await _export(b, inp, media)
    inside, size = _px(png, int(0.5 * 720), int(0.45 * 1280))
    outside, _ = _px(png, int(0.85 * 720), int(0.45 * 1280))
    return {"yellow": yellow, "inside": inside, "outside": outside, "picked": picked, "tool_after": tool_after,
            "photo_at_pick": await b.js("(()=>{const i=document.querySelector('.mb-item img');return !!i})()")}


def _check(got):
    assert _near(got["inside"], _hex(got["yellow"])), ("the fill is not in the export inside the box", got)
    assert not _near(got["outside"], _hex(got["yellow"])), ("the fill leaked outside the box", got)
    assert _near(_hex(got["picked"]), PHOTO_RGB, tol=12), ("Pick did not take the photo's colour", got)
    assert got["tool_after"] == "fill", ("Pick did not hand the previous tool back", got)


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_fill_and_pick_with_a_finger(media):
    got = {}

    async def check(b):
        got.update(await _fill_and_pick(b, True))
    asyncio.run(_open(390, 844, True, '', media, check))
    _check(got)


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_fill_and_pick_with_a_mouse_in_the_posterchanos_window(media):
    got = {}

    async def check(b):
        got.update(await _fill_and_pick(b, False))
    asyncio.run(_open(1280, 800, False, '?pcwin=meme', media, check))
    _check(got)
