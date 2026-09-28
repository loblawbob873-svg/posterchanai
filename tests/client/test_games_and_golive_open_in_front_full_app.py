"""Nostr Games and Go Live open IN FRONT of the window you were using, on PosterChanOS.

Reported: "Go Live, Nostr Games -> On PosterChanOS, the window opens behind Social or other active
windows." Both are drawn INSIDE the desktop surface -- the Games folder is an in-page window, Go Live
is a sheet (a modal) -- and the desktop surface only shows above the real toplevels when the page
publishes `pcWM.shellFront({front:true, covers:[…]})` naming the windows it overlaps (see
test_a_focused_desktop_window_comes_in_front_full_app.py for the mechanism).

The compositor is the same fixture that file uses: the desktop surface, popped-out Notes and
Terminal, and a Firefox window over nearly the whole output that holds the keyboard. Each case opens
the thing the way the desktop does and asks what the page told the compositor.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_a_focused_desktop_window_comes_in_front_full_app import (
    COMPOSITOR, _last_front, _ready, _until_front)

INGEST = r"""
const __fi=window.fetch;
window.fetch=function(url,opts){ const u=String(url);
  if(u.includes('/api/streams/ingest')) return Promise.resolve(new Response(JSON.stringify({enabled:true,
    rtmp_url:'rtmp://fixture.invalid/live',stream_key:'k3y',token:'tok123',
    hls_url:'https://fixture.invalid/hls/tok123/index.m3u8'}),{status:200,headers:{'Content-Type':'application/json'}}));
  return __fi(url,opts); };
"""


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("focused", [40, 35], ids=["firefox", "a-popped-out-posterchan-window"])
def test_the_games_folder_opens_over_the_window_you_were_using(focused):
    """Over Firefox AND over one of our own popped-out windows (Social, in the report) -- the desktop
    treats a foreign app and its own windows differently, so both are asked."""
    async def check(b):
        await _ready(b)
        await b.js(f"__wm.focus={focused};__wm.emit&&__wm.emit({{name:'window',change:'focus'}})")
        await asyncio.sleep(.5)
        await b.until("!!document.querySelector('.os-icon[data-view=\"folder:games\"]')")
        await b.js("document.querySelector('.os-icon[data-view=\"folder:games\"]').click()")
        await b.until("[...document.querySelectorAll('.osw .osw-title')].some(t=>/games/i.test(t.textContent))")
        await _until_front(b, str(focused))
        last = _last_front(await b.js("__wm.log"))
        assert last["front"] is True and focused in last["covers"], \
            f"the Games folder was left behind window {focused}: {await b.js('__wm.log')}"

    asyncio.run(desktop.with_browser("online", "", check, COMPOSITOR + INGEST))


GO_LIVE_OPENERS = {
    "api": "window.__PC.goLive ? __PC.goLive() : document.getElementById('nav-golive').click()",
    # The way a person opens it on PosterChanOS: the desktop icon / start-menu entry.
    "desktop-icon": "(document.querySelector('.os-icon[data-view=\"__golive\"],.os-icon[data-view=\"golive\"]')"
                    "||document.getElementById('nav-golive')).click()",
}


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("focused", [40, 35], ids=["firefox", "a-popped-out-posterchan-window"])
@pytest.mark.parametrize("opener", sorted(GO_LIVE_OPENERS))
def test_go_live_opens_over_the_window_you_were_using(focused, opener):
    async def check(b):
        await _ready(b)
        await b.js(f"__wm.focus={focused};__wm.emit&&__wm.emit({{name:'window',change:'focus'}})")
        await asyncio.sleep(.5)
        await b.js("__wm.log.length=0")
        await b.js(GO_LIVE_OPENERS[opener])
        await b.until("!!document.querySelector('#gl-title')")
        await _until_front(b, str(focused))
        last = _last_front(await b.js("__wm.log"))
        assert last["front"] is True and focused in last["covers"], \
            f"the Go Live sheet was left behind window {focused}: {await b.js('__wm.log')}"

    asyncio.run(desktop.with_browser("online", "", check, COMPOSITOR + INGEST))
