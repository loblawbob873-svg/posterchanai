"""Pressing the music widget twice must not put the desktop over every application.

Reported as "Clicking a button on the Desktop Music Widget causes all the windows to hide on the
desktop". Nothing was hidden or minimised: the desktop surface -- one opaque window the size of the
monitor -- ended up ON TOP of them.

The compositor half is Wayfire 0.10.1's click-to-focus, read from its source (src/core/wm.cpp
`check_focus_surface` → `focus_raise_view`): EVERY button press raises the view under the pointer,
but the `view-focused` IPC event -- the only thing main.js answers with `send-to-back` -- is emitted
only when keyboard focus CHANGES. Send-to-back leaves the keyboard where it was, so after one click
the desktop is at the bottom and still focused, and the second click raises it with no event for
anybody to answer. The music widget is the surface people press several times in a row.

The fixture below is that compositor, stated as its two rules, installed ahead of the page's own
listeners exactly as the compositor acts ahead of the client: a press raises the desktop; a press
that CHANGES focus also produces the event main answers by sinking it. `pcWM.shellFront` is main's
`pc:wm:shell-front` handler, whose one effect here is `sinkShellSurfaces()`. The clicks are real
mouse input through CDP, not `element.click()`, because the bug is in what a PRESS does.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


COMPOSITOR = r'''
window.__publishOK=true;   // the relay accepts the layout write that adds the widget
window.__wm={shellOnTop:false,focused:'firefox',log:[]};
window.pcWM={windows:async()=>[],onEvent:()=>()=>{},focus:async()=>true,
  launch:async()=>({pid:1}),
  // main.js pc:wm:shell-front → sinkShellSurfaces(): the desktop goes to the back, focus stays.
  shellFront:async()=>{__wm.shellOnTop=false;__wm.log.push('sink');return true}};
// Wayfire's click-to-focus, ahead of every listener the page will add.
window.addEventListener('pointerdown',()=>{
  __wm.shellOnTop=true;__wm.log.push('raise');
  if(__wm.focused!=='shell'){
    __wm.focused='shell';__wm.log.push('view-focused');
    setTimeout(()=>{__wm.shellOnTop=false;__wm.log.push('sink-on-focus')},0);   // sinkShellOnFocus
  }
},true);
'''


async def _press(b, selector):
    box = await b.js("(()=>{const r=document.querySelector(" + repr(selector) + ").getBoundingClientRect();"
                     "return [r.left+r.width/2,r.top+r.height/2]})()")
    for kind in ('mousePressed', 'mouseReleased'):
        await b.call('Input.dispatchMouseEvent', {'type': kind, 'x': box[0], 'y': box[1],
                                                  'button': 'left', 'buttons': 1 if kind == 'mousePressed' else 0,
                                                  'clickCount': 1})
    await asyncio.sleep(.35)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_pressing_the_music_widget_twice_leaves_the_applications_in_front():
    async def check(b):
        await desktop.login(b)
        await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.querySelector('#os-desk')")
        # Put a Now-playing widget on the desk the way a person does: right-click → Add a widget.
        for _ in range(30):
            if not await b.js("!!document.querySelector('.os-wgtpick')"):
                await b.js("""(()=>{const d=document.querySelector('#os-desk');
                  d.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:300,clientY:300}));
                  const row=[...document.querySelectorAll('.os-ctx .os-ctx-b')].find(x=>/add a widget/i.test(x.textContent));
                  if(row) row.click();})()""")
                await asyncio.sleep(.3)
            await b.js("document.querySelector('.os-wgtpick .os-wgt-pick[data-t=\"music\"]')?.click()")
            await asyncio.sleep(.4)
            if await b.js("!!document.querySelector('.os-wgt[data-type=\"music\"] [data-m=\"next\"]')"):
                break
        assert await b.js("!!document.querySelector('.os-wgt[data-type=\"music\"] [data-m=\"next\"]')"), \
            await b.js("({pick:!!document.querySelector('.os-wgtpick'),wgts:document.querySelectorAll('.os-wgt').length,"
                       "errors:__errors})")
        # The machine-setup card is a separate first-boot surface over the desk; this is a desktop
        # somebody is already using. The press must land on the widget, or it tests the card.
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        at = await b.js("(()=>{const t=document.querySelector('.os-wgt[data-type=\"music\"] [data-m=\"next\"]');"
                        "const r=t.getBoundingClientRect();return t.contains(document.elementFromPoint("
                        "r.left+r.width/2,r.top+r.height/2))})()")
        assert at, 'something is drawn over the widget, so a press would not reach it: ' + await b.js(
            "(()=>{const t=document.querySelector('.os-wgt[data-type=\"music\"] [data-m=\"next\"]');"
            "const r=t.getBoundingClientRect();const e=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);"
            "return (e?e.outerHTML.slice(0,160):'none')+' '+JSON.stringify(r)})()")
        await b.js("__wm.focused='firefox';__wm.shellOnTop=false;__wm.log=[]")

        button = '.os-wgt[data-type="music"] [data-m="next"]'
        await _press(b, button)
        assert await b.js('__wm.shellOnTop') is False, await b.js('__wm.log')
        await _press(b, button)      # the desktop already has the keyboard: no view-focused this time
        log = await b.js('__wm.log')
        assert 'view-focused' not in log[log.index('sink-on-focus') + 1:], log
        assert await b.js('__wm.shellOnTop') is False, \
            'the second press left the desktop over every application: ' + repr(log)
        await _press(b, '.os-wgt[data-type="music"] [data-m="prev"]')
        assert await b.js('__wm.shellOnTop') is False, await b.js('__wm.log')
        assert not await b.js('__errors'), await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check, COMPOSITOR))
