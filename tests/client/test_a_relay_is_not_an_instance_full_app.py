"""A relay named as the INSTANCE is refused, and a client already set that way is told so.

"Vyram set his instance to his own relay" (2026-10-10): his .onion relay worked in the relay list, but named as the
instance every server feature failed at once -- Files "couldn't load files from http://…onion (failed to fetch)",
every app "could not establish your app session" -- and Notes looked broken beside them. A PosterChan server's
/client/config is a JSON object with relay_url and nostr_only; a relay answers something else (a NIP-11 document,
a 404). The pickers ask before switching; at boot a configured instance that answers as something else is named.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

RELAYISH = r'''
window.__setInstance=[];
if(window.pcShell){const s=pcShell.setInstance;pcShell.setInstance=async(u)=>{__setInstance.push(u);return true};}
const __pcFetch=window.fetch;
window.fetch=function(url,opts={}){
  const u=String(url);
  const nip11=()=>Promise.resolve(new Response(JSON.stringify({name:'my relay',supported_nips:[1,11,42],software:'strfry'}),{status:200,headers:{'Content-Type':'application/json'}}));
  if(/relay\.example\.onion\/client\/config/.test(u))return nip11();
  if(/good\.example\/client\/config/.test(u))return Promise.resolve(new Response(JSON.stringify({relay_url:'wss://good.example/relay',nostr_only:false,name:'Good'}),{status:200,headers:{'Content-Type':'application/json'}}));
  if(window.__bootRelayish && /\/client\/config$/.test(new URL(u,location.href).pathname))return nip11();
  return __pcFetch(url,opts);
};
'''


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_sign_in_picker_refuses_a_relay_and_accepts_a_posterchan_server():
    got = {}

    async def check(b):
        await b.until("document.body.classList.contains('guest')")
        await b.js("document.querySelector('#btn-auth-conn').click()")
        await b.until("!!document.querySelector('#conn-instance') && !document.querySelector('#auth-conn').classList.contains('hidden')")
        await b.js("document.querySelector('#conn-instance').value='relay.example.onion';document.querySelector('#btn-conn-instance').click()")
        await b.until("/not a PosterChan server/.test(document.querySelector('#conn-error').textContent)")
        got['refused'] = await b.js("document.querySelector('#conn-error').textContent")
        got['set_after_refusal'] = await b.js("__setInstance.slice()")
        await b.js("document.querySelector('#conn-instance').value='good.example';document.querySelector('#btn-conn-instance').click()")
        await b.until("__setInstance.length>0 || /PosterChan server/.test(document.querySelector('#conn-error').textContent)")
        got['set_after_good'] = await b.js("__setInstance.slice()")

    asyncio.run(desktop.with_browser('online', '', check, RELAYISH))
    assert 'relay.example.onion' in got['refused'] and 'Relays' in got['refused'], got
    assert got['set_after_refusal'] == [], ("a relay was saved as the instance", got)
    assert got['set_after_good'] == ['https://good.example'], got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_client_already_pointed_at_a_relay_is_told_and_offered_no_server():
    got = {}

    async def check(b):
        await b.until("!!window.__PC")
        await b.until("/is set as your PosterChan server, but it is not one/.test(document.body.innerText)")
        got['text'] = await b.js("document.body.innerText")

    asyncio.run(desktop.with_browser('online', '', check, "window.__bootRelayish=true;" + RELAYISH))
    assert 'Relays belong under Relays' in got['text'], got['text'][-600:]
