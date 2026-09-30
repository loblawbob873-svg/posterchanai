"""A big form must not push the pages off the window.

Reported: "on laptop i still can't scroll down for edits". Measured live on the reporting laptop
(PosterChanOS, a 986x893 window): a 9-page form's field strip was 3,919px tall with no height limit,
which pushed the pages box ~4,000px down -- off the window -- and squeezed it to 24px. The pages could
scroll; there was no room left for them. Every earlier test used a form with ONE field.

Driven in the shipped bundle with a 9-page, 150-field form at the laptop's size and at 1366x768: the
pages keep most of the window and wheel-scroll to the last page, the field list scrolls to its last
field, and Hide form gives the pages the rest.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


MAKE_BIG = r"""(async()=>{
  await new Promise((res,rej)=>{const s=document.createElement('script');s.src='/static/vendor/pdf-lib/pdf-lib.min.js';s.onload=res;s.onerror=rej;document.head.appendChild(s);});
  const L=PDFLib, d=await L.PDFDocument.create(), f=await d.embedFont(L.StandardFonts.Helvetica);
  const form=d.getForm();
  for(let i=1;i<=9;i++){const p=d.addPage([612,792]);p.drawText('PAGE '+i,{x:40,y:740,size:16,font:f});
    for(let j=0;j<17;j++){const n=(i-1)*17+j; if(n>=150)break;
      const tf=form.createTextField('field_'+String(n).padStart(3,'0')); tf.addToPage(p,{x:40,y:700-j*38,width:260,height:22});}}
  const bytes=await d.save(); window.__saved=null;
  const P=await new Promise(r=>{const t=setInterval(()=>{if(window.PCPreview){clearInterval(t);r(PCPreview);}},50);
    if(!window.PCPreview){const s=document.createElement('script');s.src='/static/js/client/preview.js';document.head.appendChild(s);}});
  P.open({name:'big-form.pdf',mime:'application/pdf',blob:new Blob([bytes],{type:'application/pdf'}),saveBack:async b=>{window.__saved=b;}});
})()"""

GEOM = r"""(()=>{const p=document.querySelector('.pe-pages'),r=p.getBoundingClientRect(),vh=innerHeight;
  const visible=Math.max(0,Math.min(r.bottom,vh)-Math.max(r.top,0));
  const l=document.querySelector('.pe-fields-list')||document.querySelector('.pe-fields');
  return {visible, vh, top:r.top, st:p.scrollTop, max:p.scrollHeight-p.clientHeight,
          listScroll: l ? l.scrollHeight>l.clientHeight+10 : null,
          listBottom: l ? Math.round(l.getBoundingClientRect().bottom) : null}})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("w,h", [(986, 893), (1366, 768)])
def test_a_big_form_leaves_the_pages_room_and_they_scroll_to_the_end(w, h):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=w, height=h, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js(MAKE_BIG)
        await b.until("!!document.querySelector('.pv-edit')")
        await b.js("document.querySelector('.pv-edit').click()")
        await b.until("document.querySelectorAll('.pe-page').length===9 && document.querySelectorAll('.pe-fields [data-f]').length===150")
        await asyncio.sleep(.6)
        got["open"] = await b.js(GEOM)
        # The last field can be reached by scrolling the list, and typed into.
        await b.js("(()=>{const l=document.querySelector('.pe-fields-list')||document.querySelector('.pe-fields');l.scrollTop=l.scrollHeight;})()")
        got["last_field_visible"] = await b.js("""(()=>{const l=(document.querySelector('.pe-fields-list')||document.querySelector('.pe-fields')).getBoundingClientRect();
            const f=document.querySelector('.pe-fields [data-f=field_149]').getBoundingClientRect();
            return f.bottom<=l.bottom+1 && f.top>=l.top-1;})()""")
        # Wheel over the pages reaches the last page.
        pt = await b.js("(()=>{const r=document.querySelector('.pe-pages').getBoundingClientRect();return [r.left+r.width/2, Math.max(r.top,0)+60]})()")
        for _ in range(40):
            await b.call("Input.dispatchMouseEvent", dict(type="mouseWheel", x=pt[0], y=pt[1], deltaX=0, deltaY=600))
        await asyncio.sleep(.8)
        got["scrolled"] = await b.js(GEOM)
        got["last_page"] = await b.js("(()=>{const r=[...document.querySelectorAll('.pe-page')].pop().getBoundingClientRect();return r.bottom<=innerHeight+1 && r.bottom>0})()")
        # Hide form gives the pages the room back.
        await b.js("document.querySelector('.pe-fields-toggle') && document.querySelector('.pe-fields-toggle').click()")
        await asyncio.sleep(.3)
        got["hidden"] = await b.js(GEOM)
        got["toggle"] = await b.js("(document.querySelector('.pe-fields-toggle')||{}).textContent||null")

    PLAIN = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")
    asyncio.run(desktop.with_browser("online", "", check, PLAIN))
    o = got["open"]
    assert o["visible"] >= 0.45 * o["vh"], ("the form pushed the pages off the window", o)
    assert o["listScroll"], ("the field list does not scroll", o)
    assert got["last_field_visible"], "the last field cannot be reached"
    s = got["scrolled"]
    assert s["st"] >= s["max"] - 2 and got["last_page"], ("the last page cannot be reached", s)
    assert got["hidden"]["visible"] > o["visible"] and got["toggle"] == "Show form", got
