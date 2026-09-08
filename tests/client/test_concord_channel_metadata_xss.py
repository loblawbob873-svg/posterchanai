"""Actual bundled renderer treats decrypted channel metadata as text, never markup."""
import asyncio
import json
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('payload', [
    '</b><img src="/fixture-missing-image" onerror="window.__channelXss=1"><b>',
    '"></textarea><img src="/fixture-missing-image" onerror="window.__channelXss=1"><textarea placeholder="',
])
def test_remote_channel_title_cannot_execute_in_bundled_renderer(payload):
    async def check(b):
        await desktop.login(b)
        await b.js('window.__payload='+json.dumps(payload))
        await b.js(r'''(()=>{
          const name=window.__payload;
          window.__maliciousChannel=name;
          const room={name:'Safe fixture',communityId:'c'.repeat(64),naddr:'fixture-community',
            channels:[{id:'safe',name:'general'}],cord:{bundle:{relays:['wss://fixture.invalid']},hydrated:true}};
          // Only the decrypt/transport boundary is synthetic. Actual hydration, channel selection,
          // rendering, DOM parsing, and browser event execution run unchanged.
          window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[
            {id:'safe',name:'general',streamPubkeys:[]},
            {id:'malicious',name,streamPubkeys:[]}]}),
            inspectChat:async()=>({messages:[],reactions:[],reactionIds:[]})};
          localStorage.setItem('pc.concord.invites',JSON.stringify([room]));
          localStorage.setItem('pc.concord.active','0');
          __PC.switchMessagesTab('concord');
        })()''')
        await b.until("[...document.querySelectorAll('[data-cc-channel]')].some(x=>x.dataset.ccChannel===__maliciousChannel)")
        await b.js("[...document.querySelectorAll('[data-cc-channel]')].find(x=>x.dataset.ccChannel===__maliciousChannel).click()")
        await asyncio.sleep(.5)
        assert not await b.js('window.__channelXss===1'), 'channel metadata executed in app origin'
        assert not await b.js("!!document.querySelector('.cc-conversation img[onerror]')")
        assert await b.js("document.querySelector('.cc-conversation header b').textContent===__maliciousChannel")
        assert await b.js("document.querySelector('.cc-welcome h2').textContent==='Welcome to #'+__maliciousChannel")
        assert await b.js("document.querySelector('#cc-input').getAttribute('placeholder')==='Message #'+__maliciousChannel")

    extra="localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))
