"""The windowed desktop must not pay, at rest or while scrolling, for paint nobody can see.

Reported on an Android tablet: "desktop performance is very slow". Profiled against the shipped bundle
at 1280x800 (mobile, touch) with 6x CPU throttle and a 240-post timeline in a window, two costs
dominated and both were invisible:

* the classic sidebar's connection dot pulses with an INFINITE CSS animation while `#os-desk` covers it
  completely — 60 style recalcs a second for ever, 0.93s of main-thread work per 3s idle (0.04s without);
* the sticky timeline tabs paint `--canvas` with `background-attachment:fixed`, which repaints the bar on
  every scroll frame and takes the scroller off the compositor — 4.5s of work per scripted scroll
  (1.1s without).

Timings are the wrong thing to gate on (they move with the runner), so this pins the two RULES those
numbers came from, plus one count that is exact rather than timed: a desktop at rest recalculates
style ~0 times a second, and a covered infinite animation makes that ~60.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


INIT = r'''
delete window.pcShell; delete window.pcPopup;
{const s=JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}');s.osMode=true;
 localStorage.setItem('pc_nostr_settings',JSON.stringify(s));}
document.addEventListener('DOMContentLoaded',()=>{try{
  const keys=[1,2,3].map(n=>new Uint8Array(32).fill(n+20)),now=Math.floor(Date.now()/1000);
  window.__events=Array.from({length:60},(_,i)=>NostrTools.finalizeEvent({kind:1,created_at:now-i*30,
    content:'Tablet desktop post '+i+' long enough to wrap across a couple of lines in a window.',tags:[]},keys[i%3]));
}catch(_){}});
'''

# Every INFINITE animation that is running must be on something a person can see. "Covered" is asked
# the way the eye asks it: what is on top at the element's centre.
COVERED_INFINITE = r'''(()=>document.getAnimations().filter(a=>{
  if(a.playState!=='running') return false;
  const t=a.effect&&a.effect.target, it=a.effect&&a.effect.getTiming().iterations;
  if(!t||it!==Infinity||a.effect.pseudoElement) return false;
  const r=t.getBoundingClientRect(); if(!r.width||!r.height) return true;
  const top=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
  return !top||!(t===top||t.contains(top)||top.contains(t));
}).map(a=>(a.animationName||'?')+' on '+a.effect.target.tagName+'.'+String(a.effect.target.className)))()'''

FIXED_IN_WINDOWS = r'''(()=>[...document.querySelectorAll('.osw *')].filter(el=>
  /fixed/.test(getComputedStyle(el).backgroundAttachment)&&getComputedStyle(el).backgroundImage!=='none'
).map(el=>el.tagName+'.'+String(el.className).slice(0,40)))()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_tablet_desktop_is_quiet_at_rest_and_scrolls_on_the_compositor():
    async def check(b):
        await desktop.login(b)
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 1280, 'height': 800, 'deviceScaleFactor': 2, 'mobile': True})
        await b.call('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 5})
        await b.call('Page.reload')
        await asyncio.sleep(.3)
        await b.until('!!window.__PC_BOOTED && PCOS.isOn()')
        await b.js("__PC.switchView('global')")
        await b.until("PCOS.windows().some(w=>w.view==='global') && !!document.querySelector('.osw .tl-tabs')")
        await asyncio.sleep(2)

        assert await b.js(COVERED_INFINITE) == [], 'an endless animation runs under the desktop'
        assert await b.js(FIXED_IN_WINDOWS) == [], 'a fixed background inside a window repaints every scroll frame'

        await b.call('Performance.enable')
        async def recalcs():
            m = await b.call('Performance.getMetrics')
            return next(x['value'] for x in m['metrics'] if x['name'] == 'RecalcStyleCount')
        before = await recalcs()
        await asyncio.sleep(3)
        idle = await recalcs() - before
        # ~180 with a covered pulse; a quiet desktop does a handful (a relay status paint, say).
        assert idle < 30, f'{idle} style recalculations in 3s at rest'

    asyncio.run(desktop.with_browser('online', '', check, INIT))
