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


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_text_size_more_menu_and_split_panes():
    """The toolbar does what it says: A+ and A− change the terminal's text size (and remember it), More → Reset
    text size puts it back; Split → Split down adds a pane below, and Close this pane removes it again."""
    async def check(b):
        await _open_terminal(b, 1280, 860, False)
        size = "(+localStorage.getItem('pc_tty_font_size')||0)"
        # What the terminal DRAWS, not only the number it saves: a saved size the screen ignores is a dead button. In a
        # window of fixed width, larger text is FEWER columns -- read off the live grid (PCTerm._grid).
        cols = "PCTerm._grid().cols"
        before = await b.js(cols)
        await b.js("document.querySelector('#tty-font-more').click(); 1")
        await b.until(f"{size}>0")
        bigger = await b.js(size)
        await b.until(f"{cols}<{before}")
        await b.js("document.querySelector('#tty-font-less').click(); document.querySelector('#tty-font-less').click(); 1")
        await b.until(f"{size}==={bigger - 2}")
        await b.until(f"{cols}>{before}")
        await b.js("document.querySelector('#tty-more').click(); 1")
        await b.until("!document.querySelector('#tty-more-menu').hidden")
        await b.js("document.querySelector('#tty-more-font-reset').click(); 1")
        await b.until("localStorage.getItem('pc_tty_font_size')===null && document.querySelector('#tty-more-menu').hidden")

        panes = "document.querySelectorAll('.tty-pane').length"
        one = await b.js(panes)
        await b.js("document.querySelector('#tty-split').click(); 1")
        await b.until("!document.querySelector('#tty-split-menu').hidden")
        await b.js("document.querySelector('#tty-split-down').click(); 1")
        await b.until(f"{panes}==={one}+1")
        await b.js("document.querySelector('#tty-split').click(); 1")
        await b.until("!document.querySelector('#tty-split-menu').hidden && !document.querySelector('#tty-pane-close').classList.contains('hidden')")
        await b.js("document.querySelector('#tty-pane-close').click(); 1")
        await b.until(f"{panes}==={one}")
        assert not await b.js("__errors"), await b.js("__errors")

    asyncio.run(desktop.with_browser('online', '', check))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_history_from_another_device_is_typed_in_and_detach_leaves_the_shell_running():
    """The history button appears once a command arrives from ANOTHER terminal of this account (an encrypted ephemeral
    21078 to yourself); its panel names the command and the device, and pressing a row TYPES it at the prompt -- never
    runs it, since a command from another machine may name a path this one does not have. More → Detach leaves the
    shell running (the local session is detached, not closed)."""
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=1280, height=860, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        # Another device ran a command: its encrypted history event is on the relay when this terminal subscribes.
        await b.js(r"""(async()=>{const me=__PC.me().pubkey,at=Date.now()+2000;
          const content=await __PC.nip44enc(me,JSON.stringify({c:'make deploy-check',at,d:'Laptop'}));
          const ev=await __PC.signTemplate({kind:PCTermHistory.KIND||21078,created_at:Math.floor(at/1000),tags:[],content});
          (window.__events=window.__events||[]).push(ev);})()""")
        await b.js("__PC.switchView('terminal'); true")
        await b.until("!!document.querySelector('.xterm-viewport') && document.querySelector('.xterm-screen').getBoundingClientRect().height>100")
        await b.js("(()=>{window.__detached=[];window.__closed=[];const d=pcTerm.detach,c=pcTerm.close;"
                   "pcTerm.detach=async(...a)=>{__detached.push(a);return d(...a)};pcTerm.close=async(...a)=>{__closed.push(a);return c(...a)};})()")
        await b.until("!document.querySelector('#tty-hist').classList.contains('hidden')")
        await b.js("document.querySelector('#tty-hist').click(); 1")
        await b.until("!document.querySelector('#tty-hist-panel').hidden && document.querySelector('#tty-hist-panel').innerText.includes('make deploy-check')")
        assert 'Laptop' in await b.js("document.querySelector('#tty-hist-panel').innerText")
        before = await b.js("__localWrites.length")
        await b.js("document.querySelector('#tty-hist-panel .tty-hist-row').click(); 1")
        await b.until(f"__localWrites.slice({before}).join('').includes('make deploy-check')")
        typed = await b.js(f"__localWrites.slice({before}).join('')")
        assert '\r' not in typed and '\n' not in typed, ('a command from another device was RUN, not typed in', typed)
        assert await b.js("document.querySelector('#tty-hist-panel').hidden")

        await b.js("document.querySelector('#tty-more').click(); 1")
        await b.until("!document.querySelector('#tty-more-menu').hidden && !document.querySelector('#tty-stop').classList.contains('hidden')")
        await b.js("document.querySelector('#tty-stop').click(); 1")
        await b.until("__detached.length>0 || !PCTerm.connected()")
        assert not await b.js("__closed.length"), 'Detach closed the shell'
        assert not await b.js("__errors"), await b.js("__errors")

    asyncio.run(desktop.with_browser('online', '', check))
