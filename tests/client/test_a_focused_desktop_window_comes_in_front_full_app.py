"""A window the desktop draws comes IN FRONT of the windows it overlaps, however it was opened.

Two reports, one mechanism. Preview, Search, folders and System Settings are drawn INSIDE the
desktop surface, so they are visible only when main puts the desktop above the applications they
overlap -- the ids the renderer publishes as `covers` with `front: true` (pcWM.shellFront).

1. "pc-open tried opening PDF and the window went behind all the other windows." Measured on the
   desktop: the Preview frame was focused and maximised, and `covers` went out EMPTY. `domStackPlan`
   asked only what fraction of the FRAME a window overlapped; a maximised document over a
   1358px-wide Notes window overlaps a small part of itself, so Notes was never listed.

2. "Taskbar Search -> Search window always goes behind active windows and always stays behind
   firefox." `focusWin` stated `front: true`, then its own `drawBar()` re-derived `false` from the
   stale `_foreignFocused` (Firefox had the keyboard a moment ago), and the shell's focus was then
   answered by the bottom guard's lowerShell.

The fixture is the compositor as main presents it: a snapshot with the desktop's own surface, a
popped-out PosterChan window and Firefox, and a log of what the page publishes. Neither case has a
press on the frame -- pc-open arrives over a socket and Search opens from a key press in the bar.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


COMPOSITOR = r'''
window.__publishOK=true;
window.__wm={log:[],focus:40};
const __rows=()=>[
  {id:13,app:'place.poster.desktop',title:'PosterChan Desktop',workspace:'1',
   rect:{x:0,y:0,width:innerWidth,height:innerHeight},focused:__wm.focus===13},
  {id:35,app:'place.poster.desktop',title:'PosterChan Window — notes',workspace:'1',
   rect:{x:200,y:60,width:400,height:400},focused:__wm.focus===35},
  {id:40,app:'firefox',title:'Mozilla Firefox',workspace:'1',
   rect:{x:0,y:0,width:innerWidth,height:innerHeight-60},focused:__wm.focus===40}];
window.pcWM={windows:async()=>__rows(),onEvent:()=>()=>{},launch:async()=>({pid:1}),
  snapshot:async()=>({windows:__rows(),allIds:[13,35,40],shellId:13}),
  focus:async(id)=>{__wm.log.push({focus:id});__wm.focus=id;return true},
  shellFront:async(w)=>{__wm.log.push({front:!!(w&&w.front),covers:(w&&w.covers)||[]});return true}};
'''


def _last_front(log):
    fronts = [x for x in log if 'front' in x]
    assert fronts, log
    return fronts[-1]


async def _ready(b):
    await desktop.login(b)
    await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.querySelector('#os-desk')")
    await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
    await asyncio.sleep(1)
    await b.js("__wm.focus=40;__wm.log=[]")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_maximised_preview_is_put_above_a_smaller_window_it_covers():
    async def check(b):
        await _ready(b)
        await b.js("__wm.focus=35")        # the terminal-ish popped-out window has the keyboard
        opened = await b.js("PCPreview.open({name:'m.pdf',mime:'application/pdf',"
                            "blob:new Blob(['%PDF-1.4'],{type:'application/pdf'})})")
        assert opened is True
        await asyncio.sleep(1.5)
        log = await b.js('__wm.log')
        last = _last_front(log)
        assert last['front'] is True, log
        assert 35 in last['covers'], 'the maximised Preview left Notes drawn over it: ' + json.dumps(log)
        assert {'focus': 13} in log, log

    asyncio.run(desktop.with_browser('online', '', check, COMPOSITOR))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_taskbar_search_is_not_sunk_under_the_browser_that_had_focus():
    async def check(b):
        await _ready(b)
        await b.js("(()=>{const q=document.querySelector('#os-q-bar');q.focus();q.value='bitcoin';"
                   "q.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))})()")
        await asyncio.sleep(1.5)
        assert await b.js("[...document.querySelectorAll('.osw.focused .osw-title')].some(t=>/search/i.test(t.textContent))")
        log = await b.js('__wm.log')
        after = log[next(i for i, x in enumerate(log) if x.get('front') is True):]
        assert all(x.get('front') is not False for x in after), \
            'focusWin stated the front and then its own drawBar took it back: ' + json.dumps(log)
        assert 40 in _last_front(log)['covers'], log

    asyncio.run(desktop.with_browser('online', '', check, COMPOSITOR))
