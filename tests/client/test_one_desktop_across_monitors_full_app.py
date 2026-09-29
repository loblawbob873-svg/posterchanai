"""PosterChanOS monitors are ONE desktop: one arrival card, one chime, one read state.

Reported: "each monitor should be unified with events and notifications, looks like each monitor does
things separately". Every monitor is its own renderer running the whole client, so every arrival
popped a card and played the chime once PER MONITOR, and reading notifications on one monitor left the
bell lit on the others. The real bundled desktop; `pcShell.backgroundOwner === false` is what a
secondary monitor's preload says about itself, and a StorageEvent is exactly what another monitor's
write looks like from this page.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


CARDS = "document.querySelectorAll('.os-toast,.notif-toast').length"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("secondary", [False, True])
def test_only_the_primary_monitor_announces_an_arrival(secondary):
    async def check(b):
        await desktop.login(b)
        await b.until("!!window.PCOS && PCOS.isOn() && !!document.querySelector('#os-bell')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        await b.js(f"window.pcShell=Object.assign({{}},window.pcShell||{{}},{{backgroundOwner:{json.dumps(not secondary)}}})")
        before = await b.js(CARDS)
        await b.js("__PC.notifToast('<b>Alice</b> mentioned you','',null,'mention')")
        await asyncio.sleep(.3)
        after = await b.js(CARDS)
        if secondary:
            assert after == before, "a second monitor popped its own card for the same arrival"
        else:
            assert after == before + 1, "the primary monitor stopped announcing arrivals at all"

    asyncio.run(desktop.with_browser("online", "", check))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_reading_notifications_on_one_monitor_clears_the_bell_on_the_others():
    async def check(b):
        await desktop.login(b)
        await b.until("!!document.querySelector('#os-bell')")
        await b.js("""(()=>{
          const owner=String(window.__PC_API_BASE__||location.origin).replace(/\\/+$/,'')+':'+__PC.me().pubkey;
          const now=Math.floor(Date.now()/1000);
          localStorage.setItem('pc_reminder_history:'+owner,JSON.stringify([
            {type:'reminder',id:'reminder:one',created_at:now-60,content:'Dentist at three',route:'calendar',alerted:true}]));
          localStorage.setItem('pc_notif_seen','0');
        })()""")
        prev = await b.js("__documentIdentity")
        await b.call("Page.reload")
        await b.until("window.__documentIdentity!==" + json.dumps(prev) + " && !!window.__PC?.me() && !!document.querySelector('#os-bell')")
        await b.until("__PC.notifUnread()===1")
        # The OTHER monitor opens its notification centre: it writes the shared marker, and this page
        # hears it as a storage event.
        await b.js("""(()=>{const v=String(Math.floor(Date.now()/1000));
          window.dispatchEvent(new StorageEvent('storage',{key:'pc_notif_seen',oldValue:'0',newValue:v}));})()""")
        await asyncio.sleep(.5)
        assert await b.js("__PC.notifUnread()") == 0, "read on another monitor, still unread on this one"
        assert await b.js("document.querySelector('#os-bell .os-dot')===null"), "this monitor's bell stayed lit"

    asyncio.run(desktop.with_browser("online", "", check))
