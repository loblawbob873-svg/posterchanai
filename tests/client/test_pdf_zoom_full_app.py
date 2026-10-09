"""PDFs zoom on a phone: two fingers pinch, − / + buttons step, pages re-draw sharp, and in the editor
everything still lands where the finger is after zooming.

Reported 2026-10-09: "unable to zoom in PDF on mobile". The APK's WebView has the browser's own
pinch-zoom switched off (and it would scale the whole app anyway), so a PDF could never be read closer
than fit-to-width, in the viewer or the editor. Driven with REAL two-finger touch input (CDP) in the
shipped bundle; the editor half saves and reads the PDF back with pdf.js, so the coordinate mapping is
checked against what is actually written, not against what is drawn.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_pdf_edit_full_app import MAKE
from tests.client.test_pdf_editor_scroll_sign_preview_full_app import _open, _page_point, _touch_tap


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _pinch(b, cx, cy, d0, d1, steps=12):
    pts = lambda d: [dict(x=cx - d / 2, y=cy, id=0), dict(x=cx + d / 2, y=cy, id=1)]
    await b.call("Input.dispatchTouchEvent", dict(type="touchStart", touchPoints=pts(d0)))
    for i in range(1, steps + 1):
        await b.call("Input.dispatchTouchEvent", dict(type="touchMove", touchPoints=pts(d0 + (d1 - d0) * i / steps)))
        await asyncio.sleep(0.016)
    await b.call("Input.dispatchTouchEvent", dict(type="touchEnd", touchPoints=[]))
    await asyncio.sleep(.6)


VIEW = r"""(async()=>{
  await new Promise((res,rej)=>{const s=document.createElement('script');s.src='/static/vendor/pdf-lib/pdf-lib.min.js';s.onload=res;s.onerror=rej;document.head.appendChild(s);});
  const L=PDFLib,d=await L.PDFDocument.create(),f=await d.embedFont(L.StandardFonts.Helvetica);
  for(let i=1;i<=2;i++){const p=d.addPage([400,500]);p.drawText('SMALL PRINT '+i,{x:40,y:450,size:6,font:f});}
  const bytes=await d.save();
  const P=await new Promise(r=>{const t=setInterval(()=>{if(window.PCPreview){clearInterval(t);r(PCPreview);}},50);
    if(!window.PCPreview){const s=document.createElement('script');s.src='/static/js/client/preview.js';document.head.appendChild(s);}});
  P.open({name:'small.pdf',mime:'application/pdf',blob:new Blob([bytes],{type:'application/pdf'})});
})()"""
PAGE = "(()=>{const c=document.querySelector('.pv-pdf-page'),r=c.getBoundingClientRect(),s=document.querySelector('.pv-pdf-body');return {cssW:r.width,backW:c.width,left:r.left,sl:s.scrollLeft,sx:s.getBoundingClientRect().left}})()"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_viewer_pinches_and_steps_and_redraws_sharp():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=400, height=800, deviceScaleFactor=2, mobile=True))
        await b.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(VIEW)
        await b.until("document.querySelectorAll('.pv-pdf-page').length===2")
        await asyncio.sleep(.4)
        got["before"] = await b.js(PAGE)
        await _pinch(b, 200, 400, 80, 240)                              # spread: zoom in ~3x
        await asyncio.sleep(1.2)                                         # the sharp re-draw
        got["pinched"] = await b.js(PAGE)
        got["pct"] = await b.js("document.querySelector('.pv-zoom-pct').textContent")
        await b.js("(()=>{const s=document.querySelector('.pv-pdf-body');s.scrollLeft=0})()")
        got["at_left"] = await b.js(PAGE)
        await b.js("document.querySelector('.pv-zoom-out').click()")
        await asyncio.sleep(.3)
        got["pct_out"] = await b.js("document.querySelector('.pv-zoom-pct').textContent")
        got["errors"] = await b.js("__errors")

    asyncio.run(desktop.with_browser("online", "", check))
    bf, pz = got["before"], got["pinched"]
    assert pz["cssW"] > bf["cssW"] * 2, ("two fingers did not zoom the PDF", got)
    assert pz["backW"] > bf["backW"] * 1.5, ("the zoomed page was stretched, not re-drawn", got)
    assert got["pct"] != "100%", got
    assert got["at_left"]["left"] >= got["at_left"]["sx"] - 1, ("the zoomed page's left side cannot be scrolled to", got)
    assert int(got["pct_out"].rstrip("%")) < int(got["pct"].rstrip("%")), got
    assert not got["errors"], got["errors"]


TEXTS = r"""(async()=>{const pdf=await pdfjsLib.getDocument({data:new Uint8Array(await __saved.arrayBuffer())}).promise;
  const c=await (await pdf.getPage(1)).getTextContent();return c.items.filter(i=>i.str==='ZOOMED').map(i=>({x:i.transform[4],y:i.transform[5]}));})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_editor_zooms_and_a_tap_still_lands_where_the_finger_is():
    got = {}

    async def check(b):
        await _open(b)
        w0 = await b.js("document.querySelector('.pe-page').getBoundingClientRect().width")
        await _pinch(b, 200, 400, 80, 200)                              # zoom in ~2.5x
        await asyncio.sleep(1.0)
        got["w_ratio"] = (await b.js("document.querySelector('.pe-page').getBoundingClientRect().width")) / w0
        got["boxes_after_pinch"] = await b.js("document.querySelectorAll('.pe-textbox').length")
        # Bring page 1's top-left into view and tap at a known spot on it, in PAGE fractions.
        await b.js("(()=>{const s=document.querySelector('.pe-pages');s.scrollTop=0;s.scrollLeft=0})()")
        await asyncio.sleep(.3)
        x, y = await _page_point(b, 0, 0.1, 0.1)
        await _touch_tap(b, x, y)
        await b.until("!!document.querySelector('.pe-textbox')")
        await b.call("Input.insertText", {"text": "ZOOMED"})
        await b.js("document.querySelector('.pe-textbox').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))")
        await b.js("document.querySelector('.pe-save').click()")
        await b.until("!!window.__saved")
        got["texts"] = await b.js(TEXTS)
        got["errors"] = await b.js("__errors")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["w_ratio"] > 1.8, ("two fingers did not zoom the editor", got)
    assert got["boxes_after_pinch"] == 0, ("the first finger of the pinch dropped a text box", got)
    assert len(got["texts"]) == 1, got
    t = got["texts"][0]
    # 400x500pt page; the tap was at 10% across and 10% down, i.e. x~40, y~450 (PDF y grows upward).
    assert 25 < t["x"] < 60 and 420 < t["y"] < 465, ("after zooming, the tap landed somewhere else on the page", got)
    assert not got["errors"], got["errors"]
