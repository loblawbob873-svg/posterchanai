"""NOTHING THE START MENU OPENS IS DRAWN UNDER THE WINDOWS.

On PosterChanOS the desktop page sits UNDER every application window. Three start-menu buttons drew
their UI there and so "opened" invisibly: Power ("hidden behind active windows"), the account
switcher, and Log out's confirmation. This presses EVERY footer button the way the start popup
delivers it (a pc:act tick to the desktop) with the machine's popup-window bridge present, and after
each one the desktop page must not hold a menu, popover or dialog of its own — whatever the button
shows must have been asked for as a window. A button added later that paints on the desktop surface
fails here without anyone having to remember this rule.

Then the answers travel back: Log out's window saying Cancel logs nothing out and saying Log out
does; the account window's pick switches the desktop's account; Classic leaves the desktop and the
instance logo brings it back.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_start_footer_buttons_do_something_full_app import MACHINE, TICK, _desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


POPUPS = r'''
window.__popups=[];
window.pcPopup={open:async(kind,rect,arg)=>{__popups.push({how:'open',kind,rect,arg:arg||''});return true},
  toggle:async(kind,rect)=>{__popups.push({how:'toggle',kind,rect});return true},
  act:()=>true,pick:()=>true,close:async()=>true};
'''
IN_PAGE = r'''[...document.querySelectorAll('.menu-pop,.os-pop,.acct-pop,.modal-bg,.modal,.uic-bg,[role=alertdialog]')]
  .filter(e=>{const r=e.getBoundingClientRect(),cs=getComputedStyle(e);return r.width>0&&r.height>0&&cs.display!=='none'&&cs.visibility!=='hidden'})
  .map(e=>e.className||e.getAttribute('role'))'''


def _run(check):
    asyncio.run(desktop.with_browser('online', '', check, MACHINE + POPUPS))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_no_footer_button_draws_on_the_desktop_surface():
    got = {}

    # The buttons, read from the REAL start menu -- its own popup window, as on the machine.
    async def read_footer(b):
        await desktop.login(b)
        await b.until("!!document.querySelector('.os-foot .os-foot-btn')")
        got['kinds'] = await b.js("[...document.querySelectorAll('.os-foot-btn')].map(x=>x.dataset.foot)")
        got['chip'] = await b.js("!!document.querySelector('.os-acct, #os-acct, .os-me')")
    asyncio.run(desktop.with_browser('online', '?pcpopup=start', read_footer, MACHINE + POPUPS))
    # Every footer button, plus the account chip at the top (it sends the same `accounts` act).
    kinds = got['kinds'] + ['accounts']

    async def check(b):
        await _desktop(b)
        got['each'] = {}
        for kind in kinds:
            if kind in ('classic',):                                     # leaves the desktop: its own test
                continue
            await b.js("window.__popups=[];document.querySelectorAll('.menu-pop,.os-pop,.acct-pop').forEach(e=>e.remove())")
            await b.js(TICK % kind)
            await asyncio.sleep(1.2)
            got['each'][kind] = {'in_page': await b.js(IN_PAGE), 'popups': await b.js("__popups.map(p=>p.kind)")}
            await b.js("__wmListeners.forEach(f=>f({name:'tick',payload:'pc:popup-closed:ask'}))")
    _run(check)
    for kind, r in got['each'].items():
        assert not r['in_page'], (f"Start → {kind} drew on the desktop surface, i.e. under every window", r)
    assert got['each']['power']['popups'] == ['tray'], got['each']['power']
    assert got['each']['accounts']['popups'] == ['accounts'], got['each']['accounts']
    assert got['each']['logout']['popups'] == ['ask'], got['each']['logout']
    assert 'classic' in got['kinds'], ("Classic layout is not offered on PosterChanOS", got['kinds'])
    assert got['chip'], 'the start menu has no account chip to open the switcher from'


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_log_out_asks_in_a_window_and_only_yes_logs_out():
    got = {}

    async def check(b):
        await _desktop(b)
        await b.js("window.__out=0;PCOSShell.logoutSession=async()=>{__out++}")
        await b.js(TICK % 'logout')
        await b.until("__popups.some(p=>p.kind==='ask')")
        got['arg'] = await b.js("JSON.parse(__popups.find(p=>p.kind==='ask').arg)")
        await b.js("__wmListeners.forEach(f=>f({name:'tick',payload:'pc:act:ask:cancel'}))")
        await asyncio.sleep(.4)
        got['after_cancel'] = await b.js("__out")
        await b.js(TICK % 'logout')
        await b.until("__popups.filter(p=>p.kind==='ask').length===2")
        await b.js("__wmListeners.forEach(f=>f({name:'tick',payload:'pc:popup-closed:ask'}))")
        await asyncio.sleep(.4)
        got['after_dismiss'] = await b.js("__out")
        await b.js(TICK % 'logout')
        await b.until("__popups.filter(p=>p.kind==='ask').length===3")
        await b.js("__wmListeners.forEach(f=>f({name:'tick',payload:'pc:act:ask:ok'}))")
        await b.until("__out===1")
        got['after_ok'] = await b.js("__out")
    _run(check)
    assert got['arg']['text'] == 'Log out?' and got['arg']['ok'] == 'Log out', got
    assert got['after_cancel'] == 0 and got['after_dismiss'] == 0 and got['after_ok'] == 1, got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_ask_window_answers_with_its_buttons_and_keys():
    got = {}

    async def check(b):
        await b.until("!!document.querySelector('.os-ask')")
        got['text'] = await b.js("document.querySelector('.os-ask-text').textContent")
        got['frame'] = await b.js("parseFloat(getComputedStyle(document.querySelector('.os-ask')).borderTopWidth)")
        await b.js("document.querySelector('[data-ask=ok]').click()")
        got['acts'] = await b.js("__acts")
    extra = POPUPS.replace("act:()=>true", "act:(a)=>{__acts.push(a);return true}") + "window.__acts=[];"
    arg = '%7B%22text%22%3A%22Log%20out%3F%22%2C%22ok%22%3A%22Log%20out%22%7D'
    asyncio.run(desktop.with_browser('online', '?pcpopup=ask&pcarg=' + arg, check, MACHINE + extra))
    assert got['text'] == 'Log out?' and got['frame'] >= 1, got
    assert any('ask:ok' in str(a) for a in got['acts']), got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_picking_an_account_in_its_window_switches_the_desktop():
    got = {}

    async def check(b):
        await _desktop(b)
        await b.js("window.__acts=[];const real=__PC.accountAct;__PC.accountAct=(w)=>{__acts.push(w)}")
        await b.js("__wmListeners.forEach(f=>f({name:'tick',payload:'pc:act:acct:add'}))")
        await b.js("__wmListeners.forEach(f=>f({name:'tick',payload:'pc:act:acct:'+__PC.me().pubkey}))")
        await asyncio.sleep(.4)
        got['acts'] = await b.js("__acts")
        got['me'] = await b.js("__PC.me().pubkey")
        got['list'] = await b.js("__PC.accountList().map(a=>a.current)")
    _run(check)
    assert got['acts'] == ['add', got['me']], got
    assert True in got['list'], got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_classic_leaves_the_desktop_and_the_logo_brings_it_back():
    """"webui shows the classic mode button but OS does not." Offered again on the machine; what makes
    that safe is the way back, so the round trip is the test."""
    got = {}

    async def check(b):
        await _desktop(b)
        await b.js(TICK % 'classic')
        await b.until("!document.body.classList.contains('os-on')")
        got['start_gone'] = await b.js("!document.querySelector('#os-start')")
        await b.until("(()=>{const l=document.querySelector('.brand-logo');return !!l&&l.getBoundingClientRect().width>0})()")
        await b.js("document.querySelector('.brand-logo').click()")
        await b.until("document.body.classList.contains('os-on') && !!document.querySelector('#os-start')")
        got['back'] = True
    _run(check)
    assert got['start_gone'] and got['back'], got
