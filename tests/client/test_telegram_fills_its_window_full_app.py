"""On PosterChanOS, Telegram in its own window uses the whole window and is titled "Telegram".

Reported: "telegram UI components don't fit properly in the OS window, waste of space". Measured in a
677x1100 window (the size the desktop opened it at on the reporter's monitor): the chat ended 80px
above the window's bottom edge — the feed's padding for the PHONE's bottom nav and FAB, neither of
which a window has — and a 62px page header repeated "Telegram" directly under the window's own title
bar, which itself read "tg": a window opened without a label titled itself with the view id.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_telegram_client_full_app import FAKE

WINDOW = "window.pcShell.windowContext={role:'app',view:'tg'};window.pcShell.backgroundOwner=false;"
FIT = r"""(()=>{const r=s=>{const e=document.querySelector(s);return e?e.getBoundingClientRect():null};
  const bar=r('#pc-oswin-chrome'), app=r('.tg-app'), top=r('.topbar'), comp=r('.tg-composer');
  return {vh:innerHeight, bar:bar&&Math.round(bar.bottom), appTop:app&&Math.round(app.top), appBottom:app&&Math.round(app.bottom),
          composerBottom:comp&&Math.round(comp.bottom), topbar:top?Math.round(top.height):0,
          title:(document.querySelector('.pc-oswin-title')||{}).textContent||''}})()"""


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_telegram_uses_the_whole_os_window():
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 677, "height": 1100, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("!!document.querySelector('.tg-composer')")
        await asyncio.sleep(.3)
        fit = await b.js(FIT)
        assert fit["title"] == "Telegram", ("the window is titled with its view id", fit)
        assert fit["topbar"] == 0, ("a page header repeats the window's title bar", fit)
        assert fit["appTop"] - fit["bar"] <= 2, ("space between the title bar and the app", fit)
        assert fit["vh"] - fit["appBottom"] <= 2 and fit["vh"] - fit["composerBottom"] <= 2, \
            ("the composer stops short of the window's bottom edge", fit)

    asyncio.run(desktop.with_browser("online", "?pcwin=tg", check,
                                     extra_init=FAKE.replace("state:'none'", "state:'ready'") + WINDOW))
