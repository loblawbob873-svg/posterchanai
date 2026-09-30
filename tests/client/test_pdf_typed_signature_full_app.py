"""PDF editor → Sign → Type: a CURSIVE signature typed as a name, placed on the page, saved in the file.

Asked for: "PDF Editor, add ability to add Cursive Signature to documents". Sign already took a DRAWN
signature; this adds Type beside Draw -- a name in one of four bundled handwriting fonts, black or blue
ink. Driven in the shipped bundled client at desktop and phone width, and the SAVED bytes are read back:

  * the dialog offers Draw and Type; Type shows the name box, four styles and two inks, all on screen;
  * the handwriting fonts really LOAD (a canvas silently falls back to another font when they have
    not, and the placed signature would then be in a different hand from the preview);
  * the preview draws the name in the chosen ink; an empty name is refused with a sentence;
  * the placed signature keeps the typed name's own proportions (it used to be forced into 160x60,
    which squashes "Al" and crushes "Alexandria Ocasio-Cortez"), and it is IN the saved PDF.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_pdf_edit_full_app import MAKE


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# The saved file's first page: every image drawn, with the width/height it was drawn at.
IMAGES = r"""(async()=>{
  const bytes=new Uint8Array(await __saved.arrayBuffer());
  const pdf=await pdfjsLib.getDocument({data:bytes}).promise, ops=await (await pdf.getPage(1)).getOperatorList();
  // pdf-lib draws an image as save, translate, rotate, scale, skew, Do, restore: the size is the
  // PRODUCT of the transforms since the save, never the last one alone (that is the identity skew).
  const mul=(a,b)=>[a[0]*b[0]+a[2]*b[1], a[1]*b[0]+a[3]*b[1], a[0]*b[2]+a[2]*b[3], a[1]*b[2]+a[3]*b[3],
                    a[0]*b[4]+a[2]*b[5]+a[4], a[1]*b[4]+a[3]*b[5]+a[5]];
  const out=[], stack=[]; let m=[1,0,0,1,0,0];
  ops.fnArray.forEach((f,i)=>{
    if(f===pdfjsLib.OPS.save) stack.push(m);
    else if(f===pdfjsLib.OPS.restore) m=stack.pop()||[1,0,0,1,0,0];
    else if(f===pdfjsLib.OPS.transform) m=mul(m, ops.argsArray[i]);
    else if(f===pdfjsLib.OPS.paintImageXObject) out.push({w:Math.hypot(m[0],m[1]), h:Math.hypot(m[2],m[3])}); });
  return out;
})()"""

# Ink on the preview pad: how many pixels are drawn, and their average colour.
INK = r"""(()=>{const p=document.querySelector('.pe-sign-pad'),d=p.getContext('2d').getImageData(0,0,p.width,p.height).data;
  let n=0,r=0,g=0,b=0;for(let i=0;i<d.length;i+=4){if(d[i+3]>200 && d[i]+d[i+1]+d[i+2]<380){n++;r+=d[i];g+=d[i+1];b+=d[i+2];}}
  return {n, r:n?r/n:0, g:n?g/n:0, b:n?b/n:0};})()"""

BOX = r"""(sel=>[...document.querySelectorAll(sel)].map(e=>{const r=e.getBoundingClientRect();
  return {l:r.left,r:r.right,t:r.top,b:r.bottom,w:r.width,h:r.height,vw:innerWidth,vh:innerHeight}}))"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 400])
def test_a_typed_cursive_signature_is_placed_and_saved(width):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("window.__toasts=[];const _t=__PC.toast;__PC.toast=m=>{__toasts.push(String(m));try{_t&&_t(m)}catch(_){}}")
        await b.js(MAKE)
        await b.until("!!document.querySelector('.pv-edit')")
        await b.js("document.querySelector('.pv-edit').click()")
        await b.until("document.querySelectorAll('.pe-page').length===3")
        await b.js("document.querySelector('.pe-tool[data-tool=\"sign\"]').click()")
        await b.until("!!document.querySelector('.pe-sign-tab[data-mode=\"type\"]')")
        got["tabs"] = await b.js("[...document.querySelectorAll('.pe-sign-tab')].map(x=>x.textContent)")
        await b.js("document.querySelector('.pe-sign-tab[data-mode=\"type\"]').click()")
        await b.until("document.activeElement===document.querySelector('.pe-sign-name')")
        # document.fonts.check() answers TRUE for a family that does not exist, so wait for the faces
        # themselves: all four added and loaded.
        await b.until("[...document.fonts].filter(f=>/PC Sig/.test(f.family)&&f.status==='loaded').length===4")
        # Loaded AND in use: the style's own font measures differently from the generic fallback.
        got["font_used"] = await b.js("(()=>{const c=document.createElement('canvas').getContext('2d');"
                                      "c.font='40px \"PC Sig Great Vibes\", cursive';const a=c.measureText('Ada Lovelace').width;"
                                      "c.font='40px cursive';return Math.abs(a-c.measureText('Ada Lovelace').width)>2;})()")
        got["styles"] = await b.js("[...document.querySelectorAll('.pe-sign-style')].map(x=>x.textContent)")
        got["layout"] = await b.js(BOX + "('.pe-sign-card, .pe-sign-style, .pe-sign-ink, .pe-sign-ok, .pe-sign-name')")
        # An empty name is refused.
        await b.js("document.querySelector('.pe-sign-ok').click()")
        got["refused"] = await b.js("!!document.querySelector('.pe-sign-card') && __toasts.some(t=>/type your name/.test(t))")
        await b.call("Input.insertText", {"text": "Ada Lovelace"})
        await asyncio.sleep(.2)
        got["black"] = await b.js(INK)
        await b.js("document.querySelector('.pe-sign-style[data-style=\"1\"]').click();document.querySelector('.pe-sign-ink[data-ink=\"#1a3a8f\"]').click()")
        got["blue"] = await b.js(INK)
        await b.js("document.querySelector('.pe-sign-ok').click()")
        await b.until("!document.querySelector('.pe-sign-card')")
        first = ".pe-cell:nth-of-type(1) .pe-overlay"
        box = await b.js("(()=>{const r=document.querySelector(%r).getBoundingClientRect();return [r.left,r.top]})()" % first)

        async def tap(x, y):
            for k in ("mousePressed", "mouseReleased"):
                await b.call("Input.dispatchMouseEvent", dict(type=k, x=box[0] + x, y=box[1] + y, button="left", clickCount=1))
        await tap(120, 120)
        # Sign pressed again changes the signature: a SHORT name this time, placed lower down.
        await b.js("document.querySelector('.pe-tool[data-tool=\"sign\"]').click()")
        await b.until("!!document.querySelector('.pe-sign-card')")
        await b.js("document.querySelector('.pe-sign-tab[data-mode=\"type\"]').click()")
        await b.until("document.activeElement===document.querySelector('.pe-sign-name')")
        await b.call("Input.insertText", {"text": "Al"})
        await b.js("document.querySelector('.pe-sign-ok').click()")
        await b.until("!document.querySelector('.pe-sign-card')")
        await tap(120, 260)
        await b.js("document.querySelector('.pe-save').click()")
        await b.until("!!window.__saved")
        got["images"] = await b.js(IMAGES)
        got["errors"] = await b.js("__errors")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["tabs"] == ["Draw", "Type"], got["tabs"]
    assert got["font_used"], "the handwriting font did not load — the canvas fell back to another font"
    assert got["styles"] == ["Dancing", "Elegant", "Classic", "Handwritten"], got["styles"]
    for r in got["layout"]:
        assert r["w"] > 0 and r["l"] >= -0.5 and r["r"] <= r["vw"] + 0.5 and r["b"] <= r["vh"] + 0.5, ("off screen", r)
    assert got["refused"], "an empty name was accepted"
    assert got["black"]["n"] > 800 and max(got["black"]["r"], got["black"]["g"], got["black"]["b"]) < 60, got["black"]
    blue = got["blue"]
    assert blue["n"] > 800 and blue["b"] > blue["r"] + 40, ("the ink is not blue", blue)
    assert len(got["images"]) == 2, ("both signatures should be in the saved file", got["images"])
    long_, short = [i["w"] / i["h"] for i in got["images"]]
    # Each keeps ITS OWN shape: "Ada Lovelace" is far wider than "Al" (a fixed 160x60 box made them equal).
    assert long_ > short * 1.8, ("the typed signatures were forced into one fixed box", got["images"])
    assert not got["errors"], got["errors"]
