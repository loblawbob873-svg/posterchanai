"""Opening Notifications clears the bell even for a notification dated AHEAD of this device's clock.

Reported 2026-10-02: "i have no idea what my notifications are, bell says 3 after clicking on
notifications". Read used to mean `created_at <= the moment the centre was opened`, so a mention signed by
a device whose clock runs fast stayed unread after it was opened -- the badge hid for an instant and the
next refresh lit it again, for as long as the skew lasted -- and its row said "-3598s". Measured with the
shipped client: unread 2 before opening, 1 after.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_notifs_module_boots_full_app import RELAY, NOTE, ME_PK, MENTIONS, BUILD_PROBE


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_mention_from_a_fast_clock_is_read_once_it_has_been_opened():
    async def check(b):
        await b.js("(()=>{const me=" + ME_PK + ";const r=_rel();"
                   "r.push(" + NOTE + "(7,'FROM A FAST CLOCK',-3600,[['p',me]]));"
                   "r.push(" + NOTE + "(8,'AN ORDINARY MENTION',60,[['p',me]]));"
                   "localStorage.setItem('__relayEvents',JSON.stringify(r));})()")
        await desktop.login(b)
        await b.until(MENTIONS + ".length>0")
        await b.until("__PC.notifUnread()>=2")
        await b.js("__PC.switchView('notifications')")
        await b.until("[...document.querySelectorAll('.notif')].some(n=>n.innerText.includes('FROM A FAST CLOCK'))")
        await asyncio.sleep(.6)
        state = await b.js("({unread:__PC.notifUnread(), when:[...document.querySelectorAll('.notif')]"
                           ".filter(n=>n.innerText.includes('FROM A FAST CLOCK')).map(n=>n.innerText)})")
        assert state["unread"] == 0, ("opened, and the bell still counts it", state)
        assert not any("-" in t.split("\n")[-1] for t in state["when"]), ("a negative age", state)
        # The bell is repainted on every arrival; that must not bring it back.
        await b.js("__PC.switchView('global')")
        await asyncio.sleep(.3)
        await b.js("(()=>{const me=" + ME_PK + ";const late=" + NOTE + "(9,'A NEW ONE',0,[['p',me]]);"
                   "for(const q of " + MENTIONS + ")q.sock.fire('message',['EVENT',q.sub,late]);})()")
        await b.until("__PC.notifUnread()>=1")
        await asyncio.sleep(.4)
        n = await b.js("__PC.notifUnread()")
        assert n == 1, ("only the new arrival is unread; the fast-clock one came back", n)

    asyncio.run(desktop.with_browser("online", "", check, extra_init=RELAY + BUILD_PROBE))
