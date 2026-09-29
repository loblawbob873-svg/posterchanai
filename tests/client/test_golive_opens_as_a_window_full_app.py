"""Go Live is a real window on PosterChanOS, not a sheet on the desktop surface.

Reported again after the shellFront stacking fix: "Go Live -> PosterChanOS -> opening behind active
window". That fix passed against the test compositor (test_games_and_golive_open_in_front_full_app.py)
and not on a real desktop, where the desktop surface sits under every real toplevel. System Settings,
Task Manager and the installer learned the same thing and became windows of their own (os.js
EXTRA_WINDOWS); Go Live joins them.

The real bundled client, with the same compositor fixture the stacking tests use. window.open is
recorded (the desktop's windows are compositor toplevels, not tabs), and the Go Live window's page is
then loaded on its own — exactly what the shell does — with window.close recorded.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_a_focused_desktop_window_comes_in_front_full_app import COMPOSITOR, _ready
from tests.client.test_games_and_golive_open_in_front_full_app import INGEST

RECORD = r"""
window.__opened=[]; window.__closed=0;
window.open=function(u,n,f){ __opened.push(String(u)); return {closed:false,focus(){},close(){},addEventListener(){}}; };
window.close=function(){ window.__closed++; };
"""


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_desktop_opens_go_live_as_its_own_window():
    async def check(b):
        await _ready(b)
        await b.js("(document.querySelector('.os-icon[data-view=\"__golive\"]')||document.getElementById('nav-golive')).click()")
        await b.until("__opened.some(u=>u.includes('pcwin=__golive'))")
        await asyncio.sleep(.4)
        assert not await b.js("!!document.querySelector('#gl-title')"), \
            "Go Live was drawn on the desktop surface, under every open window"

    asyncio.run(desktop.with_browser("online", "", check, COMPOSITOR + INGEST + RECORD))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_go_live_window_shows_the_setup_and_closes_itself_on_cancel():
    async def check(b):
        await _ready(b)
        await b.js("location.href=location.pathname+'?pcwin=__golive'")
        await b.until("!!document.querySelector('#gl-title')")
        await b.js("document.getElementById('gl-cancel').click()")
        await b.until("window.__closed>0")

    asyncio.run(desktop.with_browser("online", "", check, COMPOSITOR + INGEST + RECORD))
