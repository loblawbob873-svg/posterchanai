"""Joining from an invite closes the join sheet.

Reported: "Concord joining a community, you enter an invite URL, click Preview, then Join, the modal never
disappears". render() remembers which sheets are open and reopens them after it redraws (so a background refresh
cannot shut a sheet somebody is typing in) -- and the sheet was still open when Join's own render ran, so the join
reopened it, invite URL and all. Decline worked only because it hid the sheet again AFTER its render.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_join_from_the_preview_closes_the_sheet_and_opens_the_community():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}; __PC.switchView('concord'); true")
        await b.until("!!document.getElementById('cc-welcome-join') && !!window.PCConcord && !!PCConcord.__testInvite")
        await b.js("document.getElementById('cc-welcome-join').click(); true")
        await b.js("document.getElementById('cc-invite-url').value='https://x.invalid/invite/naddr1abc#k'; true")
        await b.js("PCConcord.__testInvite({url:'https://x.invalid/invite/naddr1abc#k',"
                   "room:{name:'Fixture Club',communityId:'fixture-club-1',relays:[]}}); true")
        await b.until("!!document.getElementById('cc-invite-accept')")
        got["open_before"] = await b.js("!document.getElementById('cc-join').classList.contains('hidden')")
        await b.js("document.getElementById('cc-invite-accept').click(); true")
        await b.until("/Fixture Club/.test(document.body.innerText)")
        for _ in range(20):
            await asyncio.sleep(.1)
        await b.js("PCConcord.render(); true")      # a later refresh must not bring it back either
        await asyncio.sleep(.3)
        got["open_after"] = await b.js("!document.getElementById('cc-join').classList.contains('hidden')")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["open_before"], ("fixture: the preview was expected inside the open sheet", got)
    assert not got["open_after"], ("Join left the join sheet on screen", got)
