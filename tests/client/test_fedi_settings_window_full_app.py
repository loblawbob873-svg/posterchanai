"""Exercise bundled desktop Settings using real handlers and boundary-only fixtures."""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('relays_enabled', [False, True])
@pytest.mark.parametrize('change_relay_toggle', [False, True])
def test_crosspost_save_keeps_settings_window(relays_enabled, change_relay_toggle):
    async def check(b):
        await desktop.login(b)
        await b.until('PCOS.isOn()')
        await b.js("__PC.switchView('settings')")
        await b.until("!!document.querySelector('#us-fedi-crosspost')")
        await b.js("""
          document.querySelector('.us-tab[data-tab=social]').click();
          window.__settingsHost=document.querySelector('#user-settings');
          window.__settingsWindow=__settingsHost.closest('.osw');
          window.__windowCount=document.querySelectorAll('.osw').length;
          window.__routes=[]; window.__settingsWrites=[];
          const route=PCOS.routeView;
          PCOS.routeView=function(...args){__routes.push(args[0]);return route.apply(this,args)};
          const originalFetch=window.fetch;
          window.fetch=function(url,opts={}){
            if(String(url).includes('/api/auth/settings') && opts.method==='PUT')
              __settingsWrites.push(JSON.parse(opts.body));
            return originalFetch.apply(this,arguments);
          };
        """)
        assert await b.js('!!__settingsWindow'), 'fixture must exercise a real desktop window'
        await b.js("document.querySelector('#us-fedi-crosspost').click()")
        assert await b.js('__routes') == []
        if change_relay_toggle:
            await b.js("document.querySelector('#set-relays-on').click()")
        await b.js("document.querySelector('#us-save').click()")
        await b.until("(document.querySelector('#us-save-status')?.innerText||'').includes('Saved')")
        assert await b.js('__settingsWrites.length') == 1
        assert await b.js('__settingsWrites[0].fedi_crosspost_enabled') is True
        reload_scheduled = await b.js("document.querySelector('#us-save-status').innerText.includes('reloading')")
        if change_relay_toggle:
            assert reload_scheduled, 'an explicit relay on/off change still needs reconnect'
            return
        assert not reload_scheduled, 'unrelated cross-post save scheduled a desktop reload'
        # The real reload timer fires after 600 ms; keep observing beyond that point.
        await asyncio.sleep(1)
        assert await b.js("""document.querySelector('#user-settings')===__settingsHost &&
          __settingsHost.closest('.osw')===__settingsWindow &&
          document.querySelectorAll('.osw').length===__windowCount""")
        assert await b.js('__routes') == []

    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),relaysEnabled:" + json.dumps(relays_enabled) + ",relays:" + json.dumps(['wss://fixture.invalid'] if relays_enabled else []) + "}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))
