"""Making a member Moderator works in the real app, in a community from before split control keys.

"i can't even make people moderator! ... staff promotion requires binary key" (2026-10-16). PosterChan and Anime
were created before Concord's split control keys (09-15), so their bundles carry no control_pk/control_root. The
grant demanded a control-key hand-off such a community does not have (every member already holds its control
signer), so "Make moderator" failed in both, every time. This drives the shipped client with the REAL CORD reader
and a real community: right-click a member on Known members -> Make moderator -> the grant folds.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

ROOM = r"""(async()=>{
  const me=__PC.me().pubkey, R=window.PosterCordReader;
  const sign=async t=>__PC.signTemplate(t);
  const made=await PosterCord.createCommunity({owner:me,name:'Old room',relays:['wss://fixture.invalid'],base:'https://fixture.invalid',signEvent:sign});
  const bundle={...PosterCord.openInvite(made.url,made.events).bundle};
  if(%s){delete bundle.control_pk;delete bundle.control_root;}           // a community from before split keys
  else bundle.control_root=made.secrets.controlRoot;
  const names=['alice','bob','carol'];
  window.__msgs=names.map((n,i)=>{const sk=new Uint8Array(32).fill(i+40);const pk=NostrTools.getPublicKey(sk);
    Store.saveProfile(NostrTools.finalizeEvent({kind:0,created_at:1000,tags:[],content:JSON.stringify({name:n})},sk));
    return {id:'m'+String(i).padStart(3,'0')+'x'.repeat(60),pubkey:pk,text:'hello from '+n,at:1000+i,kind:9,tags:[]};});
  const room={name:'Old room',communityId:bundle.community_id,naddr:'fixture-old',url:made.url,
    channels:[{id:'fixture-general',name:'general'}],cord:{bundle,hydrated:true}};
  localStorage.setItem('pc.concord.rooms.v1.'+me,JSON.stringify([room]));localStorage.setItem('pc.concord.active.v1.'+me,'0');
  window.PosterCordReader={...R,inspectChat:async()=>({messages:window.__msgs.slice(),reactions:[],reactionIds:[]})};
  window.__published=[];
  __PC.relayPublishRoom=async(_r,ev)=>{window.__published.push(ev);return {ok:true,accepted:1,uncertain:false,msg:''};};
  window.__bundle=bundle;
  __PC.switchView('concord');
  return true;
})()"""


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("legacy", [True, False])
def test_make_moderator_from_known_members(legacy):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=1500, height=900, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(ROOM % ("true" if legacy else "false"))
        await b.js("PCConcord.render()")
        await b.until("document.querySelectorAll('.cc-members-pane .cc-member').length>=3")
        me = await b.js("__PC.me().pubkey")
        target = await b.js("[...document.querySelectorAll('.cc-members-pane .cc-member')].map(r=>r.dataset.ccMember).find(pk=>pk!==%r)" % me)
        x, y = await b.js("(()=>{const r=document.querySelector('.cc-members-pane .cc-member[data-cc-member=\"%s\"]').getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()" % target)
        await b.call("Input.dispatchMouseEvent", dict(type="mousePressed", x=x, y=y, button="right", buttons=2, clickCount=1))
        await b.call("Input.dispatchMouseEvent", dict(type="mouseReleased", x=x, y=y, button="right", buttons=0, clickCount=1))
        await b.until("!!document.querySelector('[data-cc-member-role=\"moderator\"]')")
        await b.js("document.querySelector('[data-cc-member-role=\"moderator\"]').click()")
        await b.until("[...document.querySelectorAll('.toast')].some(t=>/Role granted|was not changed/.test(t.textContent))")
        got['toast'] = await b.js("[...document.querySelectorAll('.toast')].map(t=>t.textContent).join(' | ')")
        got['grants'] = await b.js("(()=>{const i=window.PosterCordReader.inspectControl(window.__bundle,window.__published);"
                                   "const mod=i.roles.find(r=>r.name==='Moderator');return {mod:mod&&mod.id,held:i.grants[%r]||[]};})()" % target)

    asyncio.run(desktop.with_browser("online", "", check, ""))
    assert "Role granted" in got['toast'], got['toast']
    assert got['grants']['mod'] and got['grants']['mod'] in got['grants']['held'], got
