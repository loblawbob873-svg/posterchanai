"""A ResizeObserver-loop notice is not an error, so it never becomes a "something went wrong" toast.

Reported on a phone: "ResizeObserver happens on mobile right after you grant local storage, not happening on
desktop". Granting persistent storage (Firefox asks; Chrome does not) closes a prompt bar, the viewport changes
height, and the timeline's cards -- lazily laid out on a phone (content-visibility) -- settle over a couple of
frames. The browser reports that as an ErrorEvent, "ResizeObserver loop completed with undelivered
notifications", and the client's last-resort error surface turned it into a "⚠ something went wrong" toast.
Nothing went wrong: by the spec the notifications are delivered on the next frame. The notice is still logged;
a real error is still shown.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_resize_observer_notice_shows_nothing_and_a_real_error_still_does():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("document.querySelectorAll('#toast-root .toast').forEach(t=>t.remove());true")
        for msg in ("ResizeObserver loop completed with undelivered notifications.",
                    "ResizeObserver loop limit exceeded"):
            await b.js("window.dispatchEvent(new ErrorEvent('error',{message:%r}));true" % msg)
        await asyncio.sleep(.3)
        got["after_notice"] = await b.js("[...document.querySelectorAll('#toast-root .toast')].map(t=>t.textContent)")
        await asyncio.sleep(1.7)   # past the reporter's 1.5s rate limit, so a real error is not swallowed by it
        await b.js("window.dispatchEvent(new ErrorEvent('error',{message:'boom from a handler',error:new Error('boom from a handler')}));true")
        await asyncio.sleep(.3)
        got["after_error"] = await b.js("[...document.querySelectorAll('#toast-root .toast')].map(t=>t.textContent)")

    asyncio.run(desktop.with_browser("online", "", check))
    assert not any("ResizeObserver" in t for t in got["after_notice"]), ("the notice became an error toast", got)
    assert any("boom from a handler" in t for t in got["after_error"]), ("a real error no longer reaches the screen", got)
