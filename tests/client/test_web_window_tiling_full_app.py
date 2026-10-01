"""Window tiling works in the WEB desktop too ("make sure new tiling features works in webui").

PosterChanOS arranges compositor windows (desktop/tile.js + WayfireWM.arrange, Super+G). The web UI's
windowed desktop had no tiling at all: the Arrange button and keys only existed with the compositor
bridge. Driven here in the real bundle at desktop width, with four real app windows:
  * the taskbar's Arrange button opens Grid / Side by side / Stacked, and Grid makes a 2x2 that
    covers the desktop with no overlap;
  * the keys (Ctrl+Alt+Shift+G side by side, Super+Alt+G stacked) do the same, and never inside a
    text box;
  * a tiled window is a snap, so a viewport resize re-tiles it and Super+Down gives back its floating
    size;
  * and the page's tiling arithmetic is desktop/tile.js's, answer for answer.
"""
import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


RECTS = r"""(()=>{const d=document.getElementById('os-desk').getBoundingClientRect();
  return {desk:[d.left,d.top,d.width,d.height], wins:[...document.querySelectorAll('.osw:not(.minimised)')].map(w=>{const r=w.getBoundingClientRect();
    return {x:r.left-d.left,y:r.top-d.top,w:r.width,h:r.height,cls:w.className}})}})()"""


def _no_overlap(ws):
    for i, a in enumerate(ws):
        for b in ws[i + 1:]:
            ox = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
            oy = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
            assert ox <= 1 or oy <= 1, (a, b)


async def _four_windows(b):
    await desktop.login(b)
    await b.until("document.body.classList.contains('os-on') && !!document.querySelector('#os-desk')")
    await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
    views = await b.js("[...document.querySelectorAll('#os-desk [data-view]')].map(e=>e.dataset.view)")
    # Four apps that each own a window of their own (the feed views share one).
    pick = [v for v in ("calculator", "bookmarks", "notes", "drafts", "analytics", "news") if v in views][:4]
    assert len(pick) == 4, views
    for v in pick:
        await b.js("(()=>{const el=document.querySelector('#os-desk [data-view=\"%s\"]');el.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));el.click();})()" % v)
        await asyncio.sleep(.4)
    await b.until("document.querySelectorAll('.osw:not(.minimised)').length>=4")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_web_desktop_arranges_its_windows():
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await _four_windows(b)
        n = await b.js("document.querySelectorAll('.osw:not(.minimised)').length")
        floating = await b.js(RECTS)

        # The taskbar button, then Grid.
        assert await b.js("!!document.getElementById('os-arrange')"), "no Arrange button in the web desktop"
        await b.js("document.getElementById('os-arrange').click()")
        await b.until("!!document.querySelector('.menu-pop button[data-m]')")
        labels = await b.js("[...document.querySelectorAll('.menu-pop button[data-m]')].map(x=>x.textContent.trim())")
        assert labels == ["Grid", "Side by side", "Stacked"], labels
        await b.js("[...document.querySelectorAll('.menu-pop button[data-m]')].find(x=>x.textContent.trim()==='Grid').click()")
        await asyncio.sleep(.4)
        g = await b.js(RECTS)
        dw, dh = g["desk"][2], g["desk"][3]
        assert len(g["wins"]) == n
        _no_overlap(g["wins"])
        if n == 4:
            for w in g["wins"]:
                assert abs(w["w"] - dw / 2) < 24 and abs(w["h"] - dh / 2) < 24, (w, dw, dh)
        area = sum(w["w"] * w["h"] for w in g["wins"])
        assert area > 0.9 * dw * dh, ("the grid does not cover the desktop", area, dw * dh)

        # Keys: side by side, then stacked.
        await b.js("document.body.focus();document.dispatchEvent(new KeyboardEvent('keydown',{key:'G',ctrlKey:true,altKey:true,shiftKey:true,bubbles:true}))")
        await asyncio.sleep(.3)
        s = await b.js(RECTS)
        assert all(abs(w["h"] - dh) < 24 for w in s["wins"]), s
        _no_overlap(s["wins"])
        await b.js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'g',metaKey:true,altKey:true,bubbles:true}))")
        await asyncio.sleep(.3)
        t = await b.js(RECTS)
        assert all(abs(w["w"] - dw) < 24 for w in t["wins"]), t
        _no_overlap(t["wins"])

        # A resize re-tiles; Super+Down gives a window its floating size back.
        await b.js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'g',metaKey:true,bubbles:true}))")
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 800, "deviceScaleFactor": 1, "mobile": False})
        await asyncio.sleep(.6)
        r = await b.js(RECTS)
        _no_overlap(r["wins"])
        assert sum(w["w"] * w["h"] for w in r["wins"]) > 0.9 * r["desk"][2] * r["desk"][3], ("not re-tiled after a resize", r)
        await b.js("document.querySelector('.osw.focused')||document.querySelector('.osw').classList.add('focused')")
        before = await b.js("(()=>{const w=document.querySelector('.osw.focused');return w?w.getBoundingClientRect().width:0})()")
        await b.js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',metaKey:true,bubbles:true}))")
        await asyncio.sleep(.3)
        assert await b.js("!document.querySelector('.osw.focused').classList.contains('snapped')"), "Super+Down did not untile it"

        # A "g" typed into a text box arranges nothing.
        await b.js("(()=>{const t=document.createElement('textarea');t.id='tx';document.querySelector('.osw .osw-body,.osw').appendChild(t);t.focus();})()")
        snap = await b.js(RECTS)
        await b.js("document.getElementById('tx').dispatchEvent(new KeyboardEvent('keydown',{key:'G',ctrlKey:true,altKey:true,shiftKey:true,bubbles:true}))")
        await asyncio.sleep(.3)
        assert await b.js(RECTS) == snap
    asyncio.run(desktop.with_browser("online", "", check, ""))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_page_tiles_exactly_like_desktop_tile_js():
    os_src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    start = os_src.index("  const TILE_LAYOUTS = ")
    end = os_src.index("  function _tileZone(")
    prog = os_src[start:end] + r"""
      const ref = require(%s).tileRects, out = [];
      for (const layout of ['grid','side-by-side','stacked','nope'])
        for (let n = 0; n <= 9; n++)
          for (const work of [{x:0,y:0,width:1920,height:1040},{x:13,y:7,width:1853,height:1001},{x:0,y:0,width:390,height:700}])
            out.push(JSON.stringify(tileRects(layout,n,work)) === JSON.stringify(ref(layout,n,work)));
      console.log(JSON.stringify(out.every(Boolean)));
    """ % json.dumps(str(ROOT / "desktop/tile.js"))
    r = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) is True
