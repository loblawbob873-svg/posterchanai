"""Who dances on the desktop (PosterChan or the axolotl) follows the ACCOUNT, not the device ("need to
make sure that the users pref is saved for axolotl or posterchan"). Two browser profiles signed in with the
same key: choosing the axolotl on the first publishes it in the account's `pcai:client-prefs` document;
the second -- a fresh profile that never touched the setting -- comes up with the axolotl dancing and
Settings showing it, and choosing PosterChan there publishes that.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client.test_right_panel_syncs_to_the_account_full_app import _prefs_of, device

PUBLISHED_PREFS = "window.__published.some(e=>(e.tags||[]).some(t=>t[0]==='d'&&t[1]==='pcai:client-prefs'))"
DANCER = "(()=>{const i=document.querySelector('#os-desk .os-buddy img');return i&&i.complete&&i.naturalWidth>0?(i.getAttribute('src')||''):''})()"


async def _desktop(b):
    await b.js("PCOS.enter();true")
    await b.until("document.body.classList.contains('os-on') && !!document.querySelector('#os-desk .os-buddy img')")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_choosing_the_axolotl_on_one_device_shows_her_on_another():
    async def on_a(b):
        await _desktop(b)
        start = await b.js(DANCER)
        await b.js("window.__published.length=0;PCBuddy.choose('axolotl');true")
        await b.until(PUBLISHED_PREFS)
        return {"start": start, "published": await b.js("window.__published")}
    a = asyncio.run(device([], on_a))
    assert "/mascot/dance/" in a["start"], ("PosterChan is not the default", a["start"])
    prefs = _prefs_of(a["published"])
    assert prefs and (prefs.get("desktopBuddy") or {}).get("who") == "axolotl", ("the choice was not saved to the account", prefs)
    seed = [e for e in a["published"] if ["d", "pcai:client-prefs"] in [t[:2] for t in e.get("tags", [])]][-1:]

    async def on_b(b):
        await _desktop(b)
        await b.until(f"/\\/mascot\\/axolotl\\//.test({DANCER})")
        out = {"dancer": await b.js(DANCER)}
        await b.js("__PC.switchView('settings')")
        await b.until("!!document.querySelector('.us-tab[data-tab=\"timeline\"]')")
        await b.js("document.querySelector('.us-tab[data-tab=\"timeline\"]').click()")
        await b.until("!!document.getElementById('set-desktop-buddy-who')")
        out["select"] = await b.js("document.getElementById('set-desktop-buddy-who').value")
        await b.js("window.__published.length=0;(()=>{const s=document.getElementById('set-desktop-buddy-who');s.value='posterchan';s.dispatchEvent(new Event('change'));})()")
        await b.until(PUBLISHED_PREFS)
        out["published"] = await b.js("window.__published")
        return out
    res = asyncio.run(device(seed, on_b))
    assert "/mascot/axolotl/" in res["dancer"], ("a second device ignored the account's axolotl", res["dancer"])
    assert res["select"] == "axolotl", ("Settings on the second device does not show the axolotl", res["select"])
    assert ((_prefs_of(res["published"]) or {}).get("desktopBuddy") or {}).get("who") == "posterchan", \
        "choosing PosterChan in Settings was not saved to the account"
