"""macOS mode was REMOVED (2026-10-06: "wait just scrap macos mode" / "remove macos mode from the code").

An account that had it switched on still carries `osDesktopStyle: mac` in its synced preferences, and
nothing deletes that value, so what this asserts is what such a person SEES: the ordinary PosterChan
desktop — no menu bar, no Dock, the tray in the taskbar — and nowhere to switch macOS mode back on.
Driven in the real bundled client.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_an_account_that_had_macos_mode_gets_the_posterchan_desktop():
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=1440, height=1000, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.until("!!window.PCOS && PCOS.isOn() && !!document.querySelector('#os-desk')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        # The stored preference of somebody who used macOS mode, then a fresh desktop.
        await b.js("ClientSettings.set('osDesktopStyle','mac'); PCOS.exit(); PCOS.enter()")
        await b.until("!!document.querySelector('#os-root #os-desk') && !!document.querySelector('#os-bar .os-tray, #os-root .os-tray')")
        await asyncio.sleep(.4)
        got = await b.js("""(()=>{const root=document.getElementById('os-root'), bar=document.getElementById('os-bar');
          const r=bar.getBoundingClientRect(), menu=document.getElementById('os-mac-menu');
          const tray=document.querySelector('#os-root .os-tray');
          return {mac:root.classList.contains('os-style-mac'),
                  menuShown:!!menu && menu.getBoundingClientRect().height>0,
                  barAtBottom:Math.abs(innerHeight-r.bottom)<=2 && Math.round(r.left)<=1 && Math.round(innerWidth-r.right)<=1,
                  trayInBar:!!tray && bar.contains(tray)}})()""")
        assert got == {"mac": False, "menuShown": False, "barAtBottom": True, "trayInBar": True}, \
            "a stored macOS preference still changes the desktop: %s" % got

        # Nowhere to turn it back on: the desktop's right-click menu…
        await b.js("""(()=>{const d=document.getElementById('os-desk').getBoundingClientRect();
          document.getElementById('os-desk').dispatchEvent(new MouseEvent('contextmenu',
            {bubbles:true,cancelable:true,clientX:d.left+40,clientY:d.bottom-60}))})()""")
        await b.until("document.querySelectorAll('.os-ctx-b').length>0")
        rows = await b.js("[...document.querySelectorAll('.os-ctx-b')].map(x=>x.textContent.trim())")
        assert any("background" in r.lower() for r in rows), rows   # the menu really is open
        assert not [r for r in rows if "macos" in r.lower()], rows

    asyncio.run(desktop.with_browser("online", "", check))
