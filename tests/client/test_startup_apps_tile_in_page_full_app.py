"""STARTUP APPS ARE TILED ON THE WINDOWS/macOS APP TOO ("Windows app not tiling correctly, so much wasted monitor
space", 2026-10-29).

Off PosterChanOS the PosterChan apps are IN-PAGE windows, and there are no compositor monitors: System Settings ->
Startup apps offered its tiling choice only per monitor that pcDisplays named (none there), and runStartupApps' in-page
branch opened the apps at their cascade sizes and never tiled them. The shipped bundle in the Windows-app shape (the
windowed desktop on, an autostart bridge that is not the shell, NO pcWM and NO pcDisplays).

Each case fails on the code before the fix: the "This screen" row does not exist, and seeded the same preference the
windows stay at their cascade sizes.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


WINDOWS_APP = r'''
window.pcShell=Object.assign(window.pcShell||{},{backgroundOwner:true});
window.pcAutostart={programs:false,platform:'win32',isShell:async()=>false,list:async()=>[],add:async()=>'',addApp:async()=>'',set:async()=>({}),remove:async()=>({}),run:async()=>({ok:true})};
'''

WS1 = ['notes', 'messages', 'global', 'calendar']
WS2 = 'contacts'
PAGE = "document.querySelector('[data-settings-page=\"startup\"]:not([hidden])')"
SNAP = 8

# Visible windows of the current workspace against #os-desk (the area above the taskbar): the share of the desk they
# cover (rasterised at 4px, so overlaps count once) and the widest empty strip along each desk edge.
MEASURE = r'''(()=>{
  const d=document.querySelector('#os-desk').getBoundingClientRect();
  const ws=[...document.querySelectorAll('#os-desk .osw')].filter(e=>!e.classList.contains('osw-offws')&&!e.classList.contains('osw-handoff-preview')&&e.offsetWidth>0)
    .map(e=>e.getBoundingClientRect());
  const S=4,cols=Math.ceil(d.width/S),rows=Math.ceil(d.height/S);let hit=0;
  for(let y=0;y<rows;y++)for(let x=0;x<cols;x++){const px=d.left+x*S+S/2,py=d.top+y*S+S/2;
    if(ws.some(r=>px>=r.left&&px<r.right&&py>=r.top&&py<r.bottom))hit++;}
  const edge=ws.length?{left:Math.min(...ws.map(r=>r.left))-d.left,top:Math.min(...ws.map(r=>r.top))-d.top,
    right:d.right-Math.max(...ws.map(r=>r.right)),bottom:d.bottom-Math.max(...ws.map(r=>r.bottom))}:null;
  return {n:ws.length,cover:hit/(cols*rows),edge,desk:[d.width,d.height]};
})()'''


async def _until(b, expression, seconds):
    for _ in range(int(seconds / .25)):
        if await b.js(expression):
            return
        await asyncio.sleep(.25)
    raise AssertionError({'waiting': expression, 'errors': await b.js('__errors')})


async def _desktop(b):
    await b.until("!!document.body && document.body.classList.contains('os-on') && !!document.querySelector('#os-bar')")
    await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")


async def _open_startup_page(b):
    await b.js("PCOS.openSystemSettings()")
    await b.until("!!document.querySelector('.os-set-nav [data-page=\"startup\"]')")
    await b.js("document.querySelector('.os-set-nav [data-page=\"startup\"]').click()")
    await b.until(f"!!{PAGE} && {PAGE}.querySelectorAll('[data-startup-view]').length>3")


async def _close_windows(b):
    await b.js("[...document.querySelectorAll('#os-desk .osw .osw-x')].forEach(x=>x.click());true")
    await b.until("!document.querySelector('#os-desk .osw:not(.osw-handoff-preview)')")


async def _tiled_after_login(b, got):
    """The reload is the next login: the page restores the account's prefs and opens the startup apps itself."""
    await b.js("location.reload()")
    await b.until("!!window.__PC && !!window.PCOS && !!__PC.me()")
    await _desktop(b)
    await _until(b, f"document.querySelectorAll('#os-desk .osw:not(.osw-offws)').length>={len(WS1)}", 40)
    # Polled to a deadline, not slept: tiled is reached within seconds or never.
    for _ in range(60):
        got['ws1'] = await b.js(MEASURE)
        if got['ws1']['cover'] >= .9:
            break
        await asyncio.sleep(.5)
    # A person arranges a window on workspace 1 themselves; nothing may undo it.
    got['mine'] = await b.js("(()=>{const e=document.querySelector('#os-desk .osw:not(.osw-offws)');"
                             "e.style.left='40px';e.style.top='30px';e.style.width='300px';e.style.height='200px';e.id='mine';return e.style.cssText})()")
    await b.js("PCOS.switchWorkspace(1);true")
    await b.until("PCOS.workspaces().current===1")
    for _ in range(60):
        got['ws2'] = await b.js(MEASURE)
        if got['ws2']['n'] and got['ws2']['cover'] >= .9:
            break
        await asyncio.sleep(.5)
    got['ws2_title'] = await b.js("[...document.querySelectorAll('#os-desk .osw:not(.osw-offws) .osw-title')].map(t=>t.textContent)")
    # Once: rearranged by hand, a second visit leaves it as it is, and so does the first workspace on return.
    await b.js("(()=>{const e=document.querySelector('#os-desk .osw:not(.osw-offws)');e.id='mine2';"
               "e.style.left='60px';e.style.top='50px';e.style.width='320px';e.style.height='220px'})()")
    await b.js("PCOS.switchWorkspace(0);true")
    await asyncio.sleep(1.5)
    got['mine_after'] = await b.js("document.getElementById('mine').style.cssText")
    await b.js("PCOS.switchWorkspace(1);true")
    await asyncio.sleep(1.5)
    got['mine2_after'] = await b.js("[document.getElementById('mine2').style.width,document.getElementById('mine2').style.height]")
    await b.js("PCOS.switchWorkspace(0);true")


def _assert_tiled(got):
    ws1, ws2 = got['ws1'], got['ws2']
    assert ws1['n'] == len(WS1) and ws1['cover'] >= .9, ("the startup apps were not tiled over the screen", ws1)
    assert max(ws1['edge'].values()) <= 2 * SNAP + 1, ("an empty strip along the screen edge", ws1)
    assert ws2['n'] == 1 and ws2['cover'] >= .9 and max(ws2['edge'].values()) <= 2 * SNAP + 1, \
        ("workspace 2's startup app was not tiled when workspace 2 was shown", ws2)
    assert any('contact' in t.lower() for t in got['ws2_title']), got['ws2_title']
    assert got['mine_after'] == got['mine'], ("workspace 1 was re-tiled over the person's arrangement", got)
    assert got['mine2_after'] == ['320px', '220px'], ("workspace 2 was tiled a second time", got)


@CHROME
def test_this_screen_tiling_is_offered_saved_and_tiles_the_startup_apps_at_login():
    got = {}

    async def check(b):
        await desktop.login(b)
        await _desktop(b)
        got['native'] = await b.js("!!(window.PCOSWin && PCOSWin.enabled && PCOSWin.enabled())")
        await _open_startup_page(b)
        await _until(b, f"!!{PAGE}.querySelector('[data-startup-tile=\"_screen\"]') && !{PAGE}.querySelector('[data-startup-tiling]').hidden", 5)
        got['row'] = await b.js(f"{PAGE}.querySelector('[data-startup-tiling]').textContent")
        for v in WS1 + [WS2]:
            await b.js(f"{PAGE}.querySelector('[data-startup-view=\"{v}\"]').click()")
            await b.until(f"!!{PAGE}.querySelector('[data-startup-ws=\"{v}\"]')")
        await b.js(f"(()=>{{const s={PAGE}.querySelector('[data-startup-ws=\"{WS2}\"]');s.value='2';s.dispatchEvent(new Event('change'))}})()")
        # The person picks Grid: focus the select, move to the option with the keyboard, commit with change.
        await b.js(f"(()=>{{const s={PAGE}.querySelector('[data-startup-tile=\"_screen\"]');s.focus();"
                   "s.selectedIndex=[...s.options].findIndex(o=>o.textContent==='Grid');s.dispatchEvent(new Event('input',{bubbles:true}));"
                   "s.dispatchEvent(new Event('change',{bubbles:true}))})()")
        got['tiling'] = await b.js("ClientSettings.get('startupTiling',{})")
        got['workspace'] = await b.js("ClientSettings.get('startupWorkspace',{})")
        await asyncio.sleep(1.0)
        await _close_windows(b)
        await _tiled_after_login(b, got)
        # Hydrated on the fresh load: the page shows what was saved.
        await _open_startup_page(b)
        await b.until(f"!!{PAGE}.querySelector('[data-startup-tile=\"_screen\"]')")
        got['shown'] = await b.js(f"{PAGE}.querySelector('[data-startup-tile=\"_screen\"]').value")
        got['shown_ws'] = await b.js(f"{PAGE}.querySelector('[data-startup-ws=\"{WS2}\"]').value")
        got['errors'] = await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check, WINDOWS_APP))
    assert got['native'] is False, "the fixture is not the Windows-app shape"
    assert 'This screen' in got['row'], got['row']
    assert got['tiling'] == {'_screen': 'grid'} and got['workspace'] == {WS2: 2}, got
    _assert_tiled(got)
    assert got['shown'] == 'grid' and got['shown_ws'] == '2', ("the choice did not come back on a fresh load", got)
    assert not got['errors'], got['errors']


@CHROME
def test_a_saved_screen_tiling_tiles_the_in_page_startup_windows():
    """The same preference written straight into this device's settings (as an earlier session left it), so the
    tiling half is proved on its own, independent of the Settings page."""
    got = {}

    async def check(b):
        await desktop.login(b)
        await _desktop(b)
        await b.js(f"ClientSettings.set('startupApps',{WS1 + [WS2]!r});ClientSettings.set('startupAppsAt',Date.now());"
                   f"ClientSettings.set('startupWorkspace',{{'{WS2}':2}});ClientSettings.set('startupTiling',{{_screen:'grid'}});"
                   f"__PC.saveStartupApps&&__PC.saveStartupApps({WS1 + [WS2]!r},Date.now());true")
        await asyncio.sleep(1.0)
        await _close_windows(b)
        await _tiled_after_login(b, got)
        got['errors'] = await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check, WINDOWS_APP))
    _assert_tiled(got)
    assert not got['errors'], got['errors']
