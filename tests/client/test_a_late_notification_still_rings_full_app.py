"""A notification that ARRIVES after you opened Notifications rings the bell -- however old its timestamp.

"on desktop I seen new notifications without the bell changing": on both monitors the bell said nothing,
and clicking it showed new rows. Opening Notifications sets the read marker to NOW, and both the bell's
count and the live announcement were gated on `created_at > marker` -- so anything delivered AFTER you
looked but dated BEFORE it (a relay passing it on late, a sender's slow clock, a reaction to an old post)
counted as already read the moment it landed. The notification centre is its own freshly-started window
and lists everything recent, which is exactly where the "new stuff" was found.

The other half is what must NOT ring: the same old rows a relay sends again after a reconnect.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_notifs_module_boots_full_app import RELAY, NOTE, ME_PK, MENTIONS, BUILD_PROBE


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


STATE = ("({toast:[...document.querySelectorAll('#toast-root > *, .toast')].map(t=>t.innerText).join(' | '),"
         "unread:__PC.notifUnread()})")


async def _scenario(kind):
    out = {}

    async def check(b):
        await desktop.login(b)
        await b.until(MENTIONS + ".length>0")
        await asyncio.sleep(1.5)                                   # past EOSE: what arrives now is live
        # An old mention you have already seen: in the list when you opened Notifications.
        await b.js("(()=>{const me=" + ME_PK + ";const ev=" + NOTE + "(8,'AN OLD MENTION',3600,[['p',me]]);"
                   "for(const q of " + MENTIONS + ")q.sock.fire('message',['EVENT',q.sub,ev]);window.__old=ev;})()")
        await asyncio.sleep(0.6)
        await b.js("__PC.notifsRead(); document.querySelectorAll('#toast-root > *, .toast').forEach(t=>t.remove()); true")
        await asyncio.sleep(1.1)                                   # the read marker is whole seconds
        assert await b.js("__PC.notifUnread()") == 0
        if kind == "late":
            # Dated ten minutes before you looked, delivered now.
            await b.js("(()=>{const me=" + ME_PK + ";const ev=" + NOTE + "(9,'A LATE MENTION',600,[['p',me]]);"
                       "for(const q of " + MENTIONS + ")q.sock.fire('message',['EVENT',q.sub,ev]);})()")
        else:
            # A reconnect: the relay sends the already-seen mention again, on a fresh subscription.
            await b.js("for(const q of " + MENTIONS + ")q.sock.fire('message',['EVENT',q.sub,__old]); true")
        await asyncio.sleep(1.0)
        out.update(await b.js(STATE))

    await desktop.with_browser("online", "", check, extra_init=RELAY + BUILD_PROBE)
    return out


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_mention_delivered_late_rings_the_bell_and_pops():
    s = asyncio.run(_scenario("late"))
    assert s["unread"] >= 1, ("a mention that arrived after you looked was counted as already read", s)
    assert "mentioned you" in s["toast"], ("it never popped either", s)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_an_already_seen_mention_sent_again_stays_read():
    s = asyncio.run(_scenario("again"))
    assert s["unread"] == 0 and "mentioned you" not in s["toast"], ("a re-sent, already-seen mention rang again", s)
