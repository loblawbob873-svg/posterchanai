"""Concord's community list has "Check my communities", and on a phone it opens a readable report.

Reported 2026-10-09: "I don't see the developers room on phone", after the vault held the room and
its invite resolved from a desktop. Every step of the membership pass fails into a silent
`continue`, and there is no device here to debug, so the phone has to say what it measured. The
report logic is driven in concord_runtime.mjs; this proves the button is reachable on the shipped
page at phone and desktop width, that it opens a report with real lines in it (not an empty modal),
and that the report stays inside the screen.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [390, 1280])
def test_check_my_communities_opens_a_report_on_the_community_list(width):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=850, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('concord')")
        await b.until("!!document.getElementById('cc-check-memberships')")
        await b.js("document.getElementById('cc-check-memberships').click()")
        await b.until("((document.getElementById('cc-mcheck-out')||{}).textContent||'').length>0")
        got.update(await b.js("""(()=>{const o=document.getElementById('cc-mcheck-out'),r=o.getBoundingClientRect(),
            btn=document.getElementById('cc-check-memberships').getBoundingClientRect();
          return {text:o.textContent, inside:r.left>=0&&r.right<=innerWidth+1, btnInside:btn.right<=innerWidth+1&&btn.width>0,
                  copy:!document.getElementById('cc-mcheck-copy').hidden}})()"""))

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["btnInside"], got
    assert "Membership documents found:" in got["text"], ("the report says nothing it measured", got["text"][:300])
    assert got["inside"], ("the report runs off the screen", got)
    assert got["copy"], "no way to copy the report off the phone"
