"""Sending or receiving in a Communities room never shows the top of its history, not even for a frame.

Reported from PosterChanOS: "Communities -> when I send message or message is received, chat room
location jumps". Measured: every repaint rebuilt `.cc-messages`, four times for one sent message and
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


IMAGES = r"""(async()=>{
  const plain=new Uint8Array(await (await fetch('/static/screenshot-wide.png')).arrayBuffer());
  const hx=b=>[...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,'0')).join('');
  const raw=crypto.getRandomValues(new Uint8Array(32)),iv=crypto.getRandomValues(new Uint8Array(16));
  const key=await crypto.subtle.importKey('raw',raw,'AES-GCM',false,['encrypt']);
  const cipher=await crypto.subtle.encrypt({name:'AES-GCM',iv},key,plain);
  const ox=hx(await crypto.subtle.digest('SHA-256',plain));
  const IMG='https://blossom.fixture/'+ox+'.bin';
  window.__busy=0;const f0=window.fetch;window.fetch=(u,o)=>String(u)===IMG?(__busy++,new Promise(r=>setTimeout(()=>{__busy--;r(new Response(cipher.slice(0)));},150))):f0(u,o);
  window.__imeta=['imeta','url '+IMG,'m image/png','encryption-algorithm aes-gcm','decryption-key '+hx(raw),'decryption-nonce '+hx(iv),'ox '+ox,'name shot.png'];
})()"""

SETUP = r"""(()=>{
  const me=__PC.me().pubkey, other='b'.repeat(64);
  window.__msgs=[];for(let i=0;i<60;i++)window.__msgs.push({id:'m'+String(i).padStart(3,'0')+'x'.repeat(60),pubkey:i%3?other:me,
     text:'message number '+i+(i%5?'':' with a longer line that wraps so rows differ in height, twice over, twice over'),at:1000+i,kind:9,tags:(window.__imeta&&i%4===1)?[__imeta]:[]});
  const room={name:'Scroll room',communityId:'c'.repeat(64),naddr:'fixture-community',url:'https://fixture.invalid/invite/x#s',
    channels:[{id:'fixture-general',name:'general'}],cord:{bundle:{owner:me,relays:['wss://fixture.invalid']},hydrated:true}};
  localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify([room]));localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');
  window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[{id:'fixture-general',name:'general',streamPubkeys:['6'.repeat(64)]}]}),
    inspectChat:async()=>({messages:window.__msgs.slice(),reactions:[],reactionIds:[]}),
    createChatWrap:async(_b,_w,_c,text,_a,_s,tags,kind)=>({rumorId:'f'.repeat(64),wrap:{kind:1059,pubkey:'5'.repeat(64),id:'w'.repeat(64)},ms:Date.now(),tags})};
  __PC.relayPublishRoom=async()=>({ok:true,accepted:1,uncertain:false,msg:''});
  __PC.switchView('concord');
})()"""

# Every change to the room's DOM is sampled, in the microtask straight after it (before a frame can be
# drawn) -- a rebuilt scroller AND a message list patched in place, which is how a new message arrives
# now: the scroller is kept, so watching only for a NEW scroller would see nothing at all.
WATCH = r"""(()=>{window.__seen=[];const feed=document.getElementById('feed');
  const mo=new MutationObserver(recs=>{const b=document.querySelector('.cc-messages');if(!b)return;
    if(!recs.some(r=>r.target===b||b.contains(r.target)||[...r.addedNodes].some(n=>n===b||(n.contains&&n.contains(b)))))return;
    __seen.push({top:Math.round(b.scrollTop),fromBottom:Math.round(b.scrollHeight-b.clientHeight-b.scrollTop)});});
  mo.observe(feed,{childList:true,subtree:true});window.__stopWatch=()=>mo.disconnect();})()"""

# Settled = no picture download in flight and nothing in the page changed for `quiet` ms, measured
# instead of a flat sleep; never longer than `cap`, the flat sleep it replaces. Before a watch window this
# only keeps earlier work out of it; AS the watch window after a send, it lasts until the send's repaints
# (optimistic row, signing, sent -- measured within ~0.3 s) have stopped, plus `quiet`.
SETTLE = r"""(quiet,cap)=>new Promise(res=>{let last=performance.now();const t0=last;
 const mo=new MutationObserver(()=>{last=performance.now();});mo.observe(document.body,{childList:true,subtree:true,attributes:true,characterData:true});
 const t=setInterval(()=>{const now=performance.now();if(window.__busy)last=now;
  if(now-last>=quiet||now-t0>=cap){clearInterval(t);mo.disconnect();res(Math.round(now-t0));}},50);})"""


async def settle(b, cap_s, quiet_ms=500):
    return await b.js("(%s)(%d,%d)" % (SETTLE, quiet_ms, cap_s * 1000))


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
        await settle(b, 2)
        # Sending from the bottom.
        await b.js(WATCH)
        await b.js("const i=document.getElementById('cc-input');i.value='hello from the test';i.dispatchEvent(new Event('input'));document.getElementById('cc-send').click()")
        await settle(b, 2)
        seen = await b.js("__stopWatch(),__seen")
        assert seen, "sending did not repaint the room — the check saw nothing"
        assert all(s["fromBottom"] <= 2 for s in seen), f"sending showed the room away from the newest message: {seen}"
        # Reading back when one arrives -- scrolled up with a real wheel: only the reader's own input unpins a room.
        r = await b.js("(()=>{const q=document.querySelector('.cc-messages').getBoundingClientRect();return {x:q.left+q.width/2,y:q.top+q.height/2};})()")
        for _ in range(3):
            await b.call('Input.dispatchMouseEvent', {'type': 'mouseWheel', 'x': r['x'], 'y': r['y'], 'deltaX': 0, 'deltaY': -300})
            await asyncio.sleep(.1)
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


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 390])
def test_a_room_with_images_stays_at_the_bottom_when_a_message_comes_in(width):
    """"if a user or myself send message, the scroll bar goes up": every repaint drew each encrypted
    image as a one-line "Decrypting…" placeholder — even decrypted ones — so the rebuilt room came back
    2,527 px short and grew again: measured, the scrollbar jumping up on every message."""
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=850, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        if width < 600:
            await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(IMAGES)
        await b.js(SETUP)
        await b.until("document.querySelectorAll('.cc-message').length>=50 || !!document.querySelector('[data-cc-channel]')")
        await b.js("(()=>{const c=document.querySelector('[data-cc-channel]');if(c&&!document.querySelector('.cc-app.show-chat .cc-message'))c.click();})()")
        await b.until("document.querySelectorAll('.cc-encrypted-attachment img').length>=10")
        await settle(b, 2.5)
        await b.js(WATCH)
        await b.js("const i=document.getElementById('cc-input');i.value='hello';i.dispatchEvent(new Event('input'));document.getElementById('cc-send').click()")
        await settle(b, 2)
        sent = await b.js("__stopWatch(),__seen")
        await b.js(WATCH)
        await b.js("__msgs.push({id:'n'+'y'.repeat(63),pubkey:'b'.repeat(64),text:'a new one',at:5000,kind:9,tags:[__imeta]});PCConcord.refreshActiveChannel&&PCConcord.refreshActiveChannel()")
        await b.until("__seen.length>0")
        await asyncio.sleep(.5)
        got = await b.js("__stopWatch(),__seen")
        for name, seen in (("sending", sent), ("receiving", got)):
            assert seen, f"{name} did not repaint the room — the check saw nothing"
            assert all(s["fromBottom"] <= 2 for s in seen), f"{name}: the rebuilt room was not at the bottom: {seen}"

    asyncio.run(desktop.with_browser("online", "", check))
