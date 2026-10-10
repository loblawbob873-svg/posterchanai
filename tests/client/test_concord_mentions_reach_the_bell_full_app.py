"""A Concord mention lights the bell and is listed in Notifications until it is read.

"i got tagged twice in a concord room today but never got notification". The ledger is written by
concord.js (concord_mentions_ledger_runtime.mjs covers that half); here the REAL client must count it
in the bell -- including when another window wrote it -- list it as a row, and open Communities from it.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_dm_newest_first_full_app import RELAY


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


BELL = ("[...document.querySelectorAll('#notif-badge,#notif-badge-m,#rb-notif-badge')]"
        ".filter(b=>!b.classList.contains('hidden')&&b.getClientRects().length).map(b=>b.textContent)")
LEDGER = {"c" * 64 + "\ngeneral": {"room": "c" * 64, "name": "Lounge Chat", "channel": "general",
                                     "ids": ["m1", "m2"], "at": 1, "last": "m2"}}


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_two_mentions_light_the_bell_and_open_the_room():
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=1280, height=900, deviceScaleFactor=1, mobile=False))
        await b.until("document.body.classList.contains('guest')")
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        assert not any(int(t or 0) >= 2 for t in await b.js(BELL)), "lit before any mention"
        # Another window (a monitor, a popped-out Communities) records the mentions.
        val = json.dumps(json.dumps(LEDGER))
        # The ledger is PER ACCOUNT. One an older build wrote at the shared key -- possibly about the
        # OTHER account on this device -- is not this account's and must light nothing.
        await b.js(f"""(()=>{{const k='pc.concord.mentions.v1',nv={val};localStorage.setItem(k,nv);
            window.dispatchEvent(new StorageEvent('storage',{{key:k,oldValue:null,newValue:nv,storageArea:localStorage}}));}})()""")
        await asyncio.sleep(.5)
        assert not any(int(t or 0) >= 2 for t in await b.js(BELL)), "another account's (pre-separation) mentions lit this bell"
        await b.js(f"""(()=>{{const k='pc.concord.mentions.v1.'+__PC.me().pubkey,nv={val};localStorage.setItem(k,nv);
            window.dispatchEvent(new StorageEvent('storage',{{key:k,oldValue:null,newValue:nv,storageArea:localStorage}}));}})()""")
        await b.until(BELL + ".some(t=>Number(t)>=2)")
        await b.js("__PC.switchView('notifications')")
        await b.until("!!document.querySelector('#feed .cc-mention-notif')")
        row = await b.js("document.querySelector('#feed .cc-mention-notif').innerText")
        assert "2 mentions" in row and "Lounge Chat" in row and "#general" in row, row
        await b.js("document.querySelector('#feed .cc-mention-notif').click()")
        await b.until("document.body.classList.contains('concord-view')")
        assert not await b.js('__errors'), await b.js('__errors')
    asyncio.run(desktop.with_browser('online', '', check, RELAY))
