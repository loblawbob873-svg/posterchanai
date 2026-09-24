"""On a two-monitor PosterChanOS desk, launching an app whose window is on the OTHER monitor must not
draw an in-page copy of it -- which raises the whole desktop surface and covers every window on this
monitor.

Reported: "If I click on Messages, Global disappears" (and "I thought we had regression testing for
this"). Measured on the two-monitor desk: Messages was a real window on DP-1; its desktop icon was
clicked on DP-2. The DP-2 shell sees only DP-2's windows (`nativeTasks` is scoped per monitor), so it
did not find Messages; its request for a real window was refused by main's one-window-per-app rule
(which also focused the existing one); and `openApp` read the refusal as "no real window" and drew an
IN-PAGE Messages frame. Showing an in-page frame publishes `front:true` with the windows it overlaps as
`covers` -- the Social window, still mapped and never minimised, went behind the desktop.

Nothing caught it because every existing check stands the page up as ONE monitor, where the
existing-window lookup always finds the app's window before the refusal can happen. This fixture is
the DP-2 half of the desk: its own surface and the Social window in the scoped list, the Messages
window only in `allIds`, and main answering `hasAppWindow`.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# The DP-2 shell as main presents it. 7 = this monitor's desktop surface, 116 = the Social window on
# this monitor, 15 = the Messages window on the OTHER monitor (only its id is visible from here).
DP2 = r'''
window.__wm={log:[],focus:116,opened:[]};
const __rows=()=>[
  {id:7,app:'place.poster.desktop',title:'PosterChan Desktop',workspace:'2',
   rect:{x:0,y:0,width:innerWidth,height:innerHeight},focused:__wm.focus===7},
  {id:116,app:'place.poster.desktop',title:'PosterChan Window — global',workspace:'2',
   rect:{x:120,y:40,width:innerWidth-240,height:innerHeight-120},focused:__wm.focus===116}];
window.pcWM={windows:async()=>__rows(),launch:async()=>({pid:1}),
  snapshot:async()=>({windows:__rows(),allIds:[5,7,15,116],shellId:7}),
  onEvent:(cb)=>{__wm.emit=cb;return()=>{}},
  focus:async(id)=>{__wm.log.push({focus:id});__wm.focus=id;return true},
  shellFront:async(w)=>{__wm.log.push({front:!!(w&&w.front),covers:(w&&w.covers)||[]});return true},
  // main: Messages already has its window (on DP-1); a request for another is refused.
  hasAppWindow:(v)=>v==='messages'};
window.open=(u,n,f)=>{__wm.opened.push(String(u));return null};
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_messages_on_the_other_monitor_does_not_cover_social():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!document.querySelector('#os-start') && window.PCOSShell && PCOSShell.available()===true")
        await b.until("!!document.querySelector('.os-icon[data-view=\"messages\"]')")
        await asyncio.sleep(1.0)
        await b.js("__wm.log.length=0;true")
        await b.js("(()=>{const i=document.querySelector('.os-icon[data-view=\"messages\"]');"
                   "i.dispatchEvent(new MouseEvent('click',{bubbles:true}));"
                   "i.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));return true})()")
        await asyncio.sleep(1.5)
        got['asked'] = await b.js("__wm.opened.filter(u=>/pcwin=messages/.test(u)).length")
        got['frames'] = await b.js("[...document.querySelectorAll('.osw')].map(w=>(w.querySelector('.osw-title')||{}).textContent)")
        got['covered'] = await b.js("__wm.log.filter(x=>x.front && x.covers.includes(116)).length")

    asyncio.run(desktop.with_browser('online', '', check, DP2))
    assert got['asked'] >= 1, "the desktop never asked main for the Messages window: %r" % got
    assert 'Messages' not in got['frames'], (
        "an in-page Messages frame was drawn although its real window exists on the other monitor: %r" % got)
    assert got['covered'] == 0, "the desktop surface was raised over the Social window: %r" % got
