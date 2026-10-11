"""OPENING A CONCORD MENU ON A PHONE DOES NOT REBUILD THE CONVERSATION.

Reported from the APK: "going through the menus is clunky, it's not smooth at all." Measured first
(see `measure()` below, phone size 390x844, mobile emulation, CPU throttled 4x through CDP, a room
holding the 200 messages a local community keeps): every drawer toggle went through `render()`, which
rebuilds the WHOLE workspace -- the rail, the channel list, every message card and the composer -- and
rebinds every handler, to flip one class. That is the jank: one tap = one long task proportional to
the size of the conversation.

The budget asserted here is structural, not a stopwatch alone: opening or closing a menu must not
REPLACE a single message card (the cards before and after are the same nodes), and under the 4x
throttle it must not produce a long task over LONG_TASK_BUDGET_MS. Twenty-five idle seconds (one
background sweep of the other rooms) must not rebuild it either. The numbers for each action are printed so a
regression says which menu and by how much.
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# At 4x throttle a full workspace render of this room measured 170-280 ms; what is left of a menu tap
# after the fixes (a dialog's own style/layout) measured 40-60 ms, with 100 ms seen once on a machine
# busy with a deploy gate. The budget sits between the two, with room for a loaded runner.
LONG_TASK_BUDGET_MS = 150

NO_OS = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse("
         "localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")

ROOM = r'''(()=>{const me=__PC.me().pubkey;
const room={name:'Smooth menus',icon:'🕊',description:'menus',local:true,naddr:'naddr-smooth-menus',communityId:'e'.repeat(64),
  channels:[{name:'general',private:false,id:'general-id'},{name:'support',private:false,id:'support-id'},{name:'random',private:false,id:'random-id'}]};
localStorage.setItem('pc.concord.rooms.v1.'+me,JSON.stringify([room]));
localStorage.setItem('pc.concord.active.v1.'+me,'0');
const authors=Array.from({length:30},(_,i)=>(i===0?me:(i.toString(16).padStart(2,'0')).repeat(32)));
const msgs=Array.from({length:200},(_,i)=>({id:'m-'+i,pubkey:authors[i%30],by:'Member '+(i%30),at:Date.now()-(200-i)*60000,
  text:(i%7===0?'A longer message with a link https://example.test/path/'+i+' and enough words to wrap across two or three lines on a phone. ':'')+'Message '+i,
  tags:[],reactions:i%5===0?{'👍':[authors[(i+1)%30]]}:{}}));
localStorage.setItem('pc.concord.test.'+room.naddr,JSON.stringify(msgs));return true;})()'''

MEASURE = r'''window.__measure=async(sel,label)=>{
  const el=document.querySelector(sel);if(!el)return {label,missing:sel};
  const before=new Set(document.querySelectorAll('.cc-message'));
  const longs=[];let po=null;try{po=new PerformanceObserver(l=>{for(const e of l.getEntries())longs.push(Math.round(e.duration));});po.observe({type:'longtask'});}catch(_){}
  const t0=performance.now();el.click();const sync=performance.now()-t0;
  await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));const frame=performance.now()-t0;
  await new Promise(r=>setTimeout(r,1200));if(po)po.disconnect();
  const after=[...document.querySelectorAll('.cc-message')];
  const replaced=[...before].filter(n=>!n.isConnected).length;
  return {label,sync:Math.round(sync),frame:Math.round(frame),longs,cards:after.length,replaced};
};true'''


async def measure_menus(b):
    await b.call('Emulation.setDeviceMetricsOverride', dict(width=390, height=844, deviceScaleFactor=2, mobile=True))
    await desktop.login(b)
    await b.js(ROOM)
    await b.js("__PC.switchView('concord')")
    await b.until("!!window.PCConcord && document.body.classList.contains('concord-view')")
    await b.js("PCConcord.render()")
    await b.until("!!document.querySelector('[data-cc-server]')")
    await b.js("document.querySelector('[data-cc-server]').click()")
    await b.until("!!document.querySelector('[data-cc-channel=\"general\"]')")
    await b.js("document.querySelector('[data-cc-channel=\"general\"]').click()")
    await b.until("document.querySelectorAll('.cc-message').length>=100")
    # Let the room's own first-visit work (custom emoji, profiles, icons) land and repaint BEFORE
    # measuring: a data repaint is not a menu tap, and this test is about the taps.
    await b.js("window.__quiet=()=>new Promise(r=>{let last=performance.now();const mo=new MutationObserver(()=>{last=performance.now();});"
               "mo.observe(document.querySelector('#feed'),{childList:true,subtree:true});"
               "const t=setInterval(()=>{if(performance.now()-last>1500){clearInterval(t);mo.disconnect();r(true);}},100);"
               "setTimeout(()=>{clearInterval(t);mo.disconnect();r(false);},15000);})")
    await b.js('__quiet()')
    await b.js(MEASURE)
    await b.call('Emulation.setCPUThrottlingRate', {'rate': 4})
    steps = [
        ('.cc-messages', 'tap the conversation (baseline)'),
        ('#cc-back-channels', 'open channel drawer'),
        ('#cc-drawer-backdrop', 'close channel drawer'),
        ('#cc-back-channels', 'open channel drawer again'),
        ('#cc-drawer-backdrop', 'close channel drawer again'),
        ('.cc-message:last-of-type [data-cc-actions]', 'open message actions'),
        ('.cc-message:last-of-type [data-cc-actions]', 'close message actions'),
        ('#cc-head-more', 'open header menu'),
        ('#cc-members', 'open members'),
        ('#cc-members-close', 'close members'),
        ('#cc-edit-icon', 'open community settings'),
        ('#cc-settings-cancel', 'close community settings'),
    ]
    out = []
    for sel, label in steps:
        # A popover from the previous step is dismissed the way a person does it: tap elsewhere.
        if label == 'open members':
            await b.js("document.querySelector('.menu-pop')&&document.body.click()")
        out.append(await b.js('__measure(%s,%s)' % (json.dumps(sel), json.dumps(label))))
    # Doing nothing must not rebuild the conversation either. The background sweep of the other rooms
    # (every 20 s) ended in an UNCONDITIONAL full render, new messages or not -- the same 170-190 ms
    # rebuild, on a timer, under whatever menu happened to be open.
    await b.js("window.__idle=new Set(document.querySelectorAll('.cc-message'));window.__idleLongs=[];"
               "window.__idleObs=new PerformanceObserver(l=>{for(const e of l.getEntries())__idleLongs.push(Math.round(e.duration));});"
               "__idleObs.observe({type:'longtask'})")
    await asyncio.sleep(25)
    out.append({'label': 'idle 25 s (a background sweep runs)', 'longs': await b.js('__idleObs.disconnect(),__idleLongs'),
                'cards': await b.js("document.querySelectorAll('.cc-message').length"),
                'replaced': await b.js("[...__idle].filter(n=>!n.isConnected).length")})
    await b.call('Emulation.setCPUThrottlingRate', {'rate': 1})
    return out


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_concord_menus_do_not_rebuild_the_conversation_on_a_phone():
    got = {}

    async def check(b):
        got['steps'] = await measure_menus(b)
    asyncio.run(desktop.with_browser('online', '', check, NO_OS))
    steps = got['steps']
    report = '\n'.join(json.dumps(s) for s in steps)
    if os.environ.get('PC_PRINT_MEASURE'):
        print(report)
    missing = [s for s in steps if 'missing' in s]
    assert not missing, 'a menu control was not on screen:\n' + report
    rebuilt = [s for s in steps if s['replaced']]
    assert not rebuilt, 'opening/closing a menu rebuilt the message list:\n' + report
    slow = [s for s in steps if any(t > LONG_TASK_BUDGET_MS for t in s['longs'])]
    assert not slow, f'a menu tap produced a long task over {LONG_TASK_BUDGET_MS} ms at 4x CPU throttle:\n' + report
