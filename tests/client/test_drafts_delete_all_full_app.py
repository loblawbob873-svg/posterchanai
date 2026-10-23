"""Drafts → "Delete all drafts" (#draft-clear) does what it says, in the SHIPPED bundle (2026-10-22: every button gets
a test).

Cancel keeps every draft. Confirmed, every draft leaves the screen and the device, and the deletion goes to the
server copy as TOMBSTONES -- a plain drop would be resurrected by the next sync merge from the other device. Real
login, DOM and drafts.js; only the network is a fixture.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__draftPosts=[];
const __dFetch=window.fetch;
window.fetch=async function(url,opts={}){
 if(String(url).includes('/client/drafts')){const body=JSON.parse(opts.body||'{}');__draftPosts.push(body.drafts||null);
  return new Response(JSON.stringify({ok:true,drafts:body.drafts||[]}),{status:200,headers:{'Content-Type':'application/json'}});}
 return __dFetch(url,opts);
};
'''

STORE = "JSON.parse(localStorage.getItem('pc_drafts_'+__PC.me().pubkey)||'[]')"


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_delete_all_drafts_cancel_keeps_them_and_confirm_tombstones_them():
    async def check(b):
        await desktop.login(b)
        await b.js("(()=>{const now=Math.floor(Date.now()/1000);"
                   "localStorage.setItem('pc_drafts_'+__PC.me().pubkey,JSON.stringify(["
                   "{id:'da',text:'First unsent thought',ts:now-60},{id:'db',text:'Second unsent thought',ts:now-30}]));"
                   "__PC.switchView('drafts');})(); 1")
        await b.until("document.querySelectorAll('.draft-card[data-draft]').length===2")
        assert '(2)' in await b.js("document.querySelector('#draft-clear').innerText")
        # Cancel: nothing goes.
        await b.js("document.querySelector('#draft-clear').click(); 1")
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"0\"]')")
        assert 'Delete all 2 drafts' in await b.js("document.querySelector('.uiconfirm-btns').parentElement.innerText")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"0\"]').click(); 1")
        await b.until("!document.querySelector('.uiconfirm-btns')")
        assert await b.js("document.querySelectorAll('.draft-card[data-draft]').length") == 2
        assert await b.js(f"{STORE}.filter(d=>!d.del).length") == 2
        # Confirm: the screen empties, the store holds tombstones, the toast says so, and the sync carries them.
        await b.js("window.__draftPosts=[]; document.querySelector('#draft-clear').click(); 1")
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click(); 1")
        await b.until("!document.querySelector('.draft-card[data-draft]') && !!document.querySelector('#drafts-empty')")
        await b.until("/drafts deleted/.test(document.body.innerText)")
        assert not await b.js("!!document.querySelector('#draft-clear')"), "Delete all left on an empty screen"
        store = await b.js(STORE)
        assert sorted(d["id"] for d in store if d.get("del")) == ["da", "db"] and not [d for d in store if not d.get("del")], store
        await b.until("__draftPosts.some(p=>Array.isArray(p)&&['da','db'].every(id=>p.some(d=>d.id===id&&d.del)))")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
