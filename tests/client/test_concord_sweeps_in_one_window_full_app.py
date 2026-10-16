"""On PosterChanOS only the Communities window (and the desktop page) keeps Concord's background subscriptions.

"0 of 5 of this community's relays answered" / no community icons or banners on the laptop and in Firefox
(2026-10-16). Every PosterChanOS app window is its own copy of the client and each ran Concord's 20-second sweep -- a
live subscription to every relay of every community, through the account's tunnel share. Measured over one minute on
the desktop: Notes, Mail, Texts, Terminal, Telegram, Messages and a post window each opened 4-10 tunnels; together
they held all 128 of the account's tunnels and every other device was refused.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_scroll_never_shows_the_top_full_app import SETUP


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("window,sweeps", [("notes", False), ("concord", True), (None, True)])
def test_only_the_communities_window_sweeps(window, sweeps):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=1300, height=900, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        # a joined community with a channel stream -- set up WITHOUT ever opening Communities in this page, as in a
        # Notes window (opening it starts its own live timer, which is that window's to run)
        await b.js("__PC.switchView('notes')")
        await b.js(SETUP.replace("__PC.switchView('concord');", "").replace(
            "channels:[{id:'fixture-general',name:'general'}]",
            "channels:[{id:'fixture-general',name:'general',streamPubkeys:['6'.repeat(64)]}]"))
        # the community's control data is loaded, as on any device that has opened it before
        await b.js("PCConcord.__testSetControls('c'.repeat(64),[{kind:1059,id:'a'.repeat(64),pubkey:'7'.repeat(64),created_at:1,tags:[],content:'',sig:'0'.repeat(128)}])")
        if window:
            await b.js("window.PCOSWin={isWindow:()=>true,viewOf:()=>%r}" % window)
        await b.js(r"""(()=>{window.__subs=0;const R=window.PCConcordRelay||window.Relay;
          for(const k of ['subscribeFrom','queryFrom']){const o=R[k]&&R[k].bind(R);if(o)R[k]=(...a)=>{window.__subs++;return o(...a);};}
          for(const k of ['relayQueryFrom','relaySubscribeFrom']){const o=__PC[k];if(o)__PC[k]=(...a)=>{window.__subs++;return o(...a);};}})()""")
        await asyncio.sleep(24)                   # one 20 s sweep
        got["subs"] = await b.js("window.__subs")

    asyncio.run(desktop.with_browser("online", "", check, ""))
    if sweeps:
        assert got["subs"] > 0, ("the page that owns the badge stopped sweeping", got)
    else:
        assert got["subs"] == 0, ("a non-Communities desktop window opened Concord subscriptions", got)
