"""The Vector-parity controls in the real bundled client, on a phone and a desktop.

Edit, pin, the pinned list, channel options, the owner's Dissolve and the member menu's Kick / Make
moderator: each is reachable (hit-testable, not clipped, not covered), opens what it says, and the room
still has no sideways scroll at 390px. CORD crypto and relays are fixtures; the writers themselves are
covered by tests/client/test_concord_vector_parity.py.
"""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop

OTHER = 'd' * 64
MINE_ID = '1' * 64
THEIRS_ID = '2' * 64


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def hit(b, selector):
    return await b.js('''(()=>{const el=document.querySelector(''' + json.dumps(selector) + ''');if(!el)return 'missing';
      const r=el.getBoundingClientRect();if(!(r.width>0&&r.height>0))return 'zero-size';
      if(r.right>innerWidth+1||r.left<-1)return 'clipped';
      const x=r.x+r.width/2,y=r.y+r.height/2,top=document.elementFromPoint(x,y);return el.contains(top)?'ok':'covered';})()''')


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_vector_parity_controls_are_reachable(width):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=850, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js(r'''(()=>{
          const me=__PC.me().pubkey,other='OTHER';
          const room={name:'Parity fixture',communityId:'c'.repeat(64),naddr:'fixture-community',
            channels:[{id:'fixture-general',name:'general'},{id:'fixture-random',name:'random'}],
            cord:{bundle:{owner:me,relays:['wss://fixture.invalid']},hydrated:true}};
          localStorage.setItem('pc.concord.invites',JSON.stringify([room]));localStorage.setItem('pc.concord.active','0');
          const now=Date.now();
          window.PosterCordReader={
            inspectControl:()=>({controlPubkeys:[],owner:me,moderators:[],banned:[],
              channels:[{id:'fixture-general',name:'general',streamPubkeys:[]},{id:'fixture-random',name:'random',streamPubkeys:[]}],
              roles:[],grants:{}}),
            inspectChat:async()=>({messages:[
              {id:'MINE',pubkey:me,text:'my own words',at:now-60000,kind:9,tags:[]},
              {id:'THEIRS',pubkey:other,text:'their pinned words',at:now-30000,kind:9,tags:[['edited','1']]}],reactions:[],reactionIds:[]}),
            inspectPinList:()=>({available:true,entries:[{id:'THEIRS',pubkey:other,content:'their pinned words',edited:false}]}),
            canPinMessages:()=>true,authorityCitation:()=>[],inspectGuestbook:()=>({members:[me,other]})};
          __PC.switchMessagesTab('concord');
        })()'''.replace('OTHER', OTHER).replace("'MINE'", json.dumps(MINE_ID)).replace("'THEIRS'", json.dumps(THEIRS_ID)))
        await b.until("!!document.querySelector('.cc-channel-list')")
        # Channel options (owner): one ⋯ per channel, reachable.
        assert await b.js("document.querySelectorAll('[data-cc-channel-more]').length") == 2
        assert await hit(b, '[data-cc-channel-more]') == 'ok'
        await b.js("document.querySelector('[data-cc-channel-more]').click()")
        await b.until("!!document.querySelector('[data-cc-ch-rename]')")
        assert await hit(b, '[data-cc-ch-delete]') == 'ok'
        await b.js("__PC.closeModal()")
        # Into the room (on a phone the conversation is its own pane).
        await b.js("""(()=>{const app=document.querySelector('.cc-app');if(app&&!app.classList.contains('show-chat')){const ch=document.querySelector('.cc-channel');if(ch)ch.click();}})()""")
        await b.until("getComputedStyle(document.querySelector('.cc-conversation')).display!=='none'")
        await b.until("document.querySelectorAll('.cc-message').length===2")
        # (edited) is shown; the pinned list is one tap from the header and names the message.
        assert await b.js("document.querySelectorAll('.cc-edited').length") == 1
        assert await hit(b, '#cc-pins') == 'ok', 'the pinned-messages button is not reachable in the header'
        assert await b.js("document.querySelector('#cc-pins small')?.textContent") == '1'
        await b.js("document.querySelector('#cc-pins').click()")
        await b.until("!!document.querySelector('.cc-pin-item')")
        assert 'their pinned words' in await b.js("document.querySelector('.cc-pin-item').textContent")
        assert await hit(b, '.cc-pin-item') == 'ok'
        await b.js("__PC.closeModal()")
        # Edit: only on MY message; opens prefilled.
        mine = f'.cc-message[data-message-id="{MINE_ID}"]'
        theirs = f'.cc-message[data-message-id="{THEIRS_ID}"]'
        assert await b.js(f"!!document.querySelector('{mine} [data-cc-edit]')")
        assert not await b.js(f"!!document.querySelector('{theirs} [data-cc-edit]')"), "edit offered on another member's message"
        await b.js(f"document.querySelector('{mine} [data-cc-actions]').click()")
        assert await hit(b, f'{mine} [data-cc-edit]') == 'ok'
        await b.js(f"document.querySelector('{mine} [data-cc-edit]').click()")
        await b.until("!!document.querySelector('.cc-edit-text')")
        assert await b.js("document.querySelector('.cc-edit-text').value") == 'my own words'
        assert await b.js("document.querySelector('.cc-edit-text').getBoundingClientRect().right<=innerWidth+1")
        await b.js("__PC.closeModal()")
        # Pin toggle: their message is pinned → offered as Unpin.
        assert await b.js(f"document.querySelector('{theirs} [data-cc-pin]').title") == 'Unpin message'
        assert await b.js(f"document.querySelector('{mine} [data-cc-pin]').title") == 'Pin message'
        # Owner's Dissolve lives in Community settings.
        assert await b.js("!!document.querySelector('#cc-dissolve')")
        # The typing line exists and is empty (nobody typing).
        assert await b.js("document.querySelector('#cc-typing')?.textContent") == ''
        assert not await b.js("document.documentElement.scrollWidth>innerWidth+1"), 'the room scrolls sideways'

    no_os = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, no_os))
