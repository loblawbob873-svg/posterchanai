"""A Concord room opened at its newest message STAYS there when the message area gets shorter.

"clicking on room does not always go to the bottom, see posterchan room" -- "slightly above latest message"
(2026-10-29). The room was pinned to the bottom and kept there by a ResizeObserver on the message LIST, which sees
the content grow -- but not the scroller itself get shorter: a composer that grows (a draft restored, several lines
typed), a reply or typing bar, an attachment tray, a phone keyboard. The scroll position stayed put, the viewport's
bottom edge moved up, and the newest message sat just under it. The real client, real minted CORD communities on an
in-page relay (the harness of test_concord_switch_is_steady_full_app.py), the area made shorter by the "is typing" line and by the reply bar.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.concord_fixture_relay import INSTALL
from tests.client.test_concord_switch_is_steady_full_app import BUILD, IMAGES, LATENCY, NO_OS, settle


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


AT = """(()=>{const m=document.querySelector('.cc-messages');
  return {fromBottom: Math.round(m.scrollHeight-m.clientHeight-m.scrollTop), h: m.clientHeight};})()"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_newest_message_stays_in_view_when_the_composer_grows():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=1280, height=800, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js(NO_OS)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('concord')")
        await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')
        await b.js(INSTALL); await b.js(LATENCY); await b.js(IMAGES)
        counts = await b.js(BUILD)
        await b.js('PCConcord.render()')
        await b.until("!!document.querySelector('[data-cc-server=\"0\"]')")
        await b.js("document.querySelector('[data-cc-server=\"0\"]').click()")
        await b.until("!!document.querySelector('[data-cc-channel=\"general\"]')")
        await b.js("document.querySelector('[data-cc-channel=\"general\"]').click()")
        await b.until(f"document.querySelectorAll('.cc-message').length>={counts['Alpha/general']}")
        await settle(b, 3)
        got['entered'] = await b.js(AT)
        # 1. Somebody starts typing: the "is typing" line (18px, .cc-typing.on) opens under the messages.
        await b.js("document.querySelector('#cc-typing').textContent='Alice is typing…';document.querySelector('#cc-typing').classList.add('on')")
        await asyncio.sleep(0.6)
        got['typing'] = await b.js(AT)
        await b.js("document.querySelector('#cc-typing').classList.remove('on')")
        await asyncio.sleep(0.6)
        # 2. Reply to the newest message: the reply bar opens under the messages (a real click).
        await b.js("(()=>{const r=[...document.querySelectorAll('[data-cc-reply]')].pop(); r.click();})()")
        await b.until("!document.querySelector('#cc-reply').classList.contains('hidden')")
        await asyncio.sleep(0.6)
        got['reply'] = await b.js(AT)
        got['errors'] = await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check))
    e = got['entered']
    assert e['fromBottom'] <= 2, ("the room did not open at its newest message", e)
    for what in ('typing', 'reply'):
        t = got[what]
        assert t['h'] < e['h'], (f"the {what} bar did not make the message area shorter, so this proves nothing", e, t)
        assert t['fromBottom'] <= 2, (f"the newest message went {t['fromBottom']}px under the fold when the {what} bar opened", e, t)
    assert got['errors'] == []


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('latency', [0, 2500])
def test_coming_back_to_a_room_lands_on_its_newest_message(latency):
    """"just click on 1 room, then go back to general, and the scroll position won't be at the newest" (2026-10-29,
    the desktop app). Real clicks on the channel list: general (newest), random, general again."""
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=1280, height=800, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js(NO_OS)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('concord')")
        await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')
        await b.js(INSTALL); await b.js(LATENCY); await b.js(IMAGES)
        counts = await b.js(BUILD)
        await b.js('PCConcord.render()')
        await b.until("!!document.querySelector('[data-cc-server=\"0\"]')")
        await b.js("document.querySelector('[data-cc-server=\"0\"]').click()")
        await b.until("!!document.querySelector('[data-cc-channel=\"general\"]')")
        await b.js("document.querySelector('[data-cc-channel=\"general\"]').click()")
        await b.until(f"document.querySelectorAll('.cc-message').length>={counts['Alpha/general']}")
        await settle(b, 3)
        got['first'] = await b.js(AT)
        await b.js(f"window.__lat={latency}")     # a room's history refresh over a real relay / the tunnel
        await b.js("document.querySelector('[data-cc-channel=\"random\"]').click()")
        await b.until(f"document.querySelectorAll('.cc-message').length>={counts['Alpha/random']} && document.querySelectorAll('.cc-message').length<{counts['Alpha/general']}")
        await settle(b, 3)
        got['other'] = await b.js(AT)
        await b.js("document.querySelector('[data-cc-channel=\"general\"]').click()")
        await b.until(f"document.querySelectorAll('.cc-message').length>={counts['Alpha/general']}")
        samples = []
        for _ in range(28):            # the first 7 s back -- past the refresh: every sample, not just the last one
            await asyncio.sleep(0.25)
            samples.append(await b.js(AT))
        got['back'] = samples
        got['errors'] = await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check))
    assert got['first']['fromBottom'] <= 2, ("the room did not open at its newest message", got['first'])
    assert got['other']['fromBottom'] <= 2, ("the other room did not open at its newest message", got['other'])
    worst = max(s['fromBottom'] for s in got['back'][2:])     # half a second to land, then it must stay
    assert worst <= 2, ("back in general, the newest message is %dpx under the fold" % worst, got['back'])
    assert got['errors'] == []


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_only_the_reader_can_unpin_a_room():
    """"the scroll is not at the latest message" (the laptop, 2026-10-29, measured live: the newest message 836 px under
    the fold and the room's saved state pinned:false with nobody having scrolled). Any scroll the page did not cause
    itself was judged by distance and unpinned the room for good -- after that, the window being resized at startup
    and the pictures settling all stayed under the fold. A scroll nobody made (here: the position moved by script, as
    a resize or a clamp moves it) must leave the room at its newest message, through the growth that follows; a real
    wheel must still unpin it, and stay where the reader put it."""
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=1280, height=800, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js(NO_OS)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('concord')")
        await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')
        await b.js(INSTALL); await b.js(LATENCY); await b.js(IMAGES)
        counts = await b.js(BUILD)
        await b.js('PCConcord.render()')
        await b.until("!!document.querySelector('[data-cc-server=\"0\"]')")
        await b.js("document.querySelector('[data-cc-server=\"0\"]').click()")
        await b.until("!!document.querySelector('[data-cc-channel=\"general\"]')")
        await b.js("document.querySelector('[data-cc-channel=\"general\"]').click()")
        await b.until(f"document.querySelectorAll('.cc-message').length>={counts['Alpha/general']}")
        await settle(b, 3)
        got['entered'] = await b.js(AT)
        # Nobody touches anything: the position moves by 500px, as a resize or a clamp moves it.
        await b.js("document.querySelector('.cc-messages').scrollTop-=500")
        await asyncio.sleep(0.6)
        got['moved'] = await b.js(AT)
        # ...and then the room grows, as pictures settle.
        await b.js("(()=>{const r=[...document.querySelectorAll('.cc-message')].pop(),d=document.createElement('div');d.style.height='300px';r.appendChild(d);})()")
        await asyncio.sleep(0.6)
        got['grown'] = await b.js(AT)
        # A real wheel, up: the reader leaves the bottom, and the room stays where they put it.
        for _ in range(4):
            await b.call('Input.dispatchMouseEvent', {'type': 'mouseWheel', 'x': 700, 'y': 400, 'deltaX': 0, 'deltaY': -300})
            await asyncio.sleep(0.15)
        await asyncio.sleep(0.8)
        got['wheeled'] = await b.js(AT)
        await asyncio.sleep(1.5)
        got['later'] = await b.js(AT)
        got['errors'] = await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check))
    assert got['entered']['fromBottom'] <= 2, got
    assert got['moved']['fromBottom'] <= 2, ("a scroll nobody made took the room off its newest message", got)
    assert got['grown']['fromBottom'] <= 2, ("after a scroll nobody made, the room no longer followed its newest message", got)
    assert got['wheeled']['fromBottom'] > 300, ("a real wheel did not move the reader up -- the room is fighting them", got)
    assert abs(got['later']['fromBottom'] - got['wheeled']['fromBottom']) <= 2, ("the room pulled a reader who had scrolled up back down", got)
    assert got['errors'] == []
