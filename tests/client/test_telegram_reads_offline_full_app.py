"""OFFLINE, TELEGRAM STILL SHOWS YOUR MESSAGES — AND HAS A RETRY.

"Telegram looks like it's missing a retry when offline and can't connect, should be able to view your
messages without an internet connection". The Telegram session lives on the node, so with no network
the screen was "Telegram is not available" and nothing else. Two phases in ONE browser profile: online,
the real client opens the chat list and a chat; then a reload with no network (navigator.onLine false,
/api/tgc unreachable, relay sockets dead) — the chats and the messages must still be there, with a
banner saying they are the device's copy and a Retry that, once the node is back, loads it fresh.
A device that never read Telegram gets the not-available card WITH a Retry.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_telegram_client_full_app import FAKE, _open


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


DOWN = r"""
if(localStorage.getItem('__dead')==='1'){
  Object.defineProperty(Navigator.prototype,'onLine',{configurable:true,get:()=>!window.__back});
  window.WebSocket=class extends EventTarget{static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
    constructor(u){super();this.url=String(u);this.readyState=0;} send(){} close(){this.readyState=3;}};
  const up=window.fetch;
  window.fetch=(url,opts)=>String(url).includes('/api/tgc/')&&!window.__back?Promise.reject(new TypeError('Failed to fetch')):up(url,opts);
}
"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [800, 1280])
def test_telegram_shows_the_kept_messages_offline_and_retry_brings_it_back(width):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=False))
        await _open(b)
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("/hey there/.test((document.querySelector('.tg-msgs')||{}).textContent||'')")
        await asyncio.sleep(1.5)                       # the device copy is written behind the paint

        await b.js("localStorage.setItem('__dead','1')")
        await b.call('Page.reload')
        await b.until('!!window.__PC && !!__PC.me()')
        await _open(b)
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        got['banner'] = await b.js("(document.querySelector('.tg-offline')||{}).textContent||''")
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("/hey there/.test((document.querySelector('.tg-msgs')||{}).textContent||'')")

        await b.js("window.__back=true")
        await b.js("document.querySelector('.tg-offline [data-act=retry]').click()")
        await b.until("document.querySelectorAll('.tg-dialog').length===2 && !document.querySelector('.tg-offline')")
        got['errors'] = await b.js('__errors')

    asyncio.run(desktop.with_browser("online", "", check,
                                     extra_init=FAKE.replace("state:'none'", "state:'ready'") + DOWN))
    assert 'kept on this device' in got['banner'], got


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_never_read_and_unreachable_still_offers_retry():
    async def check(b):
        await b.js("localStorage.setItem('__dead','1')")
        await b.call('Page.reload')
        await b.until('!!window.__PC')
        await _open(b)
        await b.until("!!document.querySelector('.tg-card [data-act=retry]')")
        await b.js("window.__back=true")
        await b.js("document.querySelector('.tg-card [data-act=retry]').click()")
        await b.until("document.querySelectorAll('.tg-dialog').length===2")

    asyncio.run(desktop.with_browser("online", "", check,
                                     extra_init=FAKE.replace("state:'none'", "state:'ready'") + DOWN))
