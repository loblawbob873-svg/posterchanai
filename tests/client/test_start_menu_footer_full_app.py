"""The start menu's footer, Windows-11 style: you (avatar + name → account switcher) on the left,
icon-only Settings / Power / Full screen / Classic / Log out on the right -- on the web desktop AND on
PosterChanOS, where the start menu is its own popup window.

Reported: "we need the User avatar and username like win11 … icon only on bottom-right … instead of our
ugly buttons for classic and fullscreen", "clicking on your username should allow you to switch
accounts", "make sure works on Desktop and OS", and "I see it on WebUI desktop but not OS" -- the chip
copied its name out of the sidebar card (#me-card), which a popup window never draws.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


FOOT = r'''(()=>{const f=document.querySelector('.os-foot');if(!f)return null;
  const acct=f.querySelector('.os-acct');
  return {labels:[...f.querySelectorAll('.os-foot-btn')].map(b=>b.getAttribute('aria-label')),
          worded:[...f.querySelectorAll('button')].filter(b=>!b.classList.contains('os-acct')&&b.textContent.trim()).map(b=>b.textContent.trim()),
          name:acct?acct.querySelector('span').textContent.trim():null,
          iconOnly:[...f.querySelectorAll('.os-foot-btn')].every(b=>b.querySelector('svg')&&!b.textContent.trim())}})()'''

PROFILE = r'''(async()=>{const me=__PC.me();
  const ev=await __PC.signTemplate({kind:0,pubkey:me.pubkey,created_at:Math.floor(Date.now()/1000),tags:[],
    content:JSON.stringify({name:'Alice Tester',picture:location.origin+'/static/posterchan-relay.png'})});
  Store.saveEvent(ev); Store.saveProfile(ev);
  for(let i=0;i<40&&!Store.profile(me.pubkey);i++) await new Promise(r=>setTimeout(r,50));
  const c=document.querySelector('#me-card');if(c)c.remove();return !!Store.profile(me.pubkey)})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_web_desktop_footer_is_you_and_icon_only_buttons():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on') && !!document.querySelector('#os-start')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        assert await b.js(PROFILE), 'the test profile never reached the Store'
        await b.js("document.getElementById('os-start').click()")
        await b.until("!!document.querySelector('.os-foot .os-foot-btn')")
        got.update(await b.js(FOOT))
        await b.js("document.querySelector('.os-foot-btn[data-foot=\"settings\"]').click()")
        await asyncio.sleep(1.5)
        got['after_settings'] = await b.js("__PC.VIEW || ''")
        got['settings_window'] = await b.js("!![...document.querySelectorAll('.osw')].find(w=>/settings/i.test(w.textContent.slice(0,200)))")

    asyncio.run(desktop.with_browser('online', '', check, ''))
    assert got['iconOnly'], got
    for want in ('Settings', 'Full screen (F11)', 'Classic layout -- leave the desktop', 'Log out'):
        assert want in got['labels'], (want, got['labels'])
    assert got['worded'] == [], f"worded buttons left in the footer: {got['worded']}"
    assert got['name'] == 'Alice Tester', f"the name must come from the profile, not the sidebar card: {got['name']!r}"
    assert got['after_settings'] == 'settings' or got['settings_window'], got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_posterchanos_start_popup_has_the_same_footer_and_hands_actions_to_the_desktop():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!(window.__PC && __PC.me && __PC.me())")
        # EXACTLY the OS case: the popup drew its menu before sign-in was restored. Nothing redraws it --
        # the chip must arrive by itself.
        await b.until("!!document.querySelector('.os-foot .os-acct')")
        got.update(await b.js(FOOT))
        await b.js("document.querySelector('.os-foot-btn[data-foot=\"settings\"]').click()")
        await b.js("document.querySelector('.os-foot .os-acct').click()")
        await asyncio.sleep(.5)
        got['acts'] = await b.js("window.__acts")

    extra = r"""window.__acts=[];
      (function hook(){ if(!window.pcPopup) return setTimeout(hook,10);
        pcPopup.act=async(a)=>{__acts.push(String(a));return true}; })();"""
    asyncio.run(desktop.with_browser('online', '?pcpopup=start', check, extra))
    assert got.get('iconOnly') and 'Settings' in got['labels'] and 'Log out' in got['labels'], got
    assert got['name'], "the OS start menu must show who you are"
    assert 'settings' in got['acts'] and 'accounts' in got['acts'], got['acts']
