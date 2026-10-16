"""User Settings -> API keys -> "Sign out all other devices" ends every other session and keeps this one.

Security review #11: a session token could not be revoked. The real bundled Settings renderer; the server's
answer is a fixture (the server half is tests/test_session_revocation.py). The button must ask first, call
the endpoint, swap this device's bearer for the fresh token it returns, and say what happened.
"""
import asyncio
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_settings_loading_full_app import bundle  # noqa: F401  (patches renderUserSettings' session gate)

FETCH = r"""window.__revokes=0;{const inner=window.fetch;window.fetch=function(u,o){
  if(String(u).includes('/api/auth/sessions/revoke')){__revokes++;
    return Promise.resolve(new Response(JSON.stringify({ok:true,access_token:'FRESH-TOKEN',token_type:'bearer',valid_after:1}),
      {status:200,headers:{'Content-Type':'application/json'}}));}
  return inner.apply(this,arguments);};}"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_button_asks_then_signs_out_the_rest_and_keeps_this_device():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("window.__settingsAuthGate=()=>Promise.resolve({});__PC.switchView('settings')")
        await b.until("!!document.querySelector('#us-save')")
        await b.js("document.querySelector('.us-tab[data-tab=\"keys\"]').click()")
        await b.until("!!document.querySelector('#us-sessions-revoke') && document.querySelector('#us-sessions-revoke').offsetParent!==null")
        await b.js("document.querySelector('#us-sessions-revoke').click()")
        await asyncio.sleep(.4)
        got['asked_first'] = await b.js("window.__revokes===0 && !!document.querySelector('.uiconfirm')")
        await b.js("[...document.querySelectorAll('.uiconfirm button')].find(x=>/sign them out/i.test(x.textContent)).click()")
        await b.until("/every other device/.test(document.querySelector('#us-sessions-said').textContent)")
        got['calls'] = await b.js('window.__revokes')
        got['token'] = await b.js('window.__PC_TOKEN__')

    asyncio.run(desktop.with_browser('online', '', check,
                                     "localStorage.setItem('pc_nostr_settings',JSON.stringify({osMode:false}));" + FETCH))
    assert got['asked_first'], 'it signed every device out without asking'
    assert got['calls'] == 1 and got['token'] == 'FRESH-TOKEN', got
