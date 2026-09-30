"""The bell lights for a new notification even while a Notifications view is open somewhere unwatched.

Reported: "notifications bell … is not illuminating anymore" — "on laptop the bell illuminated, not on
desktop anymore". Rendering the Notifications view marks everything read, and it re-renders on EVERY
arrival. On PosterChanOS a Notifications view is often open where nobody is looking — its own window
in the background, or a monitor's desktop surface whose last view it was — so each new notification
was marked read the moment it landed. That used to clear only that page's copy of the marker; since
read state is shared between monitors and windows (`pc_notif_seen`, so reading on one clears all),
it cleared the bell everywhere. A two-monitor desktop has such a surface; a laptop usually does not.

Now a RE-render marks read only while the view is being looked at (visible and focused); opening the
view still marks read, and so does coming back to it.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_notifs_module_boots_full_app import RELAY, NOTE, ME_PK, MENTIONS, BUILD_PROBE


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


BADGE = "[...document.querySelectorAll('#notif-badge,#notif-badge-m')].some(e=>!e.classList.contains('hidden')&&+e.textContent>=1)"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_an_unwatched_notifications_view_does_not_swallow_new_arrivals():
    async def check(b):
        await b.js("(()=>{const me=" + ME_PK + ";const r=_rel();const mine=" + NOTE + "(1,'MY OWN POST',900);r.push(mine);"
                   "localStorage.setItem('__relayEvents',JSON.stringify(r));window.__mine=mine;})()")
        await desktop.login(b)
        await b.until(MENTIONS + ".length>0")
        await b.js("__PC.switchView('notifications')")          # opened: everything so far is read
        await asyncio.sleep(.5)
        seen0 = await b.js("+localStorage.getItem('pc_notif_seen')||0")
        assert seen0 > 0
        # Now nobody is looking at this page (a background window / another monitor's surface).
        await b.js("document.hasFocus=()=>false")
        await asyncio.sleep(1.1)                                  # so a new mark would be a later second
        await b.js("(()=>{const me=" + ME_PK + ";const reply=" + NOTE + "(7,'A LIVE REPLY TO YOU',0,[['e',__mine.id,'','root'],['p',me]]);"
                   "const r=_rel();r.push(reply);localStorage.setItem('__relayEvents',JSON.stringify(r));"
                   "for(const q of " + MENTIONS + ")q.sock.fire('message',['EVENT',q.sub,reply]);})()")
        await b.until("[...document.querySelectorAll('.notif')].some(n=>n.innerText.includes('A LIVE REPLY TO YOU'))")
        await asyncio.sleep(.6)
        state = await b.js("({seen:+localStorage.getItem('pc_notif_seen')||0, unread:__PC.notifUnread(), badge:" + BADGE + "})")
        assert state["seen"] == seen0, ("a view nobody was looking at marked the new notification read", state)
        assert state["unread"] >= 1 and state["badge"], ("the bell did not light", state)
        # Looking at it again reads it.
        await b.js("document.hasFocus=()=>true; window.dispatchEvent(new Event('focus'))")
        await asyncio.sleep(.3)
        after = await b.js("({seen:+localStorage.getItem('pc_notif_seen')||0, unread:__PC.notifUnread()})")
        assert after["seen"] > seen0 and after["unread"] == 0, after

    asyncio.run(desktop.with_browser("online", "", check, extra_init=RELAY + BUILD_PROBE))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_desktop_surface_holding_focus_does_not_swallow_new_arrivals():
    """"not seeing notification bell illuminated on desktop but i hear notifications". A PosterChanOS
    monitor surface is always visible and holds focus after a click on the taskbar or desktop; its last
    view can be Notifications, rendered into its feed and shown nowhere. Measured on the reporting
    desktop: ownsFeedView('notifications') false and the read mark moved 86 s earlier. Focused and
    visible, but NOT on screen: a new arrival must light the bell, not be read by nobody."""
    async def check(b):
        await b.js("(()=>{const me=" + ME_PK + ";const r=_rel();const mine=" + NOTE + "(1,'MY OWN POST',900);r.push(mine);"
                   "localStorage.setItem('__relayEvents',JSON.stringify(r));window.__mine=mine;})()")
        await desktop.login(b)
        await b.until(MENTIONS + ".length>0")
        await b.js("__PC.switchView('notifications')")
        await asyncio.sleep(.5)
        seen0 = await b.js("+localStorage.getItem('pc_notif_seen')||0")
        # The desktop surface: focused and visible, but the Notifications view is not on screen.
        await b.js("document.hasFocus=()=>true; window.PCOS=Object.assign(window.PCOS||{}, {isOn:()=>true, ownsFeedView:v=>false})")
        await asyncio.sleep(1.1)
        await b.js("(()=>{const me=" + ME_PK + ";const reply=" + NOTE + "(8,'ANOTHER LIVE REPLY',0,[['e',__mine.id,'','root'],['p',me]]);"
                   "const r=_rel();r.push(reply);localStorage.setItem('__relayEvents',JSON.stringify(r));"
                   "for(const q of " + MENTIONS + ")q.sock.fire('message',['EVENT',q.sub,reply]);})()")
        await b.until("[...document.querySelectorAll('.notif')].some(n=>n.innerText.includes('ANOTHER LIVE REPLY'))")
        await asyncio.sleep(.6)
        state = await b.js("({seen:+localStorage.getItem('pc_notif_seen')||0, unread:__PC.notifUnread(), badge:" + BADGE + "})")
        assert state["seen"] == seen0, ("the desktop surface marked a notification read that nobody could see", state)
        assert state["unread"] >= 1 and state["badge"], ("the bell did not light", state)
        # When the view IS on screen, focused, reading still happens.
        await b.js("PCOS.ownsFeedView=v=>v==='notifications'; window.dispatchEvent(new Event('focus'))")
        await asyncio.sleep(.3)
        after = await b.js("({seen:+localStorage.getItem('pc_notif_seen')||0, unread:__PC.notifUnread()})")
        assert after["seen"] > seen0 and after["unread"] == 0, after

    asyncio.run(desktop.with_browser("online", "", check, extra_init=RELAY + BUILD_PROBE))
