"""Swiping Home <-> Nostrverse <-> Trending must not rebuild the page.

"lag switching timelines on his phone ... swiping between home nostrverse & trending, how long the transition"
(2026-10-16). Measured at phone speed (6x CPU throttle) on the bundled client, two costs made every swipe slow:

  * STYLE: 29 `.modal:has(#pf-save) ...` rules (the Edit Profile dialog) and four `.main:has(...)` rules made Chrome
    restyle ~1,700 elements -- the whole page -- on every change to the feed: ~120 ms of style recalculation
    before a pixel moved. They key on classes now (`.modal.pf-edit`, `.main.has-navbar/.own-search/.tg-whole`).
  * CARDS: leaving a timeline threw its cards away and coming back built every one again. The timeline you left
    keeps them (detached), and the reconcile reuses them.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_timeline_pictures_two_at_a_time_full_app import RELAY


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _restyled(b, view):
    """Elements Chrome restyled while switching to `view` (largest single recalculation)."""
    evs = []
    await b.call("Tracing.start", {"categories": "devtools.timeline", "transferMode": "ReportEvents"})
    await b.js("(async()=>{__PC.switchView('%s'); await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));})()" % view)
    await b.ws.send(json.dumps({"id": 987654, "method": "Tracing.end"}))
    while True:
        m = json.loads(await asyncio.wait_for(b.ws.recv(), 60))
        if m.get("method") == "Tracing.dataCollected":
            evs += m["params"]["value"]
        if m.get("method") == "Tracing.tracingComplete":
            break
    return max([e.get("args", {}).get("elementCount") or 0 for e in evs
                if e.get("name") == "UpdateLayoutTree" and e.get("ph") == "X"] or [0])


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_swipe_restyles_only_what_changed_and_reuses_the_cards():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 412, "height": 900, "deviceScaleFactor": 1, "mobile": True})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('global')")
        await b.until("document.querySelectorAll('#tl-notes .note').length>=50")
        await asyncio.sleep(1.0)
        await b.js("window.__mark=[...document.querySelectorAll('#tl-notes > [data-key]')].slice(0,20); __mark.forEach(n=>n.__seen=1)")
        got["trending"] = await _restyled(b, "trending")
        await asyncio.sleep(0.8)
        await b.js("__PC.switchView('global')")
        await b.until("document.querySelectorAll('#tl-notes .note').length>=50")
        got["reused"] = await b.js("[...document.querySelectorAll('#tl-notes > [data-key]')].slice(0,20).filter(n=>n.__seen).length")
        # Settings changes how cards are drawn: after visiting it, the cards are built fresh
        await b.js("__PC.switchView('settings')")
        await asyncio.sleep(0.8)
        await b.js("__PC.switchView('global')")
        await b.until("document.querySelectorAll('#tl-notes .note').length>=50")
        got["after_settings"] = await b.js("[...document.querySelectorAll('#tl-notes > [data-key]')].slice(0,20).filter(n=>n.__seen).length")
        got["dup_ids"] = await b.js("document.querySelectorAll('[id=\"tl-notes\"]').length")

    asyncio.run(desktop.with_browser("online", "", check, RELAY))
    assert got["trending"] < 300, ("a timeline swipe restyled the whole page", got)
    assert got["reused"] == 20, ("coming back to Nostrverse rebuilt the cards it had a moment ago", got)
    assert got["after_settings"] == 0, ("cards kept across a Settings visit could show a stale display setting", got)
    assert got["dup_ids"] == 1, got
