"""CONCORD CALLS FROM THE ROOM ITSELF: the 📞 in the room header starts the call for THIS channel.

"we need test cases around Concord Calls". Every Concord call test before this injected cord-call.js and
drove it directly (encryption before the microphone, the lifecycle races, the transport scoping) — none
ever pressed the room's own Call button, so the part a person touches was untested: that the button is
on screen at phone width (the phone header was just reorganised behind ⋯), that it opens a call for the
channel on screen with that channel's voice keys, that a call which cannot start SAYS so and leaves the
room usable, and that a NIP-29 room with nobody else in it says that instead of ringing no one.
The real client and the real Concord screen; the SFU module is the boundary (window.PCCordCall).
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_channel_controls_full_app import click


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


ROOM = r'''((kind, openMode)=>{
  const room={name:'Call fixture',communityId:'c'.repeat(64),naddr:'fixture-community',
    channels:[{id:'fixture-general',name:'general'},{id:'fixture-voice',name:'hangout'}],
    cord:{bundle:{owner:__PC.me().pubkey,relays:['wss://fixture.invalid'],epoch:1},hydrated:true}};
  if(kind==='nip29')room.protocol='nip29';
  localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify([room]));localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');
  window.__opened=null;window.__group=null;
  window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[{id:'fixture-general',name:'general',streamPubkeys:[]},{id:'fixture-voice',name:'hangout',streamPubkeys:[]}]}),
    inspectChat:async()=>({messages:[],reactions:[],reactionIds:[]}),
    voiceMaterial:(b,c,ch)=>({room:'voice:'+ch+':'+b.epoch,stream:'stream:'+ch})};
  window.PCCordVoice={};
  window.PCCordCall={open:async ctx=>{ if(openMode==='fail') throw new Error('The call server could not be reached');
    window.__opened={name:ctx.name,material:ctx.material(),current:ctx.current()}; return ctx; }};
  __PC.startGroupCall=(peers,video)=>{window.__group={peers,video};};
  __PC.switchMessagesTab('concord');
})'''

EXTRA = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"


async def _in_room(b, width, kind='cord', open_mode='ok', channel='general'):
    await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=850, deviceScaleFactor=1, mobile=width < 600))
    await desktop.login(b)
    await b.js(ROOM + f"('{kind}','{open_mode}')")
    await b.until("!!document.querySelector('.cc-channel-list')")
    await b.js(f"""(()=>{{const ch=[...document.querySelectorAll('.cc-channel')].find(c=>c.textContent.includes('{channel}'));if(ch)ch.click();}})()""")
    await b.until("!!document.querySelector('#cc-call')")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [360, 412, 1280])
def test_the_room_call_button_starts_the_call_for_the_channel_on_screen(width):
    got = {}

    async def check(b):
        await _in_room(b, width, channel='hangout')
        if width < 370:
            # Too narrow for 📞 in the header: it must be in ⋯ instead — never simply gone.
            await click(b, '#cc-head-more')
            await b.until("[...document.querySelectorAll('.menu-pop button')].some(x=>/call/i.test(x.textContent))")
            await b.js("[...document.querySelectorAll('.menu-pop button')].find(x=>/call/i.test(x.textContent)).click()")
        else:
            await click(b, '#cc-call')        # real pointer input; fails if hidden, clipped or covered
        await b.until("!!window.__opened")
        got['opened'] = await b.js("__opened")
        got['errors'] = await b.js("__errors")

    asyncio.run(desktop.with_browser('online', '', check, EXTRA))
    o = got['opened']
    assert o['name'] == 'hangout', ("the call is not for the channel on screen", o)
    assert o['material']['room'] == 'voice:fixture-voice:1', ("the call is not keyed to this channel", o)
    assert o['current'] is True
    assert not got['errors'], got['errors']


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_a_call_that_cannot_start_says_why_and_the_room_still_works(width):
    got = {}

    async def check(b):
        await _in_room(b, width, open_mode='fail')
        await click(b, '#cc-call')
        await b.until("[...document.querySelectorAll('.toast')].some(t=>/call server could not be reached/.test(t.textContent))")
        got['composer'] = await b.js("(()=>{const i=document.querySelector('#cc-input');const r=i&&i.getBoundingClientRect();return !!r&&r.height>0;})()")
        got['errors'] = await b.js("__errors")

    asyncio.run(desktop.with_browser('online', '', check, EXTRA))
    assert got['composer'], "the room stopped working after the call failed"
    assert not got['errors'], got['errors']


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_nip29_room_with_nobody_else_says_so_and_rings_no_one():
    got = {}

    async def check(b):
        await _in_room(b, 1280, kind='nip29')
        await click(b, '#cc-call')
        await b.until("[...document.querySelectorAll('.toast')].some(t=>/No other community members/.test(t.textContent))")
        got['group'] = await b.js("window.__group")
        got['opened'] = await b.js("window.__opened")

    asyncio.run(desktop.with_browser('online', '', check, EXTRA))
    assert got['group'] is None and got['opened'] is None, got
