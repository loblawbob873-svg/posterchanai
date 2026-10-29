"""WORKSPACES: virtual desktops on the PosterChan desktop.

Asked 2026-10-28 ("snap layouts and spaces"). The shipped desktop, real clicks and keys. Each workspace shows only its own
windows and taskbar buttons; Ctrl+Alt+Left/Right (Super+Ctrl+arrows on PosterChanOS) switch; "Move to workspace" sends a
window across; opening a window that lives elsewhere takes you there; the count survives a reload; removing a workspace
keeps its windows (they join the one before).
"""
import asyncio
import base64
import os
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_super_closes_an_open_start_menu_full_app import COMPOSITOR, _tick

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


VISIBLE = """[...document.querySelectorAll('#os-root .osw')].filter(w=>getComputedStyle(w).display!=='none')
  .map(w=>(w.querySelector('.osw-title')||{}).textContent)"""
TASKS = "[...document.querySelectorAll('#os-bar .os-task[data-kind=web] span')].map(s=>s.textContent)"
CUR = "PCOS.workspaces()"


async def _click(b, sel, button='left'):
    x, y = await b.js("(()=>{const r=document.querySelector(%r).getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()" % sel)
    await b.call('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})
    for kind in ('mousePressed', 'mouseReleased'):
        await b.call('Input.dispatchMouseEvent', {'type': kind, 'x': x, 'y': y, 'button': button, 'clickCount': 1,
                                                  'buttons': 2 if button == 'right' else 1})


async def _menu(b, sel):
    # The context menu event itself, as the other desktop menu tests send it (CDP's synthetic right button does not open one).
    await b.js("(()=>{const t=document.querySelector(%r);const r=t.getBoundingClientRect();"
               "t.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:r.left+Math.min(60,r.width/2),clientY:r.top+r.height/2}));})()" % sel)


async def _key(b, key, code, vk, mods):
    for kind in ('rawKeyDown', 'keyUp'):
        await b.call('Input.dispatchKeyEvent', {'type': kind, 'key': key, 'code': code, 'windowsVirtualKeyCode': vk, 'modifiers': mods})


async def _open(b, view, title):
    await b.js("PCOS.routeView(%r);true" % view)
    await b.until("[...document.querySelectorAll('#os-root .osw .osw-title')].some(t=>t.textContent===%r)" % title)
    await asyncio.sleep(.3)


@CHROME
def test_each_workspace_keeps_its_own_windows_and_taskbar():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 1440, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on')")
        await b.js("try{localStorage.removeItem('pc_osWorkspaces')}catch(_){};true")
        await _open(b, 'settings', 'Settings')
        await _click(b, '#os-bar .os-ws-add')                       # a new workspace, and it is in front
        await b.until("PCOS.workspaces().current===1")
        # The taskbar repaints right after the click that changed it (os.js _deferBarDraw keeps the pressed button alive).
        await b.until("!!document.querySelector('#os-bar .os-ws-b[data-ws=\"1\"].on')")
        got['ws2_empty'] = await b.js(VISIBLE)
        got['ws2_tasks'] = await b.js(TASKS)
        got['said'] = await b.js("(document.querySelector('.os-ws-say')||{}).textContent||''")
        await _open(b, 'notes', 'Notes')
        got['ws2'] = await b.js(VISIBLE)
        if os.environ.get('PC_SHOTS'):
            Path(os.environ['PC_SHOTS']).mkdir(parents=True, exist_ok=True)
            Path(os.environ['PC_SHOTS'], 'workspaces.png').write_bytes(base64.b64decode((await b.call('Page.captureScreenshot', {'format': 'png'}))['data']))
        await _key(b, 'ArrowLeft', 'ArrowLeft', 37, 2 | 1)            # Ctrl+Alt+Left
        await b.until("PCOS.workspaces().current===0")
        got['ws1'] = await b.js(VISIBLE)
        got['ws1_tasks'] = await b.js(TASKS)
        # Opening Notes from workspace 1 goes to where Notes lives.
        await b.js("PCOS.routeView('notes');true")
        await b.until("PCOS.workspaces().current===1")
        got['followed'] = True
        # Move Notes back to workspace 1 from its title-bar menu.
        await _menu(b, "#os-root .osw.focused .osw-bar")
        await b.until("[...document.querySelectorAll('.os-ctx .os-ctx-b')].some(x=>x.textContent.trim()==='Move to workspace 1')")
        await b.js("[...document.querySelectorAll('.os-ctx .os-ctx-b')].find(x=>x.textContent.trim()==='Move to workspace 1').click()")
        await asyncio.sleep(.3)
        got['after_move_ws2'] = await b.js(VISIBLE)
        await _key(b, 'ArrowLeft', 'ArrowLeft', 37, 2 | 1)
        await b.until("PCOS.workspaces().current===0")
        got['after_move_ws1'] = sorted(await b.js(VISIBLE))

    asyncio.run(desktop.with_browser('online', '', check))
    assert got['ws2_empty'] == [] and got['ws2_tasks'] == [], ("a new workspace showed another workspace's windows", got)
    assert got['said'] == 'Workspace 2', got['said']
    assert got['ws2'] == ['Notes'], got['ws2']
    assert got['ws1'] == ['Settings'] and got['ws1_tasks'] == ['Settings'], got
    assert got['after_move_ws2'] == [], got
    assert got['after_move_ws1'] == ['Notes', 'Settings'], got


@CHROME
def test_the_count_survives_a_reload_and_removing_keeps_the_windows():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 1440, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on')")
        await b.js("PCOS.addWorkspace();PCOS.addWorkspace();true")
        await b.until("PCOS.workspaces().count===3")
        await b.call('Page.reload')
        await b.until("!!window.PCOS && document.body.classList.contains('os-on') && !!document.querySelector('#os-bar .os-ws')")
        got['buttons'] = await b.js("document.querySelectorAll('#os-bar .os-ws-b').length")
        await b.js("PCOS.switchWorkspace(2);true")
        await _open(b, 'notes', 'Notes')
        await _menu(b, "#os-bar .os-ws-b[data-ws='2']")
        await b.until("[...document.querySelectorAll('.os-ctx .os-ctx-b')].some(x=>/Remove workspace 3/.test(x.textContent))")
        await b.js("[...document.querySelectorAll('.os-ctx .os-ctx-b')].find(x=>/Remove workspace 3/.test(x.textContent)).click()")
        await b.until("PCOS.workspaces().count===2")
        got['cur'] = await b.js(CUR)
        got['visible'] = await b.js(VISIBLE)

    asyncio.run(desktop.with_browser('online', '', check))
    assert got['buttons'] == 3, ("the workspaces were forgotten on reload", got)
    assert got['cur']['current'] == 1 and got['visible'] == ['Notes'], ("removing a workspace lost its window", got)


@CHROME
def test_super_ctrl_arrows_from_the_compositor_switch():
    async def check(b):
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on') && __wm.listeners.length > 0")
        await b.js("PCOS.addWorkspace();PCOS.switchWorkspace(0);true")
        await _tick(b, 'pc:ws:next')
        assert await b.js("PCOS.workspaces().current") == 1
        await _tick(b, 'pc:ws:prev')
        assert await b.js("PCOS.workspaces().current") == 0
    asyncio.run(desktop.with_browser('online', '', check, extra_init=COMPOSITOR))


def test_a_native_app_on_another_workspace_is_stashed_like_a_minimised_one():
    """nsync hands the compositor's stash plan `minimised` per window; a native app (Firefox in a frame) on another
    workspace must be stashed, or it floats over the workspace in front with no frame around it."""
    src = (Path(__file__).resolve().parents[2] / "static/js/client/os.js").read_text(encoding="utf-8")
    body = src[src.index("async function nsync"):]
    body = body[:body.index("const plan = NAT().stashPlan")]
    assert body.count("minimised: !!(w.min || w.offws)") + body.count("minimised:!!(w.min || w.offws)") == 2, body[-900:]


@CHROME
def test_super_digit_goes_to_that_workspace_and_never_closes_a_window():
    """"make the shortcuts keys super + 1 for workspace 1" and "super + 1 is making windows disappear": Super+1 was a
    leftover CLOSE chord. Now Super+N goes to workspace N (made if it is not there yet) and Super+Shift+N takes the
    window in front along. The compositor's binding table and the desktop's handling, both shipped."""
    from tests.wayfire_config import bindings
    binds = bindings()
    for n in range(1, 10):
        assert binds["<super> KEY_%d" % n].endswith("pc-wayfire-action pc:ws:goto:%d" % n), binds.get("<super> KEY_%d" % n)
        assert binds["<super> <shift> KEY_%d" % n].endswith("pc-wayfire-action pc:ws:carry:%d" % n)
    assert not any("KEY_%d" % n in c and "close" in cmd for n in range(1, 10) for c, cmd in binds.items())
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on') && __wm.listeners.length > 0")
        await _open(b, 'notes', 'Notes')
        got['before'] = await b.js(VISIBLE)
        await _tick(b, 'pc:ws:goto:3')
        got['three'] = await b.js(CUR)
        await _tick(b, 'pc:ws:goto:1')
        got['one'] = await b.js(CUR)
        got['back'] = await b.js(VISIBLE)
        await _tick(b, 'pc:ws:carry:2')
        got['carried'] = await b.js(CUR)
        got['carried_visible'] = await b.js(VISIBLE)
        await _tick(b, 'pc:ws:goto:1')
        got['left_behind'] = await b.js(VISIBLE)

    asyncio.run(desktop.with_browser('online', '', check, extra_init=COMPOSITOR))
    assert got['before'] == ['Notes'] and got['back'] == ['Notes'], ("a window disappeared", got)
    assert got['three']['current'] == 2 and got['three']['count'] >= 3, ("Super+3 did not make and open workspace 3", got)
    assert got['one']['current'] == 0, got
    assert got['carried']['current'] == 1 and got['carried_visible'] == ['Notes'], ("Super+Shift+2 did not take it along", got)
    assert got['left_behind'] == [], ("the window was not moved to workspace 2", got)
