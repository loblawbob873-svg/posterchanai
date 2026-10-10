"""A theme picked in Settings survives the page being rebuilt — and the account's OLDER theme never overwrites it.

Reported from Android: "when I hit the back button to go back … my appearance reset to default". Settings said
"applies instantly; saved to your account", but a pick was only a preview until Save — and Android can rebuild the
app's page when you leave and come back, which threw the pick away. Real client, real reloads; the account is a
small fake that keeps its theme across reloads and can be unreachable (an onion the phone cannot reach yet).
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


ACCOUNT = r"""
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
(()=>{const up=window.fetch;window.fetch=async(url,opts={})=>{
  if(String(url).includes('/api/auth/settings')){
    if(localStorage.getItem('__acct_up')!=='1')return new Response('{"detail":"unreachable"}',{status:503});
    if((opts.method||'GET')==='PUT'){const b=JSON.parse(opts.body||'{}');if(b.theme)localStorage.setItem('__acct_theme',b.theme);
      localStorage.setItem('__acct_puts',String(+(localStorage.getItem('__acct_puts')||0)+1));return new Response('{}',{status:200});}
    return new Response(JSON.stringify({theme:localStorage.getItem('__acct_theme')||'cyberpunk'}),{status:200,headers:{'Content-Type':'application/json'}});}
  return up(url,opts);};})();
"""
THEME = "document.documentElement.getAttribute('data-theme')||'cyberpunk'"


async def _reboot(b):
    await b.call("Page.reload")
    await b.until("!!window.__PC && (!!__PC.me() || document.body.classList.contains('guest'))")
    if not await b.js("!!__PC.me()"):
        await desktop.login(b)
    await asyncio.sleep(1.5)                      # the boot's theme sync runs behind the first paint


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_pick_survives_a_rebuild_and_reaches_the_account_when_it_can():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("localStorage.setItem('__acct_theme','professional');localStorage.setItem('__acct_up','0')")
        await b.js("__PC.switchView('settings')")
        await b.until("!!document.querySelector('#us-theme')")
        await b.js("{const t=document.querySelector('#us-theme');t.value='dark';t.dispatchEvent(new Event('change',{bubbles:true}))}")
        await asyncio.sleep(.3)
        await _reboot(b)                          # Android rebuilt the page; the account is still unreachable
        got["after_rebuild"] = await b.js(THEME)
        await b.js("localStorage.setItem('__acct_up','1')")
        await _reboot(b)                          # the account is reachable again
        got["after_reconnect"] = await b.js(THEME)
        got["account"] = await b.js("localStorage.getItem('__acct_theme')")
        got["pending"] = await b.js("localStorage.getItem('pc_theme_unsynced')")
    asyncio.run(desktop.with_browser("online", "", check, ACCOUNT))
    assert got["after_rebuild"] == "dark", ("the pick was lost when the page was rebuilt", got)
    assert got["after_reconnect"] == "dark", ("the account's older theme overwrote the newer pick", got)
    assert got["account"] == "dark" and got["pending"] is None, ("the pick never reached the account", got)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_theme_changed_on_another_device_still_arrives():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("localStorage.setItem('__acct_up','1');localStorage.setItem('__acct_theme','monero');"
                   "localStorage.removeItem('pc_theme_unsynced')")
        await _reboot(b)
        got["theme"] = await b.js(THEME)
    asyncio.run(desktop.with_browser("online", "", check, ACCOUNT))
    assert got["theme"] == "monero", got
