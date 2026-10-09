"""PDF editor: a placed signature is dragged to move and dragged by its corner to resize; Save on
Android writes the file to the device instead of opening the share sheet.

Reported 2026-10-09: "PDF editor -> save on android is functioning like share, not saving to device.
When adding signature, it should be moveable and resizeable". The signature was pixels on the overlay
canvas -- tap to pick, tap elsewhere to move, never a different size -- and Save went through
saveBlobAs, which in the APK IS the share sheet. Driven with REAL touch input in the shipped bundle;
the saved PDF is read back with pdf.js, so what is asserted is what gets written.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_pdf_edit_full_app import MAKE
from tests.client.test_pdf_typed_signature_full_app import IMAGES
from tests.client.test_pdf_editor_scroll_sign_preview_full_app import _open, _page_point, _touch_tap


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _touch_drag(b, x, y, dx, dy):
    await b.call("Input.dispatchTouchEvent", dict(type="touchStart", touchPoints=[dict(x=x, y=y)]))
    for i in range(1, 13):
        await b.call("Input.dispatchTouchEvent", dict(type="touchMove", touchPoints=[dict(x=x + dx * i / 12, y=y + dy * i / 12)]))
        await asyncio.sleep(0.016)
    await b.call("Input.dispatchTouchEvent", dict(type="touchEnd", touchPoints=[]))
    await asyncio.sleep(.3)


async def _sign(b):
    await b.js("document.querySelector('.pe-tool[data-tool=\"sign\"]').click()")
    await b.until("!!document.querySelector('.pe-sign-tab[data-mode=\"type\"]')")
    await b.js("document.querySelector('.pe-sign-tab[data-mode=\"type\"]').click()")
    await b.until("document.activeElement===document.querySelector('.pe-sign-name')")
    await b.call("Input.insertText", {"text": "Ada Lovelace"})
    await b.js("document.querySelector('.pe-sign-ok').click()")
    await b.until("!document.querySelector('.pe-sign-card')")


BOX = "(()=>{const r=document.querySelector('.pe-sig').getBoundingClientRect();return {x:r.left,y:r.top,w:r.width,h:r.height}})()"
HANDLE = "(()=>{const r=document.querySelector('.pe-sig .pe-sig-size').getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_finger_drags_a_signature_and_its_corner_resizes_it():
    got = {}

    async def check(b):
        await _open(b)
        await _sign(b)
        x, y = await _page_point(b, 0, 0.5, 0.2)
        await _touch_tap(b, x, y)                                   # place it near the top of page 1
        await b.until("!!document.querySelector('.pe-sig')")
        got["before"] = await b.js(BOX)
        bx = got["before"]["x"] + got["before"]["w"] / 2
        by = got["before"]["y"] + got["before"]["h"] / 2
        scroll0 = await b.js("document.querySelector('.pe-pages').scrollTop")
        await _touch_drag(b, bx, by, 0, 200)                        # drag it down the page
        got["scrolled_while_dragging"] = await b.js("document.querySelector('.pe-pages').scrollTop") - scroll0
        got["moved"] = await b.js(BOX)
        hx, hy = await b.js(HANDLE)
        await _touch_drag(b, hx, hy, 80, 0)                         # drag the corner: bigger
        got["resized"] = await b.js(BOX)
        await b.js("document.querySelector('.pe-save').click()")
        await b.until("!!window.__saved")
        got["images"] = await b.js(IMAGES.replace("out.push({w:Math.hypot(m[0],m[1]), h:Math.hypot(m[2],m[3])})",
                                                  "out.push({w:Math.hypot(m[0],m[1]), h:Math.hypot(m[2],m[3]), y:m[5]})"))
        got["errors"] = await b.js("__errors")

    asyncio.run(desktop.with_browser("online", "", check))
    bf, mv, rs = got["before"], got["moved"], got["resized"]
    assert mv["y"] - bf["y"] > 150, ("dragging the signature did not move it", got)
    assert abs(got["scrolled_while_dragging"]) < 5, ("dragging the signature scrolled the page instead", got)
    assert rs["w"] - mv["w"] > 50, ("dragging the corner did not resize it", got)
    assert abs(rs["w"] / rs["h"] - mv["w"] / mv["h"]) < 0.05 * (mv["w"] / mv["h"]), ("resizing changed its shape", got)
    assert len(got["images"]) == 1, got["images"]
    img = got["images"][0]
    assert img["y"] < 300, ("the saved signature is not where it was dragged to (500pt page, y grows upward)", got)
    assert img["w"] / img["h"] == pytest.approx(rs["w"] / rs["h"], rel=0.05), ("the saved size is not the resized one", got)
    assert not got["errors"], got["errors"]


STUB = r"""(()=>{window.__ms=null;window.__shared=0;
  window.Capacitor={isNativePlatform:()=>true,Plugins:{
    MediaSave:{available:async()=>({ok:true}),save:async o=>{window.__ms=o;return {ok:true,where:'Download/PosterChan/'+o.name};}},
    Filesystem:{writeFile:async()=>{window.__shared++;return {uri:'file:///x'};}},
    Share:{share:async()=>{window.__shared++;}}}};})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_save_a_copy_in_the_android_app_writes_to_the_device_not_the_share_sheet():
    got = {}

    async def check(b):
        await _open(b)
        await b.js(STUB)
        await b.js("document.querySelector('.pe-copy').click()")
        await b.until("!!window.__ms")
        got["ms"] = await b.js("({name:__ms.name,mime:__ms.mime,head:atob(__ms.data).slice(0,5)})")
        got["shared"] = await b.js("__shared")
        got["errors"] = await b.js("__errors")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["ms"] == {"name": "form (edited).pdf", "mime": "application/pdf", "head": "%PDF-"}, got
    assert got["shared"] == 0, ("Save opened the share sheet", got)
    assert not got["errors"], got["errors"]
