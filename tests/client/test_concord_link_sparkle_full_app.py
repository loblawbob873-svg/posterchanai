"""CONCORD: ✨ NEXT TO A LINK SUMMARIZES IT, AS IN TELEGRAM.

"Concord -> Add sparkle button next to links like we do for telegram for AI features like summarize
link etc". Telegram draws "✨ Summarize link/video" under any message carrying a link and hands that
message's links to chatassist.js. Here: the real client and the real Concord screen, two messages
from another member — one with a YouTube link, one without — at desktop and phone width. The button
must appear on the first only, read "video" for YouTube, and a tap must hand exactly that message's
text to the shared summarizer. With ✨ not allowed on this account there must be no button at all.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


LINK = 'look at this https://www.youtube.com/watch?v=dQw4w9WgXcQ wow'
ROOM = r'''(allowed=>{
  window.__sum=[];
  if(window.PCChatAssist){PCChatAssist.shown=()=>allowed;PCChatAssist.summarizeLinks=(t,m)=>{__sum.push([t,m]);};}
  const room={name:'Sparkle fixture',communityId:'c'.repeat(64),naddr:'fixture-community',
    channels:[{id:'fixture-general',name:'general'}],cord:{bundle:{relays:['wss://fixture.invalid']},hydrated:true}};
  localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify([room]));localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');
  window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[{id:'fixture-general',name:'general',streamPubkeys:[]}]}),
    inspectChat:async()=>({messages:[
      {id:'msg-link',pubkey:'b'.repeat(64),text:%s,at:Date.now()-60000,kind:9,tags:[]},
      {id:'msg-plain',pubkey:'b'.repeat(64),text:'just words here',at:Date.now()-30000,kind:9,tags:[]}],
      reactions:[],reactionIds:[]})};
  __PC.switchMessagesTab('concord');
})'''.replace('%s', repr(LINK))


# Not the windowed desktop: the plain app screen, as Telegram's ✨ is tested.
EXTRA = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"


async def _scene(b, width, allowed):
    await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 850, "deviceScaleFactor": 1, "mobile": width < 600})
    await desktop.login(b)
    await b.js("window.__publishOK=true")
    await b.js(ROOM + "(" + ("true" if allowed else "false") + ")")
    await b.until("!!document.querySelector('#cc-input')")
    await b.js("""(()=>{const app=document.querySelector('.cc-app');if(app&&!app.classList.contains('show-chat')){const ch=document.querySelector('.cc-channel');if(ch)ch.click();}})()""")
    await b.until("[...document.querySelectorAll('.cc-message')].some(m=>m.textContent.includes('just words'))")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_a_link_gets_sparkle_and_a_tap_summarizes_that_message(width):
    got = {}

    async def check(b):
        await _scene(b, width, True)
        got['buttons'] = await b.js("""[...document.querySelectorAll('.cc-message')].map(m=>{const s=m.querySelector('[data-cc-sum]');
          if(!s)return [m.dataset.messageId,null];const r=s.getBoundingClientRect();
          return [m.dataset.messageId,s.textContent.trim(),r.width>0&&r.height>0&&r.right<=innerWidth+1]})""")
        await b.js("document.querySelector('.cc-message[data-message-id=\"msg-link\"] [data-cc-sum]').click()")
        await asyncio.sleep(.2)
        got['sum'] = await b.js("__sum")

    asyncio.run(desktop.with_browser("online", "", check, EXTRA))
    rows = dict((k, v) for k, *v in got['buttons'])
    assert rows['msg-plain'] == [None], ("a message with no link got ✨", rows)
    assert rows['msg-link'][0] == '✨ Summarize video' and rows['msg-link'][1], ("no visible ✨ beside the link", rows)
    assert got['sum'] == [[LINK, 'concord']], got['sum']


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_no_sparkle_where_ai_is_not_allowed():
    got = {}

    async def check(b):
        await _scene(b, 1280, False)
        got['n'] = await b.js("document.querySelectorAll('[data-cc-sum]').length")

    asyncio.run(desktop.with_browser("online", "", check, EXTRA))
    assert got['n'] == 0
