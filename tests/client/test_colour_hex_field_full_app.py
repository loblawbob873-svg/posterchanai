"""Every colour picker in Appearance has a #rrggbb box beside it ("could you replace the color picker with one that
allows you to input #colorcode?", Vyram, git issue "Concord Theme match with app theme", 2026-10-25).

Android's picker is a wheel; matching an exact red by finger is next to impossible. The real client at phone width:
in Settings -> Appearance a typed code is shown live while typing and SAVED on Enter, `#f00` means `#ff0000`, and a
code that is not one is marked and never applied. The same box drives Concord's own theme dialog.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

NO_OS = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')"
         "||'{}'),osMode:false}));")
NEON = "getComputedStyle(document.documentElement).getPropertyValue('--neon').trim()"
ACCENT_HEX = ("[...document.querySelectorAll('#us-look .look-row')][0].querySelector("
              "\"input[type=color][data-look='accent']\").closest('.look-col').querySelector('.look-hex')")


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _type(b, el, text, enter=False):
    await b.js("(()=>{const t=%s;t.focus();t.value=%r;t.dispatchEvent(new Event('input',{bubbles:true}));%s})()"
               % (el, text, "t.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));" if enter else ""))
    await asyncio.sleep(.2)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_typed_hex_code_sets_the_colour_live_and_saves_on_enter():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 900, 'deviceScaleFactor': 1, 'mobile': True})
        await desktop.login(b)
        await b.js("__PC.switchView('settings')")
        await b.until("!!document.querySelector('.us-tab[data-tab=\"appearance\"]') && !!document.querySelector('#us-look .look')")
        await b.js("document.querySelector('.us-tab[data-tab=\"appearance\"]').click()")
        got["boxes"] = await b.js("document.querySelectorAll('#us-look .look-hex').length===document.querySelectorAll('#us-look input[type=color]').length"
                                  "&&document.querySelectorAll('#us-look .look-hex').length>0")
        # A new box is not an edit: Settings refuses to redraw over a field whose value differs from its default.
        got["not_edits"] = await b.js("[...document.querySelectorAll('#us-look .look-hex')].every(t=>t.value===t.defaultValue&&t.value)")
        got["fits"] = await b.js("[...document.querySelectorAll('#us-look .look-hex')].every(e=>{const r=e.getBoundingClientRect();return r.width>40&&r.right<=innerWidth+1})")
        await _type(b, ACCENT_HEX, "#d10000")
        got["live"] = await b.js(NEON)
        await _type(b, ACCENT_HEX, "#d10000", enter=True)
        got["saved"] = await b.js("PCLook.get().accent||''")
        await _type(b, ACCENT_HEX, "#f00", enter=True)
        got["short"] = await b.js("PCLook.get().accent||''")
        await _type(b, ACCENT_HEX, "#zz12")
        got["bad"] = await b.js("%s.classList.contains('bad')" % ACCENT_HEX)
        await b.js("%s.dispatchEvent(new Event('change',{bubbles:true}))" % ACCENT_HEX)
        await asyncio.sleep(.2)
        got["after_bad"] = await b.js("PCLook.get().accent||''")
        # Concord's own theme dialog: same box, same rule.
        await b.js("__PC.switchView('concord')")
        await asyncio.sleep(.5)
        await b.js("PCConcordTheme.openPicker(__PC)")
        await b.until("!!document.querySelector('.cc-theme-custom .look-hex')")
        cc = "document.querySelector('#cc-theme-accent')._pcHex"
        await _type(b, cc, "#00aa55", enter=True)
        got["concord_accent"] = await b.js("document.querySelector('#cc-theme-accent').value")
        got["concord_saved"] = await b.js("(PCConcordTheme.get&&PCConcordTheme.get().accent)||''")

    asyncio.run(desktop.with_browser("online", "", check, NO_OS))
    assert got["boxes"], ("not every colour picker has a #rrggbb box", got)
    assert got["not_edits"], ("a fresh #rrggbb box reads as an unsaved edit -- Settings will not redraw", got)
    assert got["fits"], ("a #rrggbb box is missing or off the screen", got)
    assert got["live"] == "#d10000", ("typing a code does not show it", got)
    assert got["saved"] == "#d10000", ("Enter did not save the typed colour", got)
    assert got["short"] == "#ff0000", ("#f00 is not #ff0000", got)
    assert got["bad"] and got["after_bad"] == "#ff0000", ("an invalid code was applied", got)
    assert got["concord_accent"] == "#00aa55", ("Concord's dialog has no working #rrggbb box", got)
