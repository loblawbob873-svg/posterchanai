"""The avatar top right of a window's title row is the new round PosterChan badge ("maybe we should update the top
right window posterchan logo with your new one", 2026-10-29).

`--pc-logo` (client.css) draws it: a phone's title rows and every PosterChanOS window. It was the old hooded portrait
in a rounded square; it is the app icon's face badge now, drawn round. Measured on the shipped client at phone width,
where every screen's title row carries it: what the browser actually paints there.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_title_row_avatar_is_the_round_app_badge():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=390, height=844, deviceScaleFactor=3, mobile=True))
        await desktop.login(b)
        await b.js("__PC.switchView('notifications')")
        await b.until("!!document.querySelector('.main:not(.has-navbar)>.topbar')")
        got['after'] = await b.js("""(()=>{const t=document.querySelector('.main:not(.has-navbar)>.topbar'),cs=getComputedStyle(t,'::after');
          return {bg:cs.backgroundImage, radius:cs.borderTopLeftRadius, w:cs.width};})()""")
        got['loads'] = await b.js("""new Promise(r=>{const m=/url\\("?([^")]+)/.exec(getComputedStyle(document.querySelector('.main:not(.has-navbar)>.topbar'),'::after').backgroundImage);
          if(!m) return r(false); const i=new Image(); i.onload=()=>r(i.naturalWidth); i.onerror=()=>r(false); i.src=m[1];})""")

    asyncio.run(desktop.with_browser('online', '', check))
    a = got['after']
    assert 'posterchan-badge.png' in a['bg'], ("the window avatar is not the new badge", got)
    assert a['radius'] == '50%', ("the badge is not drawn round", got)
    assert got['loads'] and got['loads'] >= 108, ("the badge image does not load (or is too small for a 3x screen)", got)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_sidebar_logo_and_the_start_button_are_the_badge_too():
    """"switch the sidebar and start button logo too" (2026-10-29). The start button wears whatever the sidebar's brand
    logo shows (os.js brandLogo), so both are measured: the sidebar at desktop width, the start button in desktop mode."""
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=1440, height=900, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        got['sidebar'] = await b.js("""(()=>{const i=document.querySelector('.sidebar .brand-logo'),cs=getComputedStyle(i);
          return {src:i.getAttribute('src'), radius:cs.borderTopLeftRadius, w:i.getBoundingClientRect().width, loaded:i.complete&&i.naturalWidth};})()""")
        await b.js("try{ if(window.PCOS && !PCOS.isOn()) PCOS.enter(); }catch(e){}")
        await b.until("!!document.querySelector('.os-start img')")
        await b.until("document.querySelector('.os-start img').complete")
        got['start'] = await b.js("""(()=>{const i=document.querySelector('.os-start img'),cs=getComputedStyle(i);
          return {src:i.getAttribute('src'), radius:cs.borderTopLeftRadius, loaded:i.naturalWidth};})()""")

    asyncio.run(desktop.with_browser('online', '', check))
    s, st = got['sidebar'], got['start']
    assert 'posterchan-badge.png' in s['src'] and s['loaded'], ("the sidebar logo is not the badge", got)
    assert s['radius'] == '50%', ("the sidebar badge is not drawn round", got)
    assert 'posterchan-badge.png' in st['src'] and st['loaded'], ("the start button is not the badge", got)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_the_sign_in_screen_logo_is_the_badge(width):
    """"switch the sign-in screen logo too" (2026-10-29): the logo on the sign-in card, as a signed-out visitor sees it."""
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=2, mobile=width < 600))
        # A guest presses Log in (#btn-logout reads "Log in" for a guest) and the sign-in card opens.
        await b.until("!!document.querySelector('#btn-logout') && document.querySelector('#btn-logout').textContent.trim()==='Log in'")
        await b.js("document.querySelector('#btn-logout').click()")
        await b.until("!!document.querySelector('.auth-card .logo-img') && document.querySelector('.auth-card .logo-img').getBoundingClientRect().width>0")
        await b.until("document.querySelector('.auth-card .logo-img').complete")
        got['logo'] = await b.js("""(()=>{const i=document.querySelector('.auth-card .logo-img'),cs=getComputedStyle(i);
          return {src:i.getAttribute('src'), radius:cs.borderTopLeftRadius, loaded:i.naturalWidth, w:i.getBoundingClientRect().width};})()""")

    asyncio.run(desktop.with_browser('online', '', check))
    g = got['logo']
    assert 'posterchan-badge.png' in g['src'] and g['loaded'], ("the sign-in screen's logo is not the badge", got)
    assert g['radius'] == '50%', ("the sign-in badge is not drawn round", got)
