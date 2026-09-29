"""Communities: a post in a community you are NOT looking at lights that community's icon, at once.

Reported: "on desktop, I didn't see any way to know if a community has new posts". The rail's glow and
the Communities badge are computed from messages this device holds, and only the channel on screen
was ever fetched live — a community you were not looking at could never know it had anything new.
Every joined room is now subscribed; a post pushed by a relay for another room is filed under that
room and lights it ("90s? seems too long" — it is pushed, not polled). Real bundled client; the relay
and the CORD reader are fixtures.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SETUP = r"""(()=>{
  const me=__PC.me().pubkey, A='a'.repeat(64), B='b'.repeat(64);
  const room=(name,id,stream)=>({name,communityId:id.repeat(64),naddr:'fixture-'+id,url:'https://fixture.invalid/invite/'+id+'#s',
    channels:[{id:id+'-general',name:'general',streamPubkeys:[stream]}],cord:{bundle:{owner:me,relays:['wss://fixture.invalid']},hydrated:true}});
  localStorage.setItem('pc.concord.invites',JSON.stringify([room('Room A','c','a'.repeat(64)),room('Room B','d','b'.repeat(64))]));
  localStorage.setItem('pc.concord.active','0');
  window.__subs=[];
  window.PCConcordCache={get:async k=>String(k).includes('control')?[{id:'ctl',kind:1059,pubkey:'9'.repeat(64),created_at:1,tags:[],content:''}]:[],
    put:async()=>{},page:async()=>[],sweepExpired:async()=>0};
  window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[]}),
    inspectChat:async(_b,_c,chan,wraps)=>({messages:(wraps||[]).filter(w=>w.id!=='ctl').map(w=>({id:w.id.repeat(2).slice(0,64),pubkey:'7'.repeat(64),text:'NEW IN '+chan,at:Date.now(),kind:9,tags:[]})),reactions:[],reactionIds:[]})};
  const R=window.Relay;
  R.subscribeFrom=(urls,filters,opts)=>{__subs.push({filters,opts});const off=()=>{};off.hasTargets=false;return off;};
  __PC.switchView('concord');
})()"""

RAIL = r"""(()=>[...document.querySelectorAll('.cc-communities .cc-server[data-cc-server]')].map(b=>({name:b.getAttribute('title'),unread:b.classList.contains('unread')})))()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_post_in_another_community_lights_its_icon():
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=1400, height=900, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(SETUP)
        await b.until("document.querySelectorAll('.cc-communities .cc-server[data-cc-server]').length===2")
        assert [r["unread"] for r in await b.js(RAIL)] == [False, False]
        # Room B is subscribed even though Room A is the one on screen.
        await b.until("__subs.some(s=>s.filters.some(f=>(f.authors||[]).includes('b'.repeat(64))))")
        await b.js("(()=>{const s=__subs.find(s=>s.filters.some(f=>(f.authors||[]).includes('b'.repeat(64))));"
                   "s.opts.onEvent({id:'e'.repeat(64),kind:1059,pubkey:'b'.repeat(64),created_at:Math.floor(Date.now()/1000),tags:[],content:''});})()")
        await b.until("(" + RAIL + ").find(r=>r.name==='Room B').unread")
        rail = await b.js(RAIL)
        assert [r["unread"] for r in rail] == [False, True], rail
        assert not await b.js('__errors'), await b.js('__errors')

    asyncio.run(desktop.with_browser("online", "", check))
