"""Meme Builder: a photo layer's own buttons, pressed in the real client and checked against the SAVED project
(what a reload or an export would use, not what the screen happens to show):

  * ↔ / ↕ flip it; ⟲ puts rotation and flips back; Duplicate makes a second layer; Delete removes one, and
    ↶ / ↷ undo and redo that;
  * "Match canvas" sizes the canvas to the photo (720x640 here); zoom + zooms the stage in;
  * Arrange runs the timeline's re-sequence without changing a single still layer.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client.test_meme_drawing_full_app import CHROME, _MEDIA, _open, media  # noqa: F401 (fixture)

PROJ = "JSON.parse(localStorage.getItem('pc_meme_project')||'{}')"
LAYERS = PROJ + ".layers"


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome required')
def test_photo_layer_buttons(media):
    async def check(b):
        await b.js("PCMeme.reset()")
        assert await b.js(f"PCMeme.addMedia({json.dumps(_MEDIA[0].base + '/photo.png')},'image/png')")
        await b.until("!!document.getElementById('mb-fliph')")
        click = lambda i: b.js(f"document.getElementById({i!r}).click(); 1")

        await click('mb-fliph'); await b.until(f"{LAYERS}[0].flipH===true")
        await click('mb-flipv'); await b.until(f"{LAYERS}[0].flipV===true")
        await click('mb-rot0'); await b.until(f"!{LAYERS}[0].flipH && !{LAYERS}[0].flipV && !({LAYERS}[0].rotate)")

        # Match canvas waits for the photo to have loaded.
        await b.until("(()=>{const i=document.querySelector('.mb-item img');return !!(i&&i.naturalWidth)})()")
        await click('mb-canvas-match')
        await b.until(f"{PROJ}.w===720 && {PROJ}.h===640")

        z0 = await b.js("(document.getElementById('mb-zoomlbl')||document.querySelector('.mb-zoom-val')||{}).textContent||getComputedStyle(document.querySelector('.mb-stage')).transform")
        await click('mb-zoomin')
        await b.until(f"((document.getElementById('mb-zoomlbl')||document.querySelector('.mb-zoom-val')||{{}}).textContent||getComputedStyle(document.querySelector('.mb-stage')).transform)!=={json.dumps(z0)}")

        await b.until("!!document.getElementById('mb-dup')")
        await click('mb-dup'); await b.until(f"{LAYERS}.length===2")
        await b.until("!!document.getElementById('mb-del')")
        await click('mb-del'); await b.until(f"{LAYERS}.length===1")
        await click('mb-undo'); await b.until(f"{LAYERS}.length===2")
        await click('mb-redo'); await b.until(f"{LAYERS}.length===1")

        before = await b.js(f"JSON.stringify({LAYERS}.map(l=>[l.id,l.x,l.y,l.w,l.h]))")
        await b.until("!!document.getElementById('mb-arrange')")
        await click('mb-arrange')
        await asyncio.sleep(.3)
        assert await b.js(f"JSON.stringify({LAYERS}.map(l=>[l.id,l.x,l.y,l.w,l.h]))") == before, "Arrange moved a still layer"
        assert not await b.js("window.__errors||[]")
    asyncio.run(_open(1280, 800, False, '', media, check))
