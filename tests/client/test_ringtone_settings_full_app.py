"""User Settings → Ringtone, in the shipped client: play it, ring calls with it, and on Android one tap sets it.

"maybe User Settings -> Ring Tone, then it can set it there for Launcher". Driven in the real bundle:
  * the tab exists and plays the bundled ringtone (the file is served from the bundle, not the network);
  * the "Ring PosterChan calls" switch is saved;
  * on the Android app, "Set as phone ringtone" hands the plugin the real sound and SAYS what happens next
    when Android wants "Modify system settings" first -- then, tapped again, that it is set;
  * with no Android plugin (web, desktop) the files are offered to download instead.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


PLUGIN = r"""window.Capacitor=window.Capacitor||{}; Capacitor.Plugins=Capacitor.Plugins||{};
window.__installs=[]; Capacitor.Plugins.Ringtone={ available:async()=>({ok:true,canWrite:false}),
  install:async(o)=>{ __installs.push({len:(o.data||'').length,name:o.name,mime:o.mime,setDefault:o.setDefault});
    return {ok:true, outcome: __installs.length===1 ? 'needs-permission' : 'set', where:'Ringtones/posterchan-cyberpunk.ogg'}; } };"""


async def _open_tab(b):
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js("__PC.switchView('settings');true")
    await b.until("!!document.querySelector('.us-tab[data-tab=\"ringtone\"]')")
    await b.js("document.querySelector('.us-tab[data-tab=\"ringtone\"]').click();true")
    await b.until("!!document.querySelector('[data-pane=\"ringtone\"].active #rt-play')")


CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")


@CHROME
def test_android_sets_the_phone_ringtone_and_says_what_android_needs():
    got = {}

    async def check(b):
        await _open_tab(b)
        got["sound"] = await b.js("fetch('/static/sounds/posterchan-cyberpunk.ogg').then(r=>r.ok?r.blob():null).then(x=>x?x.size:0)")
        await b.js("document.getElementById('rt-calls').click();true")
        got["calls"] = await b.js("ClientSettings.get('callRingtone', true)")
        await b.js("document.getElementById('rt-set').click();true")
        await b.until("__installs.length===1 && /Modify system settings/.test(document.getElementById('rt-said').textContent)")
        got["first"] = await b.js("document.getElementById('rt-said').textContent")
        await b.js("document.getElementById('rt-set').click();true")
        await b.until("__installs.length===2 && /ringtone now/.test(document.getElementById('rt-said').textContent)")
        got["installs"] = await b.js("__installs")
        got["download"] = await b.js("!!document.querySelector('[data-pane=\"ringtone\"] a[download]')")

    asyncio.run(desktop.with_browser("online", "", check, PLUGIN))
    assert got["sound"] > 100_000, got
    assert got["calls"] is False, "the calls switch was not saved"
    i = got["installs"][0]
    assert i["name"] == "posterchan-cyberpunk" and i["mime"] == "audio/ogg" and i["setDefault"] is True, i
    assert i["len"] > 100_000, ("the plugin was not handed the real sound", i)
    assert got["download"] is False, "the Android app offers downloads instead of the one-tap set"


@CHROME
def test_without_the_android_plugin_the_files_are_offered_to_download():
    got = {}

    async def check(b):
        await _open_tab(b)
        got["links"] = await b.js("[...document.querySelectorAll('#rt-dl a[download]')].map(a=>a.getAttribute('download'))")
        got["set"] = await b.js("!!document.getElementById('rt-set')")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["links"] == ["posterchan-cyberpunk.ogg", "posterchan-cyberpunk.m4r", "posterchan-cyberpunk.mp3"], got
    assert got["set"] is False
