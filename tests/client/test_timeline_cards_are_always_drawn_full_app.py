"""Timeline cards are always drawn on the DESKTOP layout -- no content-visibility skipping (laptop, desktop, tablet).

"i am scrolling down the social feed on laptop now and the entire timeline is jumpy when going over
attachments" / "images are triplicating on the timeline and blurrying together and flashing basically" /
"same for video". `content-visibility:auto` on every timeline card (2026-09-30, a slow-phone speed-up) let
the browser skip drawing off-screen cards and re-draw them as they scrolled in; on the PosterChanOS laptop
the compositor drew those re-entering pictures and videos in several places at once. No screenshot captured
it and the DOM never moved, so the only proof was A/B on the live window: the rule switched off there, and
"working great". A compositor fault cannot be photographed by a headless browser, so this pins the cause:
every card, on screen or 3000px below it, is laid out and drawn. The PHONE layout keeps the skipping -- it
is worth 2-3x less blocking there -- and is covered by test_timeline_offscreen_cards_cost_nothing_full_app.py.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_timeline_offscreen_cards_cost_nothing_full_app import RELAY


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width,mobile", [(1366, False), (1920, False), (1340, True)])
def test_no_timeline_card_is_ever_skipped(width, mobile):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 800, "deviceScaleFactor": 1, "mobile": mobile})
        await desktop.login(b)
        await b.js("__PC.switchView('global')")
        await b.until("document.querySelectorAll('#feed .note').length>=40")
        await asyncio.sleep(.8)
        got.update(await b.js(r"""(()=>{const feed=document.getElementById('feed'),fr=feed.getBoundingClientRect();
          const cards=[...document.querySelectorAll('#tl-notes > .note, #tl-notes > .reply-pair')];
          const inner=c=>c.querySelector('.name')||c.firstElementChild;
          const far=cards.filter(c=>c.getBoundingClientRect().top>fr.bottom+3000);
          return {cards:cards.length, far:far.length,
            notVisible:cards.filter(c=>getComputedStyle(c).contentVisibility!=='visible').length,
            skipped:far.filter(c=>!inner(c).checkVisibility({contentVisibilityAuto:true})).length}})()"""))

    asyncio.run(desktop.with_browser("online", "", check, RELAY))
    assert got["cards"] >= 40 and got["far"] >= 5, got
    assert got["notVisible"] == 0, ("timeline cards are content-visibility:auto again -- the laptop glitch", got)
    assert got["skipped"] == 0, ("off-screen cards are skipped, and are re-drawn as they scroll in", got)
