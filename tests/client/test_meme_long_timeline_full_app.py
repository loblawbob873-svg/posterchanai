"""Meme Builder as a video editor: a 25-minute edit is typed in, seen whole and readable, on a phone and a desktop.

2026-10-21: "we want it to be a real video editor". The timeline capped every clip at 120 s, and its zoom could
not go out far enough to show a long edit end to end (0.05x is ~4,000 px of lane for 30 minutes on a phone),
while the ruler, spaced as if zoom were never below 1x, stacked a label every 2 s on top of each other. Here,
in the real client: a clip's length typed as 1500 s stays 1500 s; Fit shows the whole edit inside the panel;
the ruler labels sit apart and read as minutes; zoom-out reaches the same view.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client.test_meme_drawing_full_app import CHROME, _MEDIA, _open, media  # noqa: F401 (fixture)

LONG = 1500


async def _long_edit(b):
    await b.js("PCMeme.reset()")
    assert await b.js(f"PCMeme.addMedia({json.dumps(_MEDIA[0].base + '/photo.png')},'image/png')")
    await b.until("!!document.getElementById('mb-f-dur')")
    await b.js(f"(()=>{{const f=document.getElementById('mb-f-dur');f.value='{LONG}';"
               "f.dispatchEvent(new Event('input',{bubbles:true}));f.dispatchEvent(new Event('change',{bubbles:true}))})()")
    await asyncio.sleep(.3)
    dur = await b.js("+document.getElementById('mb-f-dur').value")
    tab = "(()=>{const t=document.querySelector('.mb-tab[data-tab=\"timeline\"]');if(t)t.click();return true})()"
    await b.js(tab)                                   # a phone shows the timeline as a tab
    await asyncio.sleep(.3)
    await b.js("document.getElementById('mb-zoomfit').click()")
    await asyncio.sleep(.4)
    fit = await b.js("""(()=>{const port=document.getElementById('mb-timeline'),inner=document.getElementById('mb-tlinner');
      const ticks=[...document.querySelectorAll('#mb-rlane .mb-tick b')].map(e=>{const r=e.getBoundingClientRect();return [r.left,r.right,e.textContent]});
      let overlap=0;for(let i=1;i<ticks.length;i++) if(ticks[i][0]<ticks[i-1][1]-1) overlap++;
      return {port:port.clientWidth,scroll:port.scrollWidth,lane:document.querySelector('.mb-lane:not(.mb-rlane)').getBoundingClientRect().width,
        ticks:ticks.length,overlap,labels:ticks.map(t=>t.join(" ")),zoom:document.getElementById('mb-zoomlbl').textContent}})()""")
    # Back to 1x, then zoom out with the button until it stops: the ladder must reach a whole-edit view too.
    await b.js("PCMeme.render()")
    await b.js(tab)
    await asyncio.sleep(.3)
    for _ in range(20):
        await b.js("document.getElementById('mb-zoomout').click()")
    await asyncio.sleep(.3)
    out = await b.js("""(()=>{const port=document.getElementById('mb-timeline');return {port:port.clientWidth,scroll:port.scrollWidth}})()""")
    return {"dur": dur, "fit": fit, "out": out}


def _check(got):
    assert got["dur"] == LONG, ("a 25-minute clip was cut short", got)
    f = got["fit"]
    assert f["scroll"] <= f["port"] + 2, ("Fit does not show the whole edit", f)
    assert f["lane"] > 100, ("the clip collapsed to nothing", f)
    assert 3 <= f["ticks"] <= 40 and f["overlap"] == 0, ("the ruler labels pile up", f["labels"])
    assert any(":" in s for s in f["labels"]), ("a long edit's ruler should read in minutes", f["labels"])
    o = got["out"]
    assert o["scroll"] <= o["port"] + 2, ("zoom-out cannot reach the whole edit", o)


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_a_long_edit_on_a_phone(media):
    got = {}

    async def check(b):
        got.update(await _long_edit(b))
    asyncio.run(_open(390, 844, True, '', media, check))
    _check(got)


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_a_long_edit_on_the_desktop(media):
    got = {}

    async def check(b):
        got.update(await _long_edit(b))
    asyncio.run(_open(1280, 800, False, '?pcwin=meme', media, check))
    _check(got)
