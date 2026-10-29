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
