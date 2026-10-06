"""Texts' hover ☺ (react) opens its reaction row NEXT TO the message -- at the desktop's zoom too.

Reported on PosterChanOS: "there is an emoji icon that appears if you hover the cursor over the message
but does nothing". The desktop scales the page with body{zoom}; the reaction row was placed with
getBoundingClientRect() (ZOOMED pixels) written straight into style.left/top (LAYOUT pixels), so at the
desktop's zoom it opened far from the button -- off-screen -- and the click looked dead. Same bug class
os.js documents for its own popovers (zf()/vwL()). Drives the real Texts screen in the shipped bundle, as its own window the way PosterChanOS opens it.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_sms_image_paste_full_app import open_texts


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SEED = r"""
(()=>{
  const S=PCSms._state(), now=Date.now(), A='+15550100';
  S.msgs.clear();
  for(let i=0;i<6;i++) S.msgs.set('d'+i,{doc:'d'+i,address:A,body:'message '+i,date:now-(6-i)*60000,incoming:i%2===0,parts:[],_at:1});
  S.ready=true; S.open=''; S.q='';
  PCSms.refreshNames();
})()
"""

MEASURE = r"""(()=>{
  const b=[...document.querySelectorAll('.sms-react-add')].pop(); b.click();
  const p=document.querySelector('.sms-react-pick'); if(!p) return {pop:false};
  const pr=p.getBoundingClientRect(), br=b.getBoundingClientRect();
  return {btnLeft:Math.round(br.left), btnRight:Math.round(br.right), vw:innerWidth, docW:document.documentElement.scrollWidth, pop:true, inView: pr.left>=0 && pr.top>=0 && pr.right<=innerWidth+1 && pr.bottom<=innerHeight+1 && pr.width>0,
          gap: Math.min(Math.abs(pr.bottom-br.top), Math.abs(pr.top-br.bottom)),
          dx: Math.abs((pr.left+pr.right)/2-(br.left+br.right)/2), buttons: p.querySelectorAll('button').length};
})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("zoom", ["1", "1.25", "0.8"])
def test_the_react_button_opens_its_row_beside_the_message(zoom):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await open_texts(b)
        await b.js(SEED)
        await b.js("document.querySelector('#sms-back') && document.querySelector('#sms-back').click()")
        await b.until("[...document.querySelectorAll('.sms-thread')].length>=1")
        await b.js("document.querySelector('.sms-thread').click()")
        await b.until("document.querySelectorAll('.sms-react-add').length>0")
        # The desktop's OWN scaling (os.js applyUiScale): --ui-scale drives body{zoom} and the .app height.
        await b.js(f"document.documentElement.style.setProperty('--ui-scale','{zoom}');true")
        await asyncio.sleep(.3)
        got.update(await b.js(MEASURE))


    # As PosterChanOS shows it: Texts in its OWN window (`?pcwin=texts`), scaled by the desktop's --ui-scale.
    extra = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
             "window.pcShell.windowContext={role:'app',view:'texts'};window.pcShell.backgroundOwner=false;")
    asyncio.run(desktop.with_browser("online", "?pcwin=texts", check, extra))
    assert got.get("pop"), ("clicking ☺ opened nothing", got)
    assert got["buttons"] == 6, got
    assert got["inView"], ("the reaction row opened outside the window", zoom, got)
    assert got["gap"] < 30 and got["dx"] < 160, ("the reaction row opened away from the message", zoom, got)
