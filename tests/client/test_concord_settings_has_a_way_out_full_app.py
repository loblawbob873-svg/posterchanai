"""Concord: Community settings can always be LEFT, and Save says what happened.

Reported 2026-10-11: "Concord -> No way to leave Community Settings", then "clicking Save also does not
leave the modal". Measured on the shipped sheet before the fix: the only exit was a "Cancel" in a row of
five identically-styled buttons (Leave community, New private channel, Rotate community keys...) at the
bottom of a scrolling card; there was no close button, Escape did nothing, and the Android back button
walked the app's history out from under the sheet (leaving the community view with the sheet's edits
gone). Save greyed itself out for as long as the relays took with no sign of progress, and a refused
save was a three-second toast -- so a slow or refused save read as "Save does nothing".

Real client, real minted CORD community, real signer, real DOM and real input events (Chrome's mouse and
keyboard pipeline). Only the relay is an in-page fixture (tests/client/concord_fixture_relay.py), and
the Android back button is the Capacitor App plugin's `backButton` listener, captured the way the APK
registers it. Every route is driven at desktop width, at phone width (390px) and inside a desktop-mode
(PosterChanOS) window, and each one must land the person back on the SAME community with the sheet
gone -- not merely hidden by a class while still covering the screen.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.concord_fixture_relay import INSTALL, MINT, OPEN_ROOM
from tests.client.test_concord_channel_controls_full_app import click


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


NAME = 'Way out'
# The APK's hardware back button: app.js registers it through Capacitor's App plugin.
BACK = ("window.__back=null;window.Capacitor={isNativePlatform:()=>false,Plugins:{App:{addListener:(name,fn)=>"
        "{if(name==='backButton')window.__back=fn;return {remove(){}};}}}};")
NOT_OS = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse("
          "localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")

OPEN = "(()=>{const d=document.querySelector('#cc-settings-dialog');return !!d&&!d.classList.contains('hidden')})()"
# "Gone" is measured, not read off a class: nothing of the sheet is painted and the community's own
# header button is what the middle of the screen hits again.
STATE = r"""(()=>{const d=document.querySelector('#cc-settings-dialog'),r=d?d.getBoundingClientRect():null;
  const painted=!!d&&getComputedStyle(d).display!=='none'&&r.width>0&&r.height>0;
  const head=document.querySelector('.cc-channels>header b');
  const opener=document.querySelector('#cc-edit-icon'),o=opener&&opener.getBoundingClientRect();
  const hit=o&&o.width>0?document.elementFromPoint(o.x+o.width/2,o.y+o.height/2):null;
  return {painted,view:window.__PC&&__PC.VIEW,community:head?head.textContent.trim():'',
          openerReachable:!!hit&&opener.contains(hit)};})()"""


async def _boot(b, width, where):
    await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=820, deviceScaleFactor=1, mobile=width < 600))
    await desktop.login(b)
    if where == 'desk':
        await b.js("PCOS.isOn()||PCOS.enter()")
        await b.until("PCOS.isOn()")
        await b.js("PCOS.routeView('concord')")
    else:
        await b.js("__PC.switchMessagesTab('concord')")
    await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')
    await b.js(INSTALL)
    minted = await b.js(MINT.replace('NAME', json.dumps(NAME)))
    await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')
    await b.until("!!document.querySelector('#cc-edit-icon')")
    await b.js("window.__toasts=[];__PC.toast=m=>__toasts.push(String(m));")


async def _open(b):
    await click(b, '#cc-edit-icon')
    await b.until(OPEN)
    # Scroll the card to its END first: the way out must still be on screen, not only at the top.
    await b.js("(()=>{const c=document.querySelector('#cc-settings-dialog .cc-join-card');c.scrollTop=c.scrollHeight;})()")
    await asyncio.sleep(.15)


async def _key(b, key, code, vk):
    for kind in ('keyDown', 'keyUp'):
        await b.call('Input.dispatchKeyEvent', dict(type=kind, key=key, code=code, windowsVirtualKeyCode=vk))


async def _assert_left(b, before, route):
    await asyncio.sleep(.4)
    after = await b.js(STATE)
    assert not after['painted'], (route, 'the settings sheet is still on screen', after)
    assert after['community'] == NAME, (route, 'not back on the community', after)
    assert after['view'] == before['view'], (route, 'the app left the community view', before, after)
    assert after['openerReachable'], (route, 'the community header is covered', after)


async def _save_succeeds(b, before):
    # A save that SUCCEEDS, slowly (a real relay round trip). While it runs Save says so; when it
    # lands the sheet closes by itself, the person is back on the community, and a toast says it saved.
    await b.js("window.__accept=window.__accept||__PC.relayPublishRoom;window.__release=null;__PC.relayPublishRoom=async(...a)=>{await new Promise(r=>{window.__release=r;});return window.__accept(...a);};")
    await b.js("(()=>{const d=document.querySelector('#cc-description-value');d.value='Our saved description';d.dispatchEvent(new Event('input',{bubbles:true}));})()")
    await click(b, '#cc-settings-save')
    await b.until('typeof window.__release==="function"')
    busy = await b.js("(()=>{const s=document.querySelector('#cc-settings-save');return {text:s.textContent.trim(),disabled:s.disabled,"
                      "error:(e=>!!e&&!e.hidden)(document.querySelector('#cc-settings-error'))}})()")
    assert busy == {'text': 'Saving…', 'disabled': True, 'error': False}, ('Save shows no progress', busy)
    await b.js('window.__release()')
    await b.until('!(' + OPEN + ')')
    await _assert_left(b, before, 'save')
    assert any(t == 'Settings saved' for t in await b.js('__toasts')), await b.js('__toasts')
    saved = await b.js("JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey))[0].description")
    assert saved == 'Our saved description'


VARIANTS = [(1280, 'page'), (390, 'page'), (1280, 'desk')]


ROUTES = ['close button', 'back arrow', 'Escape', 'Android back', 'Cancel']


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('route', ROUTES)
@pytest.mark.parametrize('width,where', VARIANTS, ids=['desktop', 'phone', 'desktop-mode-window'])
def test_every_way_out_of_community_settings_lands_on_the_community(width, where, route):
    if route == 'back arrow' and width >= 600:
        pytest.skip('the back arrow is the phone layout; desktop has the close button')
    got = {}

    async def check(b):
        await _boot(b, width, where)
        before = await b.js(STATE)
        assert before['community'] == NAME and not before['painted'], before
        for attempt in range(2):   # twice: the second open is the sheet render() rebuilt after the first close
            await _open(b)
            if route in ('close button', 'back arrow'):
                sel = '#cc-settings-close' if route == 'close button' else '#cc-settings-back'
                assert await b.js(f"!!document.querySelector('{sel}')"), (route, 'the sheet has no such control')
            if route == 'close button':
                await click(b, '#cc-settings-close')   # asserts the close button is visible and not covered
            elif route == 'back arrow':
                await click(b, '#cc-settings-back')
            elif route == 'Cancel':
                await click(b, '#cc-settings-cancel')
            elif route == 'Escape':
                await _key(b, 'Escape', 'Escape', 27)
            elif route == 'Android back':
                assert await b.js('typeof window.__back==="function"'), 'the app registered no back-button handler'
                await b.js('window.__back()')
            await _assert_left(b, before, f'{route} #{attempt + 1}')
        got['errors'] = await b.js('window.__errors||[]')

    asyncio.run(desktop.with_browser('online', '', check, BACK + ('' if where == 'desk' else NOT_OS)))
    assert not got.get('errors'), got['errors']


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width,where', VARIANTS, ids=['desktop', 'phone', 'desktop-mode-window'])
def test_save_closes_on_success_and_stays_open_with_the_reason_on_refusal(width, where):
    async def check(b):
        await _boot(b, width, where)
        before = await b.js(STATE)
        # 1. REFUSED: the community's relays reject the update. The sheet stays, the edit stays, the
        #    reason is written INSIDE the sheet where it can be seen, and Save can be pressed again.
        await b.js("window.__accept=__PC.relayPublishRoom;__PC.relayPublishRoom=async()=>({ok:false,msg:'blocked: fixture relay refuses'});")
        await _open(b)
        await b.js("(()=>{const d=document.querySelector('#cc-description-value');d.value='Our refused description';d.dispatchEvent(new Event('input',{bubbles:true}));})()")
        await click(b, '#cc-settings-save')
        await b.until("!!(document.querySelector('#cc-settings-error')?.textContent||'').trim()")
        refused = await b.js(r"""(()=>{const e=document.querySelector('#cc-settings-error'),r=e.getBoundingClientRect(),
            hit=document.elementFromPoint(r.x+Math.min(20,r.width/2),r.y+r.height/2),s=document.querySelector('#cc-settings-save');
            return {text:e.textContent,visible:!e.hidden&&r.width>0&&r.height>0&&r.top>=0&&r.bottom<=innerHeight&&!!hit&&e.contains(hit),
              value:document.querySelector('#cc-description-value').value,saveEnabled:!s.disabled,saveText:s.textContent.trim()}})()""")
        assert await b.js(OPEN), 'a refused save closed the sheet and threw the edits away'
        assert 'Not saved' in refused['text'] and 'relays rejected' in refused['text'], refused
        assert refused['visible'], ('the reason is not visible on screen', refused)
        assert refused['value'] == 'Our refused description', refused
        assert refused['saveEnabled'] and refused['saveText'] == 'Save changes', refused

        # 2. Then a save that SUCCEEDS from that same sheet.
        await _save_succeeds(b, before)
        # Re-opened after a refusal and a success: no stale error left behind.
        await _open(b)
        assert await b.js("document.querySelector('#cc-settings-error').hidden")
        assert not await b.js('(window.__errors||[]).length'), await b.js('window.__errors')

    asyncio.run(desktop.with_browser('online', '', check, BACK + ('' if where == 'desk' else NOT_OS)))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width,where', VARIANTS, ids=['desktop', 'phone', 'desktop-mode-window'])
def test_a_save_that_succeeds_shows_progress_then_closes_onto_the_community(width, where):
    async def check(b):
        await _boot(b, width, where)
        before = await b.js(STATE)
        await _open(b)
        await _save_succeeds(b, before)
        assert not await b.js('(window.__errors||[]).length'), await b.js('window.__errors')

    asyncio.run(desktop.with_browser('online', '', check, BACK + ('' if where == 'desk' else NOT_OS)))
