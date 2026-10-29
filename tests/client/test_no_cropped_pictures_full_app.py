"""No picture somebody posted, attached or saved is trimmed to fit its box -- anywhere.

2026-10-28: "images in notes are still cropped? we should not be cropping anywhere". The lone timeline picture had been
fixed that morning; every other place a picture sits in a fixed box still used object-fit:cover (or a background-size
of cover), which scales the picture until the box is FULL and cuts off whatever sticks out -- a phone screenshot lost
most of its height, a receipt its total. They letterbox now: the whole picture, centred, the box's own ground around it.

Each surface's real markup is built inside the shipped client, with the shipped stylesheet (and the Photos app's own
injected styles), and the browser's COMPUTED fit is read -- a rule overridden by a later, more specific one would pass
a text search and still crop. Avatars, banners and video-call tiles are framed shapes, not pictures somebody shared, and
are deliberately not in the list.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# (surface, markup, selector of the picture inside it)
SURFACES = [
    ("post with several pictures", '<div class="note"><div class="media-car"><div class="mc-track"><div class="mc-item"><img></div></div></div></div>', '.mc-item img'),
    ("profile Media tab", '<div class="media-grid"><img></div>', '.media-grid img'),
    ("Pics grid", '<div class="pics-grid"><div class="pic-card"><img></div></div>', '.pic-card img'),
    ("Notes attachment", '<div class="nt-res"><button class="nt-res-thumb"><img></button></div>', '.nt-res-thumb img'),
    ("Photos app tile", '<div class="ph-grid"><button class="ph-tile"><img></button></div>', '.ph-tile img'),
    ("album photo", '<div class="alb-photo"><img></div>', '.alb-photo img'),
    ("album cover", '<span class="alb-cover"><img></span>', '.alb-cover img'),
    ("composer draft", '<div class="draft-body"><img></div>', '.draft-body img'),
    ("DM attachment", '<div class="dm-att"><img></div>', '.dm-att img'),
    ("file card", '<div class="files-grid"><div class="file-card"><img></div></div>', '.file-card img'),
    ("web search image", '<div class="ws-img"><img></div>', '.ws-img img'),
    ("GIF picker", '<img class="gif-item">', '.gif-item'),
]
BACKGROUNDS = [
    ("drive picture tile", '<div class="files-grid"><div class="file-card enc"><div class="file-icon has-thumb" data-encthumb="1"></div></div></div>', '.file-icon'),
    ("Explorer list icon", '<span class="fx-ic has-thumb"></span>', '.fx-ic'),
]


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('w,h', [(1280, 900), (390, 844)])
def test_every_shared_picture_is_shown_whole(w, h):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": w < 600})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        # The Photos app injects its own stylesheet when it is first drawn.
        await b.js("__PC.switchView('photos')")
        await b.until("!!document.querySelector('.ph-wrap')")
        js = """(()=>{const host=document.createElement('div');host.id='crop-probe';document.body.appendChild(host);const out={};
          for(const [name,html,sel] of %s){host.innerHTML=html;const el=host.querySelector(sel);out[name]=el?getComputedStyle(el).objectFit:'MISSING';}
          for(const [name,html,sel] of %s){host.innerHTML=html;const el=host.querySelector(sel);out[name]=el?getComputedStyle(el).backgroundSize:'MISSING';}
          host.remove();return out;})()""" % (_js(SURFACES), _js(BACKGROUNDS))
        got.update(await b.js(js))

    asyncio.run(desktop.with_browser('online', '', check))
    cropped = {k: v for k, v in got.items() if v == 'cover' or v == 'MISSING'}
    assert not cropped, ("these surfaces trim the picture to fill their box", cropped)


def _js(rows):
    import json
    return json.dumps([list(r) for r in rows])
