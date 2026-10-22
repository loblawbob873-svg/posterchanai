"""The vault's "This device" panel (Android autofill), pressed in the SHIPPED bundle with the phone's autofill
plugin stubbed the way the APK injects it:

  * it shows what the autofill service saw last time, and "Copy report" copies that report as JSON;
  * with autofill not turned on, "Turn on autofill" asks Android to open its autofill settings;
  * Close closes.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_vault_generate_export_full_app import FIXTURE


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


PLUGIN = r'''(()=>{window.__enable=0;
  const last={pkg:'com.example.bank',outcome:'filled',at:Date.now(),fieldCount:2,pickUser:0,pickPass:1,
              fields:[{text:'email',hints:'username'},{text:'password',realPw:true}]};
  window.Capacitor=Object.assign(window.Capacitor||{},{Plugins:Object.assign((window.Capacitor||{}).Plugins||{},{VaultAutofill:{
    status:async()=>({supported:true,enabled:false}),
    lastFill:async()=>({json:JSON.stringify(last)}),
    requestEnable:async()=>{window.__enable++;return {};}}})});
  return 1;})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_this_device_panel_copies_the_report_and_turns_on_autofill():
    async def check(b):
        await desktop.login(b)
        await b.js("window.__publishOK=true; __PC.switchView('vault'); 1")
        await b.until("!!document.querySelector('.pv-device')")
        await b.js(PLUGIN)
        await b.js("document.querySelector('.pv-device').click(); 1")
        await b.until("!!document.querySelector('#pd-copyfill') && !!document.querySelector('#pd-enable')")
        text = await b.js("document.querySelector('#pd-close').closest('.modal, .modal-card, [role=dialog], body').textContent")
        assert "com.example.bank" in text and "PASSWORD" in text, text[:400]
        await b.js("document.querySelector('#pd-copyfill').click(); 1")
        await b.until("__clip.some(c=>c.includes('com.example.bank'))")
        report = json.loads(await b.js("__clip.find(c=>c.includes('com.example.bank'))"))
        assert report["pickPass"] == 1 and report["fields"][1]["realPw"] is True, report
        await b.js("document.querySelector('#pd-enable').click(); 1")
        await b.until("__enable===1 && !document.querySelector('#pd-close')")
        # Opened again, Close closes it.
        await b.js("document.querySelector('.pv-device').click(); 1")
        await b.until("!!document.querySelector('#pd-close')")
        await b.js("document.querySelector('#pd-close').click(); 1")
        await b.until("!document.querySelector('#pd-close')")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
