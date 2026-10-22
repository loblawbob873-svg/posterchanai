"""A person blocked on this node's relay is not heard in its Concord communities (2026-10-21).

"if we block a user at the relay level, we need to make sure that they can't write to our concord rooms, basically
prevent abuse". No relay can stop it -- rooms live on outside relays and every message is a gift wrap under a
throwaway key -- so the members' clients do: they ask the node (members-only, only about authors they are showing)
and do not draw a blocked author's messages; an owner or moderator is offered the community ban, which removes the
person for every app. The node's answer is stubbed here; the endpoint itself is tests/test_concord_blocked_among.py.
"""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop

OTHER = 'e' * 64
NO_OS = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
ASK = r"""(()=>{const up=window.fetch;window.__asked=[];window.fetch=async(u,o)=>{
  if(String(u).includes('/client/concord/blocked-among')){const b=JSON.parse(o.body);__asked.push(b.pubkeys);
    return new Response(JSON.stringify({blocked:b.pubkeys.filter(x=>x==='OTHER')}),{status:200,headers:{'Content-Type':'application/json'}});}
  return up(u,o);};})();""".replace('OTHER', OTHER)


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_a_relay_blocked_author_is_not_drawn_and_the_owner_can_ban(width):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=850, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js(r'''(()=>{
          const me=__PC.me().pubkey,other='OTHER';
          const room={name:'Abuse fixture',communityId:'c'.repeat(64),naddr:'fixture-community',
            channels:[{id:'fixture-general',name:'general'}],cord:{bundle:{owner:me,relays:['wss://fixture.invalid']},hydrated:true}};
          localStorage.setItem('pc.concord.rooms.v1.'+me,JSON.stringify([room]));localStorage.setItem('pc.concord.active.v1.'+me,'0');
          const now=Date.now();
          window.PosterCordReader={
            inspectControl:()=>({controlPubkeys:[],owner:me,moderators:[],banned:[],channels:[{id:'fixture-general',name:'general',streamPubkeys:[]}],roles:[],grants:{}}),
            inspectChat:async()=>({messages:[
              {id:'1'.repeat(64),pubkey:me,text:'a normal message',at:now-60000,kind:9,tags:[]},
              {id:'2'.repeat(64),pubkey:other,text:'ABUSIVE SPAM',at:now-30000,kind:9,tags:[]}],reactions:[],reactionIds:[]}),
            inspectPinList:()=>({available:true,entries:[]}),canPinMessages:()=>true,authorityCitation:()=>[],inspectGuestbook:()=>({members:[me,other]})};
          __PC.switchMessagesTab('concord');
        })()'''.replace('OTHER', OTHER))
        await b.until("!!document.querySelector('.cc-channel-list')")
        await b.js("(()=>{const app=document.querySelector('.cc-app');if(app&&!app.classList.contains('show-chat')){const ch=document.querySelector('.cc-channel');if(ch)ch.click();}})()")
        await b.until("document.body.innerText.includes('a normal message')")
        for _ in range(60):
            if await b.js("!!document.querySelector('.cc-node-block-note')"):
                break
            await asyncio.sleep(.2)
        got['asked'] = await b.js("__asked")
        got['spam_shown'] = await b.js("document.body.innerText.includes('ABUSIVE SPAM')")
        got['note'] = await b.js("(document.querySelector('.cc-node-block-note')||{}).textContent||''")
        got['ban_btn'] = await b.js("!!document.querySelector('#cc-ban-node-blocked')")
        got['label'] = await b.js("[...document.querySelectorAll('.cc-member small')].map(x=>x.textContent).join('|')")
        got['fits'] = await b.js("document.documentElement.scrollWidth<=innerWidth+1")
    asyncio.run(desktop.with_browser('online', '', check, NO_OS + ASK))
    assert got['asked'] and any(OTHER in batch for batch in got['asked']), got['asked']
    assert all(len(x) == 64 for batch in got['asked'] for x in batch)
    assert got['spam_shown'] is False, "a relay-blocked author's message was drawn"
    assert '1 person here is blocked on this server' in got['note'] and got['ban_btn'], got
    assert 'blocked on this server' in got['label'], got['label']
    assert got['fits']
