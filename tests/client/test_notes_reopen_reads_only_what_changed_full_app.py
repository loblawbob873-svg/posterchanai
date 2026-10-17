"""Opening Notes again reads only what changed -- the whole library once per session.

server1, 2026-10-17: label reads of whole libraries topped the relay's answer cost; Notes re-read every note (up to
5,000) each time it was opened. A note is replaceable, so `since` the last complete read finds every edit.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_dm_live_subscription_is_bounded_full_app import RELAY


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_second_open_asks_since_the_first():
    got = {}
    LIB = "__reqs.filter(f=>(f['#l']||[]).includes('pcai-notes')&&f.limit===5000)"

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}; __PC.switchView('notes')")
        await b.until(LIB + ".length>=1")
        await asyncio.sleep(1.5)
        await b.js("__PC.switchView('global')")
        await asyncio.sleep(0.5)
        await b.js("__PC.switchView('notes')")
        await b.until(LIB + ".length>=2")
        got["reads"] = await b.js(LIB)

    asyncio.run(desktop.with_browser("online", "", check, RELAY))
    first, second = got["reads"][0], got["reads"][-1]
    assert "since" not in first, ("the first open must read the whole library", first)
    assert "since" in second, ("reopening Notes re-read the whole library", got["reads"])
