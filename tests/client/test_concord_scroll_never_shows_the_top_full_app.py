"""Sending or receiving in a Communities room never shows the top of its history, not even for a frame.

Reported from PosterChanOS: "Communities -> when I send message or message is received, chat room
location jumps". Measured: every repaint rebuilds `.cc-messages`, four times for one sent message and
once per received one, and the rebuilt scroller was born at scrollTop 0 — the room's OLDEST messages —
until a later animation frame put it back. This records the scroller in the microtask straight after
each rebuild (a MutationObserver), which is before any frame can be drawn, and requires the reader to
be where they were: at the bottom after sending, on the same message while reading back.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SETUP = r"""(()=>{
  const me=__PC.me().pubkey, other='b'.repeat(64);
  window.__msgs=[];for(let i=0;i<60;i++)window.__msgs.push({id:'m'+String(i).padStart(3,'0')+'x'.repeat(60),pubkey:i%3?other:me,
     text:'message number '+i+(i%5?'':' with a longer line that wraps so rows differ in height, twice over, twice over'),at:1000+i,kind:9,tags:[]});
  const room={name:'Scroll room',communityId:'c'.repeat(64),naddr:'fixture-community',url:'https://fixture.invalid/invite/x#s',
    channels:[{id:'fixture-general',name:'general'}],cord:{bundle:{owner:me,relays:['wss://fixture.invalid']},hydrated:true}};
  localStorage.setItem('pc.concord.invites',JSON.stringify([room]));localStorage.setItem('pc.concord.active','0');
  window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[{id:'fixture-general',name:'general',streamPubkeys:['6'.repeat(64)]}]}),
    inspectChat:async()=>({messages:window.__msgs.slice(),reactions:[],reactionIds:[]}),
    createChatWrap:async(_b,_w,_c,text,_a,_s,tags,kind)=>({rumorId:'f'.repeat(64),wrap:{kind:1059,pubkey:'5'.repeat(64),id:'w'.repeat(64)},ms:Date.now(),tags})};
  __PC.relayPublishRoom=async()=>({ok:true,accepted:1,uncertain:false,msg:''});
  __PC.switchView('concord');
})()"""

WATCH = r"""(()=>{window.__seen=[];const feed=document.getElementById('feed');
  const mo=new MutationObserver(()=>{const b=document.querySelector('.cc-messages');if(!b||b===window.__lastBox)return;window.__lastBox=b;
    __seen.push({top:Math.round(b.scrollTop),fromBottom:Math.round(b.scrollHeight-b.clientHeight-b.scrollTop)});});
  mo.observe(feed,{childList:true,subtree:true});window.__stopWatch=()=>mo.disconnect();window.__lastBox=document.querySelector('.cc-messages');})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 390])
def test_the_room_never_flashes_its_oldest_messages(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=850, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        if width < 600:
            await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(SETUP)
        await b.until("document.querySelectorAll('.cc-message').length>=50 || !!document.querySelector('[data-cc-channel]')")
        await b.js("(()=>{const c=document.querySelector('[data-cc-channel]');if(c&&!document.querySelector('.cc-app.show-chat .cc-message'))c.click();})()")
        await b.until("document.querySelectorAll('.cc-message').length>=50")
        await asyncio.sleep(2)
        # Sending from the bottom.
        await b.js(WATCH)
        await b.js("const i=document.getElementById('cc-input');i.value='hello from the test';i.dispatchEvent(new Event('input'));document.getElementById('cc-send').click()")
        await asyncio.sleep(2)
        seen = await b.js("__stopWatch(),__seen")
        assert seen, "sending did not repaint the room — the check saw nothing"
        assert all(s["fromBottom"] <= 2 for s in seen), f"sending showed the room away from the newest message: {seen}"
        # Reading back when one arrives.
        await b.js("const b=document.querySelector('.cc-messages');b.scrollTop=Math.max(0,b.scrollTop-900);b.dispatchEvent(new Event('scroll'))")
        await asyncio.sleep(.6)
        before = await b.js("Math.round(document.querySelector('.cc-messages').scrollTop)")
        await b.js(WATCH)
        await b.js("__msgs.push({id:'n'+'y'.repeat(63),pubkey:'b'.repeat(64),text:'a new one arrives',at:5000,kind:9,tags:[]});PCConcord.refreshActiveChannel&&PCConcord.refreshActiveChannel()")
        await b.until("__seen.length>0")
        await asyncio.sleep(.5)
        seen = await b.js("__stopWatch(),__seen")
        assert seen, "receiving did not repaint the room — the check saw nothing"
        assert all(abs(s["top"] - before) <= 4 for s in seen), f"a received message moved the reader (was at {before}): {seen}"

    asyncio.run(desktop.with_browser("online", "", check))
