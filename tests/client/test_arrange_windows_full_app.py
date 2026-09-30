"""The taskbar's Arrange button: a picker in a window of its own, and the choice arranges this monitor.

"an easy way to grid everything on a desktop or split". Real bundle as the PosterChanOS desktop; the
machine's bridges are fixtures. The button opens the picker as a POPUP WINDOW (never a menu on the
desktop surface, which sits under the windows it would arrange); the picker offers Grid / Side by side
/ Top & bottom; the answer comes back as pc:act:arrange:<layout> and the desktop asks the window
manager to arrange -- the WM arranges the monitor of the surface that asked (tests/test_window_arrange.py).
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_start_footer_buttons_do_something_full_app import MACHINE, _desktop
from tests.client.test_start_footer_never_draws_under_windows_full_app import POPUPS, IN_PAGE


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


ARRANGE = "window.__arranged=[];window.pcWM.arrange=async(l)=>{__arranged.push(l);return {ok:true,count:4}};"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_taskbar_button_opens_the_picker_window_and_the_choice_arranges():
    got = {}

    async def check(b):
        await _desktop(b)
        await b.until("!!document.querySelector('#os-arrange')")
        await b.js("window.__popups=[];document.querySelector('#os-arrange').click()")
        await asyncio.sleep(.5)
        got['popups'] = await b.js("__popups.map(p=>p.kind)")
        got['in_page'] = await b.js(IN_PAGE)
        await b.js("__wmListeners.forEach(f=>f({name:'tick',payload:'pc:act:arrange:grid'}))")
        await b.until("__arranged.length===1")
        got['arranged'] = await b.js("__arranged")
    asyncio.run(desktop.with_browser('online', '', check, MACHINE + POPUPS + ARRANGE))
    assert got['popups'] == ['arrange'], got
    assert not got['in_page'], ('the picker was drawn on the desktop surface', got)
    assert got['arranged'] == ['grid'], got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_picker_offers_three_layouts_and_answers_with_the_one_picked():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("document.querySelectorAll('.os-arrange-pop [data-arrange]').length===3")
        got['layouts'] = await b.js("[...document.querySelectorAll('[data-arrange]')].map(x=>x.dataset.arrange)")
        got['labels'] = await b.js("[...document.querySelectorAll('[data-arrange] b')].map(x=>x.textContent)")
        await b.js("document.querySelector('[data-arrange=side-by-side]').click()")
        got['acts'] = await b.js("__acts")
    extra = POPUPS.replace("act:()=>true", "act:(a)=>{__acts.push(a);return true}") + "window.__acts=[];" + ARRANGE
    asyncio.run(desktop.with_browser('online', '?pcpopup=arrange', check, MACHINE + extra))
    assert got['layouts'] == ['grid', 'side-by-side', 'stacked'], got
    assert got['labels'] == ['Grid', 'Side by side', 'Top & bottom'], got
    assert any('arrange:side-by-side' in str(a) for a in got['acts']), got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_no_button_where_windows_cannot_be_arranged():
    """The web desktop (no window manager bridge): no button that could only fail."""
    got = {}

    async def check(b):
        await _desktop(b)
        got['button'] = await b.js("!!document.querySelector('#os-arrange')")
    asyncio.run(desktop.with_browser('online', '', check, MACHINE))       # no pcPopup, no arrange
    assert got['button'] is False, got
