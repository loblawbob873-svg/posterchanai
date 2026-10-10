"""REACTING TO A MESSAGE IN COMMUNITIES, END TO END.

Reported: "Concord Emoji reaction on chat message is broken, nothing happens!" Every reaction test
before this one ran a piece of the flow in node (the picker choice, the NIP-30 tag); none pressed ☺
on a message in the real screen. This does: a message from ANOTHER member arrives through the normal
read path (inspectChat → the timeline merge), ☺ is pressed, an emoji is chosen in the app's real
picker, and the reaction must be signed and published as a kind-7 naming that message — and drawn
on it. A failure must be said, never swallowed.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


ROOM = r'''(()=>{
  const room={name:'React fixture',communityId:'c'.repeat(64),naddr:'fixture-community',
    channels:[{id:'fixture-general',name:'general'}],cord:{bundle:{relays:['wss://fixture.invalid']},hydrated:true}};
  localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify([room]));localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');
  window.__wraps=[];
  window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[{id:'fixture-general',name:'general',streamPubkeys:[]}]}),
    inspectChat:async()=>({messages:[{id:'msg-from-bob',pubkey:'b'.repeat(64),text:'hello from bob',at:Date.now()-60000,kind:9,tags:[]}],
                           reactions:[],reactionIds:[]}),
    createChatWrap:async(_bundle,_wraps,_channel,text,owner,sign,tags,kind)=>{
      const sealed=await sign({kind:20013,created_at:Math.floor(Date.now()/1000),content:'encrypted-fixture',tags:[]});
      __wraps.push({text,tags,kind});
      return{rumorId:sealed.id,wrap:{...sealed,kind:1059},ms:Date.now()};
    }};
  __PC.switchMessagesTab('concord');
})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_reacting_to_a_message_publishes_and_shows_the_reaction(width):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 850, "deviceScaleFactor": 1, "mobile": width < 600})
        await desktop.login(b)
        await b.js("(()=>{window.__toasts=[];const t=__PC.toast;__PC.toast=(m)=>{__toasts.push(String(m));try{t(m)}catch(_){}};window.__publishOK=true;})()")
        await b.js(ROOM)
        await b.until("!!document.querySelector('#cc-input')")
        await b.js("""(()=>{const app=document.querySelector('.cc-app');if(app&&!app.classList.contains('show-chat')){const ch=document.querySelector('.cc-channel');if(ch)ch.click();}})()""")
        await b.until("[...document.querySelectorAll('.cc-message')].some(m=>m.textContent.includes('hello from bob'))")
        # Hover/tap the message the way a person does, then ☺.
        await b.js("""(()=>{const m=[...document.querySelectorAll('.cc-message')].find(x=>x.textContent.includes('hello from bob'));
          m.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));const t=m.querySelector('[data-cc-actions]');if(t)t.click();
          m.querySelector('[data-cc-react]').click();})()""")
        await b.until("!!document.querySelector('.emoji-pop [data-e], .cc-reaction-picker button')")
        # The app's picker takes a pick on MOUSEDOWN (so a text box keeps its caret); press it the way
        # a mouse does -- down, up, click -- not with a bare click() that no mouse ever produces.
        got['emoji'] = await b.js("""(()=>{const e=document.querySelector('.emoji-pop [data-e], .cc-reaction-picker button');
          const v=e.dataset.e||e.dataset.emoji||e.textContent.trim();
          for(const t of ['mousedown','mouseup','click'])e.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));
          return v;})()""")
        await asyncio.sleep(1.5)
        got['wraps'] = await b.js("__wraps")
        got['chip'] = await b.js("""(()=>{const m=[...document.querySelectorAll('.cc-message')].find(x=>x.textContent.includes('hello from bob'));
          const c=m&&m.querySelector('.cc-reaction');return c?{mine:c.classList.contains('mine'),n:(c.querySelector('b')||{}).textContent}:null;})()""")
        got['toasts'] = await b.js("__toasts")
    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))
    reactions = [w for w in got['wraps'] if w['kind'] == 7]
    assert reactions, ('nothing was published for the reaction', got)
    assert ['e', 'msg-from-bob'] in [t[:2] for t in reactions[0]['tags']], got
    assert got['chip'] and got['chip']['mine'] and got['chip']['n'] == '1', got


BUSY = r'''(async()=>{
  // What a busy room does while the picker is open: its chat list keeps re-pinning itself to the
  // newest message, and every one of those is a `scroll` event nobody made.
  const list=[...document.querySelectorAll('.cc-message')].map(m=>{let e=m.parentElement;while(e&&e!==document.body){const cs=getComputedStyle(e);if(/(auto|scroll)/.test(cs.overflowY))return e;e=e.parentElement;}return null;}).find(Boolean)||document.scrollingElement;
  for(let i=0;i<12;i++){ list.scrollTop=(i%2)?0:list.scrollHeight; list.dispatchEvent(new Event('scroll')); document.getElementById('feed')?.dispatchEvent(new Event('scroll')); await new Promise(r=>setTimeout(r,100)); }
  return !!document.querySelector('.emoji-pop');
})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_picker_survives_a_busy_room_and_a_person_scrolling_still_closes_it():
    """"the emoji picker disappeared quickly" -- measured on the desktop: the picker closed ~100ms after
    opening, from its scroll-to-close handler, because a busy room re-pins its chat list on every
    refresh. The app scrolling itself must not dismiss it; the person scrolling still does."""
    got = {}

    async def open_picker(b):
        await b.js("""(()=>{const m=[...document.querySelectorAll('.cc-message')].find(x=>x.textContent.includes('hello from bob'));
          const t=m.querySelector('[data-cc-actions]');if(t)t.click();m.querySelector('[data-cc-react]').click();})()""")
        await b.until("!!document.querySelector('.emoji-pop [data-e]')")

    async def check(b):
        await desktop.login(b)
        await b.js("(()=>{window.__toasts=[];window.__publishOK=true;})()")
        await b.js(ROOM)
        await b.until("!!document.querySelector('#cc-input')")
        await b.until("[...document.querySelectorAll('.cc-message')].some(m=>m.textContent.includes('hello from bob'))")
        await open_picker(b)
        got['survived'] = await b.js(BUSY)
        if got['survived']:
            await b.js("""(()=>{const e=document.querySelector('.emoji-pop [data-e]');
              for(const t of ['mousedown','mouseup','click'])e.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}));})()""")
            await asyncio.sleep(1.2)
        got['published'] = await b.js("__wraps.filter(w=>w.kind===7).length")
        # The person scrolls: a wheel over the chat, then the scroll it causes.
        await open_picker(b)
        await b.js("""(()=>{const m=document.querySelector('.cc-message');m.dispatchEvent(new WheelEvent('wheel',{bubbles:true,deltaY:120}));
          document.getElementById('feed')?.dispatchEvent(new Event('scroll'));document.dispatchEvent(new Event('scroll'));})()""")
        await asyncio.sleep(.3)
        got['closed_by_person'] = await b.js("!document.querySelector('.emoji-pop')")
    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))
    assert got['survived'], ('the picker closed while the room scrolled itself', got)
    assert got['published'] >= 1, got
    assert got['closed_by_person'], ('a person scrolling no longer closes the picker', got)
