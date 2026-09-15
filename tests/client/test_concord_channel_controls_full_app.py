"""Concord's channel action is reachable in its header, with invite access preserved.

Real bundled DOM and pointer input; CORD encryption and network are fixtures.
"""
import asyncio
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def click(browser, selector):
    import json
    target = await browser.js('''(()=>{const button=document.querySelector('''+json.dumps(selector)+''');
      const r=button.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;
      return {x,y,hit:r.width>0&&r.height>0&&button.contains(document.elementFromPoint(x,y))};})()''')
    assert target['hit'], f'{selector} is hidden, clipped or covered'
    for kind in ('mousePressed', 'mouseReleased'):
        await browser.call('Input.dispatchMouseEvent', dict(type=kind, x=target['x'], y=target['y'], button='left', clickCount=1))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
@pytest.mark.parametrize('role', ['owner', 'member', 'local', 'nip29'])
def test_concord_header_channel_action_and_invite_access(width, role):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=850, deviceScaleFactor=1, mobile=width<600))
        await desktop.login(b)
        await b.js(r'''(()=>{
          const room={name:'Channel controls',communityId:'c'.repeat(64),naddr:'fixture-community',
            url:'https://fixture.invalid/invite/fixture#secret',
            channels:[{id:'fixture-general',name:'general'}],
            cord:{bundle:{owner:__PC.me().pubkey,relays:['wss://fixture.invalid']},hydrated:true}};
          const role='ROLE';
          if(role==='member')room.cord.bundle.owner='d'.repeat(64);
          if(role==='local')room.local=true;
          if(role==='nip29')room.protocol='nip29';
          localStorage.setItem('pc.concord.invites',JSON.stringify([room]));localStorage.setItem('pc.concord.active','0');
          window.__channelWrites=0;
          window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[{id:'fixture-general',name:'general',streamPubkeys:[]}]}),
            inspectChat:async()=>({messages:[],reactions:[],reactionIds:[]}),
            createChannelWrap:async()=>{__channelWrites++;throw Error('unexpected write after cancellation');}};
          __PC.switchMessagesTab('concord');
        })()'''.replace('ROLE', role))
        await b.until("!!document.querySelector('.cc-channel-list')")
        assert await b.js("document.querySelector('.cc-section-head').textContent.trim()") == 'TEXT CHANNELS'
        assert not await b.js("!!document.querySelector('.cc-section-head button')")
        assert not await b.js("!!document.querySelector('#cc-invite')"), 'redundant invite plus remains'
        assert await b.js("!!document.querySelector('#cc-copy-link[aria-label=\"Copy room invite link\"]')")
        if role == 'owner':
            assert await b.js("document.querySelectorAll('#cc-add-channel').length") == 1
            assert await b.js("document.querySelector('#cc-leave-room').nextElementSibling?.id") == 'cc-add-channel'
            assert await b.js("document.querySelector('#cc-add-channel').getAttribute('aria-label')") == 'New channel'
            await click(b, '#cc-add-channel')
            await b.until("document.querySelector('.uiconfirm-msg')?.textContent==='Name the new channel'")
            assert await b.js("document.querySelector('#cc-join').classList.contains('hidden')")
            await click(b, '.uiconfirm [data-uc="0"]')
            await b.until("!document.querySelector('.uiconfirm')")
            assert await b.js('__channelWrites') == 0
            assert await b.js("JSON.parse(localStorage.getItem('pc.concord.invites'))[0].channels.length") == 1
        else:
            assert not await b.js("!!document.querySelector('#cc-add-channel')"), 'unsupported channel control offered'
        # The separate community-rail action still opens the invite/create flow.
        await click(b, '#cc-add')
        await b.until("!document.querySelector('#cc-join').classList.contains('hidden')")
        assert await b.js("!!document.querySelector('#cc-invite-url')")
        await click(b, '#cc-join-cancel')
        if width < 600:
            await click(b, '.cc-channel')
        await click(b, '#cc-copy-link')
        await b.until("window.__copiedInvite==='https://fixture.invalid/invite/fixture#secret'")
    extra="localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{window.__copiedInvite=value;}}});"
    asyncio.run(desktop.with_browser('online', '', check, extra))
