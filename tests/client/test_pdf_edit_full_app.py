"""Preview edits a PDF: text, highlight, ink, a form field, rotate / delete pages — and saves it back.

Asked for: "make sure posterchan preview has PDF editing capabilities". Everything runs in the page
(the drive's files are encrypted and the server holds no key). This drives the SHIPPED Preview and
editor in the real bundled client, then reads the SAVED bytes back with pdf-lib and pdf.js — a mark
that looks right on screen but is not in the file is exactly what this must catch.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


MAKE = r"""(async()=>{
  await new Promise((res,rej)=>{const s=document.createElement('script');s.src='/static/vendor/pdf-lib/pdf-lib.min.js';s.onload=res;s.onerror=rej;document.head.appendChild(s);});
  const L=PDFLib, d=await L.PDFDocument.create(), f=await d.embedFont(L.StandardFonts.Helvetica);
  for(let i=1;i<=3;i++){const p=d.addPage([400,500]);p.drawText('ORIGINAL PAGE '+i,{x:40,y:450,size:16,font:f});}
  const tf=d.getForm().createTextField('full_name'); tf.addToPage(d.getPage(0),{x:40,y:380,width:200,height:24});
  const bytes=await d.save(); window.__saved=null; window.__orig=bytes.slice();
  const P=await new Promise(r=>{const t=setInterval(()=>{if(window.PCPreview){clearInterval(t);r(PCPreview);}},50);
    if(!window.PCPreview){const s=document.createElement('script');s.src='/static/js/client/preview.js';document.head.appendChild(s);}});
  P.open({name:'form.pdf',mime:'application/pdf',blob:new Blob([bytes],{type:'application/pdf'}),saveBack:async b=>{window.__saved=b;}});
})()"""

CHECK = r"""(async()=>{
  const L=PDFLib, bytes=new Uint8Array(await __saved.arrayBuffer()), d=await L.PDFDocument.load(bytes);
  const pdf=await pdfjsLib.getDocument({data:bytes.slice()}).promise;
  const texts=[];for(let i=1;i<=pdf.numPages;i++){const c=await (await pdf.getPage(i)).getTextContent();texts.push(c.items.map(x=>x.str).join(' '));}
  const base=await pdfjsLib.getDocument({data:__orig.slice()}).promise;
  const baseOps=(await (await base.getPage(1)).getOperatorList()).fnArray.length;
  const ops1=await (await pdf.getPage(1)).getOperatorList();
  return {baseOps, images:ops1.fnArray.filter(f=>f===pdfjsLib.OPS.paintImageXObject).length, pages:d.getPageCount(), rot:d.getPages().map(p=>p.getRotation().angle), field:d.getForm().getTextField('full_name').getText()||'',
          texts, ops:(await (await pdf.getPage(1)).getOperatorList()).fnArray.length};
})()"""


async def drag(b, sel, x0, y0, x1, y1):
    box = await b.js("(()=>{const r=document.querySelector(%r).getBoundingClientRect();return [r.left,r.top]})()" % sel)
    await b.call("Input.dispatchMouseEvent", dict(type="mousePressed", x=box[0] + x0, y=box[1] + y0, button="left", clickCount=1))
    for t in range(1, 6):
        await b.call("Input.dispatchMouseEvent", dict(type="mouseMoved", x=box[0] + x0 + (x1 - x0) * t / 5, y=box[1] + y0 + (y1 - y0) * t / 5, button="left"))
    await b.call("Input.dispatchMouseEvent", dict(type="mouseReleased", x=box[0] + x1, y=box[1] + y1, button="left", clickCount=1))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 400])
def test_edit_a_pdf_in_preview_and_save_it_back(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(MAKE)
        await b.until("!!document.querySelector('.pv-edit')")
        await b.js("document.querySelector('.pv-edit').click()")
        await b.until("document.querySelectorAll('.pe-page').length===3 && !!document.querySelector('.pe-fields [data-f=\"full_name\"]')")
        first = ".pe-cell:nth-of-type(1) .pe-overlay"
        # Text: tap, type, Enter.
        await b.js("document.querySelector('.pe-tool[data-tool=\"text\"]').click()")
        box = await b.js("(()=>{const r=document.querySelector(%r).getBoundingClientRect();return [r.left,r.top,r.width]})()" % first)
        for k in ("mousePressed", "mouseReleased"):
            await b.call("Input.dispatchMouseEvent", dict(type=k, x=box[0] + 30, y=box[1] + 200, button="left", clickCount=1))
        await b.until("!!document.querySelector('.pe-textbox') && document.activeElement===document.querySelector('.pe-textbox')")
        await b.call("Input.insertText", {"text": "SIGNED HERE"})
        await b.js("document.querySelector('.pe-textbox').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))")
        await b.until("!document.querySelector('.pe-textbox')")
        # Highlight and ink.
        await b.js("document.querySelector('.pe-tool[data-tool=\"highlight\"]').click()")
        await drag(b, first, 20, 20, 150, 60)
        await b.js("document.querySelector('.pe-tool[data-tool=\"draw\"]').click()")
        await drag(b, first, 40, 300, 160, 340)
        # A signature: draw it once, then place it.
        await b.js("document.querySelector('.pe-tool[data-tool=\"sign\"]').click()")
        await b.until("!!document.querySelector('.pe-sign-pad')")
        await drag(b, ".pe-sign-pad", 20, 40, 200, 80)
        await b.js("document.querySelector('.pe-sign-ok').click()")
        box2 = await b.js("(()=>{const r=document.querySelector(%r).getBoundingClientRect();return [r.left,r.top]})()" % first)
        for k in ("mousePressed", "mouseReleased"):
            await b.call("Input.dispatchMouseEvent", dict(type=k, x=box2[0] + 120, y=box2[1] + 420, button="left", clickCount=1))
        # The document's own form field.
        await b.js("(()=>{const i=document.querySelector('.pe-fields [data-f=\"full_name\"]');i.value='Ada Lovelace';i.dispatchEvent(new Event('input'));})()")
        # Rotate page 2, delete page 3.
        await b.js("document.querySelector('.pe-cell:nth-of-type(2) [data-pa=\"rot\"]').click()")
        await b.js("document.querySelector('.pe-cell:nth-of-type(3) [data-pa=\"del\"]').click()")
        await b.js("document.querySelector('.pe-save').click()")
        await b.until("!!window.__saved")
        got = await b.js(CHECK)
        assert got["pages"] == 2, got
        assert got["ops"] > got["baseOps"] + 10, ("the highlight and the ink are not in the saved file", got)
        assert got["images"] >= 1, ("the signature is not in the saved file", got)
        assert got["rot"] == [0, 90], got
        assert got["field"] == "Ada Lovelace", got
        assert "SIGNED HERE" in got["texts"][0] and "ORIGINAL PAGE 1" in got["texts"][0], got
        assert "ORIGINAL PAGE 3" not in " ".join(got["texts"]), got
        # And the viewer comes back showing the saved document.
        await b.until("!document.querySelector('.pe-root') && !!document.querySelector('.pv-pdf-pages canvas')")
        assert not await b.js("__errors"), await b.js("__errors")

    asyncio.run(desktop.with_browser("online", "", check))
