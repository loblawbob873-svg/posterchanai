"""The PDF editor on a phone: it SCROLLS, a signature shows where it goes, and Preview shows the result.

Three reports, one screen:
  * "the pdf editor has no scroll so i can't edit everything" -- every page overlay was
    touch-action:none, so on a phone (where the pages fill the screen) a swipe drew or dropped a text
    box and the document never moved past page one. Measured with a real finger drag: 504px
    scrolled with the fix, 0 with the old touch-action:none.
  * "where does the signature go after you make it?" -- the only cue was a toast. Now a strip shows the
    signature and what a tap will do; tapping a placed signature picks it, the next tap moves it.
  * "let you see a preview ... how the form looks as you edit" -- Preview renders the very bytes Save
    would write (form filled in, signature placed), and Back returns with every edit intact.

Driven with REAL touch input (CDP touch events and a synthesized scroll gesture) in the shipped bundle.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_pdf_edit_full_app import MAKE
from tests.client.test_pdf_typed_signature_full_app import IMAGES


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _open(b, phone=True):
    w, h = (400, 800) if phone else (1280, 800)
    await b.call("Emulation.setDeviceMetricsOverride", dict(width=w, height=h, deviceScaleFactor=1, mobile=phone))
    if phone:
        await b.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js(MAKE)
    await b.until("!!document.querySelector('.pv-edit')")
    await b.js("document.querySelector('.pv-edit').click()")
    await b.until("document.querySelectorAll('.pe-page').length===3")
    await asyncio.sleep(.4)


async def _page_point(b, nth, fx, fy):
    return await b.js(f"(()=>{{const r=document.querySelectorAll('.pe-overlay')[{nth}].getBoundingClientRect();"
                      f"return [r.left+r.width*{fx}, r.top+r.height*{fy}]}})()")


async def _swipe(b, x, y, dy):
    """A real finger drag through Chrome's own gesture detection (CDP's synthesizeScrollGesture does
    not scroll in this headless setup at all, fixed or not -- measured, so it proves nothing here)."""
    await b.call("Input.dispatchTouchEvent", dict(type="touchStart", touchPoints=[dict(x=x, y=y)]))
    for i in range(1, 16):
        await b.call("Input.dispatchTouchEvent", dict(type="touchMove", touchPoints=[dict(x=x, y=y + dy * i / 15)]))
        await asyncio.sleep(0.016)
    await b.call("Input.dispatchTouchEvent", dict(type="touchEnd", touchPoints=[]))
    await asyncio.sleep(.5)


async def _touch_tap(b, x, y):
    await b.call("Input.dispatchTouchEvent", dict(type="touchStart", touchPoints=[dict(x=x, y=y)]))
    await b.call("Input.dispatchTouchEvent", dict(type="touchEnd", touchPoints=[]))
    await asyncio.sleep(.2)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_finger_scrolls_the_document_and_a_tap_still_types():
    got = {}

    async def check(b):
        await _open(b)
        x, y = await _page_point(b, 0, 0.5, 0.6)
        y = min(y, 700)
        await _swipe(b, x, y, -400)
        got["scrolled"] = await b.js("document.querySelector('.pe-pages').scrollTop")
        got["boxes_after_swipe"] = await b.js("document.querySelectorAll('.pe-textbox').length")
        # Keep scrolling to the END: the last page must be reachable.
        await b.js("(()=>{const p=document.querySelector('.pe-pages');p.scrollTop=p.scrollHeight})()")
        await asyncio.sleep(.3)
        got["last_page_visible"] = await b.js("(()=>{const r=[...document.querySelectorAll('.pe-page')].pop().getBoundingClientRect();return r.bottom<=innerHeight+1&&r.top<innerHeight})()")
        # A TAP with the Text tool still opens a box.
        tx, ty = await _page_point(b, 2, 0.3, 0.3)
        await _touch_tap(b, tx, ty)
        got["box_after_tap"] = await b.js("document.querySelectorAll('.pe-textbox').length")
        await b.js("(()=>{const t=document.querySelector('.pe-textbox');t&&t.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))})()")
        # Escape cancelled the note -- and ONLY the note: it used to close the whole sheet (capture-phase
        # listener in preview.js) and throw away every unsaved edit.
        got["editor_after_escape"] = await b.js("!!document.querySelector('.pe-root') && !document.querySelector('.pe-textbox')")
        # The drawing tools keep one finger for the pen.
        await b.js("document.querySelector('.pe-tool[data-tool=\"draw\"]').click()")
        got["draw_touch_action"] = await b.js("getComputedStyle(document.querySelector('.pe-overlay')).touchAction")
        got["errors"] = await b.js("__errors")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["scrolled"] > 150, ("a finger swipe did not scroll the document", got)
    assert got["boxes_after_swipe"] == 0, "the swipe dropped a text box"
    assert got["last_page_visible"], "the last page cannot be reached"
    assert got["box_after_tap"] == 1, "a tap with Text no longer opens a box"
    assert got["editor_after_escape"], "Escape in a text note closed the editor and discarded the edits"
    # One finger is the pen; pinch is the editor's OWN now (the APK's WebView has the browser's off), so
    # the overlay hands the browser nothing -- see test_pdf_zoom_full_app.py for the pinch itself.
    assert got["draw_touch_action"] == "none", got["draw_touch_action"]
    assert not got["errors"], got["errors"]


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_signature_strip_places_moves_and_removes():
    got = {}

    async def check(b):
        await _open(b)
        await b.js("document.querySelector('.pe-tool[data-tool=\"sign\"]').click()")
        await b.until("!!document.querySelector('.pe-sign-tab[data-mode=\"type\"]')")
        await b.js("document.querySelector('.pe-sign-tab[data-mode=\"type\"]').click()")
        await b.until("document.activeElement===document.querySelector('.pe-sign-name')")
        await b.call("Input.insertText", {"text": "Ada Lovelace"})
        await b.js("document.querySelector('.pe-sign-ok').click()")
        await b.until("!document.querySelector('.pe-sign-card')")
        got["strip"] = await b.js("(()=>{const h=document.querySelector('.pe-hint');const r=h.getBoundingClientRect();"
                                  "return {shown:!h.hidden&&r.height>0, img:!!h.querySelector('img.pe-hint-sig')&&h.querySelector('img').naturalWidth>0,"
                                  "say:h.textContent, right:r.right, vw:innerWidth}})()")
        # Place it near the top of page 1, then pick it and move it lower.
        x, y = await _page_point(b, 0, 0.5, 0.2)
        await _touch_tap(b, x, y)
        await _touch_tap(b, x, y)                                     # tap the placed signature: picked
        got["picked_say"] = await b.js("document.querySelector('.pe-hint').textContent")
        x2, y2 = await _page_point(b, 0, 0.5, 0.7)
        await _touch_tap(b, x2, y2)                                   # the next tap moves it
        # A second one, then remove it with the strip's button.
        x3, y3 = await _page_point(b, 0, 0.5, 0.45)
        await _touch_tap(b, x3, y3)
        await _touch_tap(b, x3, y3)
        await b.js("document.querySelector('.pe-hint-remove').click()")
        await b.js("document.querySelector('.pe-save').click()")
        await b.until("!!window.__saved")
        got["images"] = await b.js(IMAGES.replace("out.push({w:Math.hypot(m[0],m[1]), h:Math.hypot(m[2],m[3])})",
                                                  "out.push({w:Math.hypot(m[0],m[1]), h:Math.hypot(m[2],m[3]), y:m[5]})"))
        got["errors"] = await b.js("__errors")

    asyncio.run(desktop.with_browser("online", "", check))
    s = got["strip"]
    assert s["shown"] and s["img"] and "Tap the page where your signature goes" in s["say"], s
    assert s["right"] <= s["vw"] + 0.5, s
    assert "Tap where this signature should go" in got["picked_say"], got["picked_say"]
    assert len(got["images"]) == 1, ("placed, moved, then a second placed and removed: one should remain", got["images"])
    # Moved DOWN the page: PDF y grows upward, so the moved signature sits in the lower half (500pt page).
    assert got["images"][0]["y"] < 250, ("the signature was not moved to where the second tap was", got["images"])
    assert not got["errors"], got["errors"]


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("phone", [True, False])
def test_preview_shows_what_save_writes_and_back_keeps_the_edits(phone):
    got = {}

    async def check(b):
        await _open(b, phone)
        await b.until("!!document.querySelector('.pe-fields [data-f=\"full_name\"]')")
        await b.js("(()=>{const i=document.querySelector('.pe-fields [data-f=\"full_name\"]');i.value='Ada Lovelace';i.dispatchEvent(new Event('input'));})()")
        await b.js("document.querySelector('.pe-cell:nth-of-type(3) [data-pa=\"del\"]').click()")
        await b.js("document.querySelector('.pe-preview-btn').click()")
        await b.until("document.querySelectorAll('.pe-preview-page').length===2")
        await asyncio.sleep(.3)
        got["editing_hidden"] = await b.js("getComputedStyle(document.querySelector('.pe-pages')).display==='none'")
        got["back"] = await b.js("(()=>{const r=document.querySelector('.pe-preview-back').getBoundingClientRect();return r.width>0&&r.right<=innerWidth+.5&&r.bottom<=innerHeight+.5})()")
        # The field's value is drawn IN the page: the field's area has dark (text) pixels in the preview.
        got["ink_in_field"] = await b.js(r"""(()=>{const c=document.querySelector('.pe-preview-page'),x=c.getContext('2d');
          const sx=c.width/400, sy=c.height/500;            // the test page is 400x500pt; field at x40 y380 (from bottom) 200x24
          const d=x.getImageData(Math.round(42*sx),Math.round((500-404)*sy),Math.round(196*sx),Math.round(22*sy)).data;
          let n=0;for(let i=0;i<d.length;i+=4){if(d[i]+d[i+1]+d[i+2]<300)n++;}return n;})()""")
        await b.js("document.querySelector('.pe-preview-back').click()")
        await b.until("getComputedStyle(document.querySelector('.pe-pages')).display!=='none'")
        got["field_kept"] = await b.js("document.querySelector('.pe-fields [data-f=\"full_name\"]').value")
        await b.js("document.querySelector('.pe-save').click()")
        await b.until("!!window.__saved")
        got["saved"] = await b.js("(async()=>{const d=await PDFLib.PDFDocument.load(new Uint8Array(await __saved.arrayBuffer()));"
                                  "return {pages:d.getPageCount(), field:d.getForm().getTextField('full_name').getText()}})()")
        got["errors"] = await b.js("__errors")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["editing_hidden"] and got["back"], got
    assert got["ink_in_field"] > 30, ("the filled-in field is not drawn in the preview", got["ink_in_field"])
    assert got["field_kept"] == "Ada Lovelace", "Back lost the edits"
    assert got["saved"] == {"pages": 2, "field": "Ada Lovelace"}, got["saved"]
    assert not got["errors"], got["errors"]
