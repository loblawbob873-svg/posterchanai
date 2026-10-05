"""The Terminal's last rows and its key bar are not under the phone's bottom navigation bar.

"i keep having to reload terminal to see your newer replies, no scrolling" -- on the Android app, with
the keyboard down too. Measured in the shipped client at 390x800: the fixed bottom nav sits at 738-800,
the terminal's text ran to 751 and its key bar (esc/tab/ctrl/arrows) sat at 753-800, so a tap at 745+
landed on the NAV. The newest output and the prompt were drawn under it, and since xterm believed they
were on screen there was nothing to scroll to. Every other full-height view (AI chat, Live Translate)
already clears the 62px bar + the safe area; the Terminal did not.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


MEASURE = r"""(()=>{const r=s=>{const e=document.querySelector(s);if(!e)return null;const x=e.getBoundingClientRect();return {top:x.top,bottom:x.bottom,h:x.height}};
  const nav=document.querySelector('.mobilenav'), shown=nav&&getComputedStyle(nav).display!=='none'&&nav.getBoundingClientRect().height>0;
  const keys=document.querySelector('.tty-keys');
  const kr=keys&&keys.getBoundingClientRect();
  const hit=kr?document.elementFromPoint(kr.left+kr.width/2, kr.top+kr.height/2):null;
  return {nav:shown?r('.mobilenav'):null, viewport:r('.xterm-viewport'), screen:r('.xterm-screen'), keys:kr?{top:kr.top,bottom:kr.bottom}:null,
          keysHit:!!(hit&&keys&&keys.contains(hit)), inner:innerHeight};})()"""


async def _open_terminal(b, width, height, mobile):
    await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=height, deviceScaleFactor=2 if mobile else 1, mobile=mobile))
    if mobile:
        await b.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js("__PC.switchView('terminal'); true")
    await b.until("!!document.querySelector('.xterm-viewport') && document.querySelector('.xterm-screen').getBoundingClientRect().height>100")
    await asyncio.sleep(1.2)                      # the fit is coalesced
    return await b.js(MEASURE)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('size', [(390, 800), (360, 640)])
def test_phone_terminal_ends_above_the_bottom_nav(size):
    got = {}

    async def check(b):
        got.update(await _open_terminal(b, size[0], size[1], True))

    asyncio.run(desktop.with_browser('online', '', check))
    nav = got['nav']
    assert nav, ("the bottom nav is not shown on a phone -- re-read this test", got)
    assert got['viewport']['bottom'] <= nav['top'] + 1, ("the terminal's last rows are under the bottom nav", got)
    assert got['keys'] and got['keys']['bottom'] <= nav['top'] + 1, ("the key bar is under the bottom nav", got)
    assert got['keysHit'], ("a tap on the key bar lands on the nav", got)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_desktop_terminal_keeps_its_full_height():
    got = {}

    async def check(b):
        got.update(await _open_terminal(b, 1280, 900, False))

    asyncio.run(desktop.with_browser('online', '', check))
    assert got['nav'] is None, got
    assert got['viewport']['bottom'] >= got['inner'] - 80, ("the desktop terminal lost height it does not need to give up", got)
