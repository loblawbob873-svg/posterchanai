"""The profile header's buttons carry icons, and the QR button shows a QR code.

Reported: "The profile page have a button for profile management without any icon. The qr button
don't show any qr code, only copy the npub." Edit had no icon at all, Settings' compact form was a
text "⚙" in a slot styled for SVG icons, a desktop rule hid EVERY icon in the row (Call, Zap, Tip), and
the QR-glyph button was the Copy button. The real bundled client, phone and desktop widths.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


ICON = """(sel=>{const b=document.querySelector(sel); if(!b) return 'missing';
  const i=b.querySelector('svg.ic'); if(!i) return 'no icon';
  const r=i.getBoundingClientRect(); return (r.width>4&&r.height>4&&getComputedStyle(i).display!=='none')?'ok':'hidden';})"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [390, 1280])
def test_own_profile_buttons_have_icons_and_the_qr_shows_a_code(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.openProfile(__PC.me().pubkey)")
        await b.until("!!document.querySelector('#edit-prof') && !!document.querySelector('#prof-qr')")
        for sel in ("#edit-prof", "#open-settings", "#prof-pay"):
            assert await b.js(f"({ICON})('{sel}')") == "ok", f"{sel} shows no icon at {width}px"
        npub = await b.js("NostrTools.nip19.npubEncode(__PC.me().pubkey)")
        await b.js("document.querySelector('#prof-qr').click()")
        await b.until("!!document.querySelector('.prof-qr-code svg')")
        got = await b.js("""({n:document.querySelector('.prof-qr-code svg').querySelectorAll('rect,path').length,
            code:document.querySelector('.prof-qr-npub').textContent})""")
        assert got["n"] >= 1 and got["code"] == npub, got
        assert await b.js("typeof document.getElementById('copy-npub').onclick") == "function", "Copy npub lost its binding"

    asyncio.run(desktop.with_browser("online", "", check))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_someone_elses_profile_keeps_its_call_and_zap_icons_on_desktop():
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=1280, height=900, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.openProfile('" + "7" * 64 + "')")
        await b.until("!!document.querySelector('#zap-prof')")
        for sel in ("#call-prof", "#zap-prof"):
            assert await b.js(f"({ICON})('{sel}')") == "ok", f"{sel}'s icon is hidden on desktop"

    asyncio.run(desktop.with_browser("online", "", check))
