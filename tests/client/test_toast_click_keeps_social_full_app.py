"""Clicking a desktop toast opens Notifications in ITS OWN window -- Social stays Social.

Reported on PosterChanOS: "Clicking on Toaster notification with the global window open turns that
window into Notifications with no way to go back to Social. Better to open notifications in a new
window." A toast's default action was `switchView('notifications')`, which on the desktop paints into
whichever window holds the feed -- the Social window the person was reading.

Runs the real bundled client in desktop mode: open Social, raise a toast the way a live notification
does (notifToast → PCOS.osToast), click it, and look at the windows.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

WINS = r"""PCOS.windows().filter(w=>!w.min).map(w=>({view:w.view, app:w.appView, title:w.title}))"""


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_toast_opens_notifications_in_a_new_window_and_social_is_untouched():
    async def check(b):
        await desktop.login(b)
        await b.until("!!window.PCOS && PCOS.isOn() && !!document.querySelector('#os-desk')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        await b.js("__PC.switchView('global')")
        await b.until("PCOS.windows().some(w=>/global|home/.test(w.appView||''))")
        before = await b.js(WINS)
        social = next(w for w in before if w["app"] in ("global", "home"))
        await b.js("__PC.notifToast('🔔 <b>alice</b> mentioned you', '', null, 'mentions')")
        await b.until("!!document.querySelector('.os-toast')")
        await b.js("document.querySelector('.os-toast').click()")
        await asyncio.sleep(1)
        after = await b.js(WINS)
        views = [w["app"] for w in after]
        assert social["app"] in views, f"the Social window was repainted into something else: {before} -> {after}"
        assert "notifications" in views, f"Notifications did not open in a window of its own: {after}"
        # …and a second toast reuses that window rather than stacking another.
        await b.js("__PC.notifToast('🔔 <b>bob</b> mentioned you', '', null, 'mentions')")
        await b.until("!!document.querySelector('.os-toast')")
        await b.js("document.querySelector('.os-toast').click()")
        await asyncio.sleep(.8)
        again = [w["app"] for w in await b.js(WINS)]
        assert again.count("notifications") == 1 and social["app"] in again, again

    asyncio.run(desktop.with_browser("online", "", check))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_in_a_popped_out_social_window_the_click_goes_to_the_desktop():
    """The reported case: Social is its own window (its own page). A toast raised THERE, or a native
    notification whose click comes back to it, must not repaint it -- the desktop that owns the
    window opens the destination instead."""
    async def check(b):
        await desktop.login(b)
        await asyncio.sleep(1)
        assert await b.js("!!(window.PCOSWin && PCOSWin.isWindow())"), "the fixture is not a popped-out window"
        # The desktop this window belongs to (window.opener in production).
        await b.js("""window.__desk=[]; PCOSWin.desktop=()=>({ __PC:{ openNotificationRoute:r=>{ __desk.push(r); return true; } } });""")
        view0 = await b.js("__PC.VIEW")
        await b.js("__PC.notifToast('🔔 <b>alice</b> mentioned you', '', null, 'mentions')")
        await b.until("!!document.querySelector('.notif-toast,.os-toast')")
        await b.js("document.querySelector('.notif-toast,.os-toast').click()")
        await asyncio.sleep(.5)
        assert await b.js("__desk") == ["notifications"], "the toast did not hand its destination to the desktop"
        assert await b.js("__PC.VIEW") == view0 != "notifications", "the Social window was repainted"
        # The native notification's click lands in the page that raised it -- same rule.
        await b.js("__PC.openNotificationRoute('post:' + 'a'.repeat(64))")
        assert (await b.js("__desk"))[-1] == "post:" + "a" * 64
        assert await b.js("__PC.VIEW") == view0

    asyncio.run(desktop.with_browser("online", "?pcwin=global", check))
