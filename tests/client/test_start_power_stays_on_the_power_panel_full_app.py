"""Start -> Power opens the tray window ON the Power panel, and it STAYS there.

"clicking power button on start menu brings up the wifi widget" / "it shows power widget then the widget panel
itself" (2026-10-16): the tray window boots the whole client, app.js calls os.js restore() up to three times while
it does, and each call redrew the tray -- the first opened Power (consuming the one-shot request), the next drew the
plain Quick Settings flyout over it. Runs the real bundled client in a real browser as the tray popup.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_native_power_confirm_browser import POWER as _POWER

# The machine's bridges, as the desktop has them: power (a recorder, never systemctl) and NetworkManager.
POWER = _POWER + r'''
window.pcNet={status:async()=>({wifiOn:true,connected:{ssid:'home',signal:70}}),wifi:async()=>[{ssid:'home',signal:70,secure:true,inUse:true}],
 setWifi:async()=>({ok:true}),connect:async()=>({ok:true}),disconnect:async()=>({ok:true})};
'''


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


ASKED_FOR_POWER = POWER + r'''
try{ localStorage.setItem('pc_tray_open_panel', JSON.stringify({kind:'power', at:Date.now()})); }catch(_){ }
'''


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("mode", ["online", "hang"])
def test_start_power_opens_power_and_nothing_draws_over_it(mode):
    got = {}

    async def check(b):
        await b.until("!!document.querySelector('.os-pop [data-act=reboot]')")
        seen = []
        for _ in range(50):                                     # the rest of the boot: every restore(), the readings
            k = await b.js("document.querySelector('[data-act=reboot]')?'power':"
                           "(document.querySelector('.os-qs-tile[data-os=power]')?'quick':'none')")
            if not seen or seen[-1] != k:
                seen.append(k)
            await asyncio.sleep(0.1)
        got["panels"] = seen
        got["now"] = await b.js("(()=>{const p=document.querySelector('.os-pop');return p?{power:!!p.querySelector('[data-act=reboot]'),"
                                "back:!!p.querySelector('[data-os=quickback]'),tiles:!!p.querySelector('[data-os=net]')}:null})()")

    asyncio.run(desktop.with_browser(mode, "?pcpopup=tray", check, ASKED_FOR_POWER))
    assert got["now"] and got["now"]["power"] and got["now"]["back"], ("Power was replaced by the flyout", got)
    assert "power" in got["panels"], got
    after = got["panels"][got["panels"].index("power"):]
    assert "quick" not in after, ("the flyout was drawn over the Power panel", got["panels"])


# Tiles that change a setting in place rather than opening a panel of their own.
_TOGGLES = {"awake", "mute", "quickback"}


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_every_quick_settings_tile_opens_its_own_panel_and_keeps_it():
    """Every panel tile in Quick Settings, discovered from the rendered flyout (a tile added later is tested
    for free): pressing it opens a sub-panel with a way back, the panel is still there once the boot settles,
    no two tiles open the same panel, and Back returns to the flyout."""
    got = {}

    async def check(b):
        await b.until("!!document.querySelector('.os-pop [data-os=power]')")
        await asyncio.sleep(4)                                  # boot settles first: nothing may redraw later
        kinds = await b.js("[...document.querySelectorAll('.os-pop button.os-qs-tile[data-os]')].map(t=>t.dataset.os)")
        got["kinds"] = kinds
        for k in kinds:
            if k in _TOGGLES:
                continue
            await b.js("document.querySelector('.os-pop [data-os=\"%s\"]').click()" % k)
            await b.until("!!document.querySelector('.os-pop [data-os=quickback]')")
            head = await b.js("(document.querySelector('.os-pop .os-pop-h')||{}).textContent.trim()")
            await asyncio.sleep(1.5)
            still = await b.js("!!document.querySelector('.os-pop [data-os=quickback]') && "
                               "(document.querySelector('.os-pop .os-pop-h')||{}).textContent.trim()===%s" % repr(head))
            got[k] = {"head": head, "still": still}
            await b.js("document.querySelector('.os-pop [data-os=quickback]').click()")
            await b.until("!!document.querySelector('.os-pop [data-os=power]') && !document.querySelector('.os-pop [data-os=quickback]')")

    asyncio.run(desktop.with_browser("online", "?pcpopup=tray", check, POWER))
    panels = {k: v for k, v in got.items() if k != "kinds"}
    assert "power" in panels and len(panels) >= 2, ("too few panel tiles rendered to test", got)
    for k, v in panels.items():
        assert v["head"], (k, "opened a panel with no heading", v)
        assert v["still"], (k, "its panel was replaced while open", v)
    heads = [v["head"] for v in panels.values()]
    assert len(set(heads)) == len(heads), ("two tiles open the same panel", panels)
