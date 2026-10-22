"""Settings: the account card, relays and the wallet connection work through the real controls (2026-10-22: every
button gets a test -- 30 of settings.js's 53 buttons were named by none).

  * Copy npub copies YOUR npub; Show nsec (confirmed) shows the key and Copy copies exactly it; Close closes.
  * Relays: "Add relay" adds an empty relay row to fill in.
  * Wallet (NWC): a string that is not nostr+walletconnect:// is refused with a sentence; a valid one is saved and
    says so; Disconnect clears it.
  * Log out (confirmed) returns to the signed-out screen.
Real login (a local key), DOM and settings.js; the clipboard is recorded.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__clip=[];
const __exec=document.execCommand.bind(document);
document.execCommand=function(c,...r){ if(c==='copy'){ const a=document.activeElement; __clip.push(a&&'value' in a?a.value:String(getSelection())); return true; } return __exec(c,...r); };
try{ navigator.clipboard.writeText=async t=>{ __clip.push(String(t)); }; }catch(_){}
'''

NWC_OK = "nostr+walletconnect://" + "a" * 64 + "?relay=wss%3A%2F%2Frelay.example&secret=" + "b" * 64


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def open_tab_holding(b, sel):
    """Click the user-settings tab whose pane holds `sel`."""
    pane = await b.js(f"(()=>{{const e=document.querySelector({json.dumps(sel)});const p=e&&e.closest('.us-pane');return p?p.dataset.pane:''}})()")
    assert pane, f"no settings pane holds {sel}"
    await b.js(f"document.querySelector('.us-tab[data-tab={json.dumps(pane)}]').click(); 1")
    await b.until(f"(()=>{{const e=document.querySelector({json.dumps(sel)});return !!e&&e.offsetParent!==null}})()")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_account_relays_wallet_and_logout():
    async def check(b):
        await desktop.login(b)
        await b.js("window.__publishOK=true; __PC.uiConfirm=async()=>true; __PC.switchView('settings'); 1")
        await b.until("!!document.querySelector('#set-copy-npub')")
        npub = await b.js("__PC.me().npub")
        nsec = await b.js("NostrTools.nip19.nsecEncode(new Uint8Array(32).fill(1))")
        await b.js("document.querySelector('#set-copy-npub').click(); 1")
        await b.until(f"__clip.includes({json.dumps(npub)})")
        await b.js("document.querySelector('#set-show-nsec').click(); 1")
        await b.until("!!document.querySelector('#nsec-copy')")
        await b.js("document.querySelector('#nsec-copy').click(); 1")
        await b.until(f"__clip.includes({json.dumps(nsec)})")
        await b.js("document.querySelector('#nsec-close').click(); 1")
        await b.until("!document.querySelector('#nsec-close')")
        # Relays: Add relay adds a row.
        await b.until("!!document.querySelector('#set-relay-add')")
        await open_tab_holding(b, "#set-relay-add")
        rows = await b.js("document.querySelectorAll('#set-relay-add').length && document.querySelectorAll('.us-pane[data-pane] input[type=url], .set-relay-row input, .relay-row input').length")
        await b.js("document.querySelector('#set-relay-add').click(); 1")
        await b.until(f"document.querySelectorAll('.us-pane[data-pane] input[type=url], .set-relay-row input, .relay-row input').length>{rows}")
        # Wallet connection: refuse junk, save a real one, disconnect.
        await open_tab_holding(b, "#set-nwc-save")
        await b.js("(()=>{const i=document.querySelector('#set-nwc');i.value='not a wallet';document.querySelector('#set-nwc-save').click();})(); 1")
        await b.until("/Not a valid nostr\\+walletconnect/.test(document.querySelector('#set-nwc-status').textContent)")
        await b.js(f"(()=>{{const i=document.querySelector('#set-nwc');i.value={json.dumps(NWC_OK)};document.querySelector('#set-nwc-save').click();}})(); 1")
        await b.until("/Wallet connected/.test(document.querySelector('#set-nwc-status').textContent)")
        await b.js("document.querySelector('#set-nwc-clear').click(); 1")
        await b.until("/Disconnected/.test(document.querySelector('#set-nwc-status').textContent) && document.querySelector('#set-nwc').value===''")
        # Log out.
        await b.js("__PC.switchView('settings'); 1")
        await b.until("!!document.querySelector('#set-logout')")
        await b.js("document.querySelector('#set-logout').click(); 1")
        # The dialog's own message and its OK button -- "Log out" also appears on the page behind it.
        await b.until("[...document.querySelectorAll('.uiconfirm')].some(d=>/Log out of this device/.test(d.textContent)&&d.querySelector('[data-uc=\"1\"]'))")
        await b.js("[...document.querySelectorAll('.uiconfirm')].find(d=>/Log out of this device/.test(d.textContent)).querySelector('[data-uc=\"1\"]').click(); 1")
        # Logging out reloads the page: mid-navigation there is no <body> yet.
        await b.until("!!document.body && document.body.classList.contains('guest')")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))


RELAY_INPUTS = "[...document.querySelectorAll('.us-pane[data-pane] input[type=url], .set-relay-row input, .relay-row input')].map(i=>i.value)"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_relays_import_from_the_extension_and_from_a_nip05():
    """Import from extension adds the extension's relays; Import from NIP-05 adds the relays its nostr.json names."""
    async def check(b):
        await desktop.login(b)
        await b.js("""(()=>{window.nostr=window.nostr||{};window.nostr.getRelays=async()=>({'wss://ext.example':{read:true,write:true}});
          const f=window.fetch;window.fetch=(u,o)=>String(u).startsWith('https://names.example/.well-known/nostr.json')
            ?Promise.resolve(new Response(JSON.stringify({names:{bob:'b'.repeat(64)},relays:{['b'.repeat(64)]:['wss://bob.example']}}),{status:200,headers:{'Content-Type':'application/json'}}))
            :f(u,o);})(); 1""")
        await b.js("__PC.switchView('settings'); 1")
        await b.until("!!document.querySelector('#set-relay-ext')")
        await open_tab_holding(b, "#set-relay-ext")
        await b.js("document.querySelector('#set-relay-ext').click(); 1")
        await b.until(f"{RELAY_INPUTS}.some(v=>/ext\\.example/.test(v))")
        await b.js("document.querySelector('#set-nip05').value='bob@names.example'; document.querySelector('#set-relay-nip05').click(); 1")
        await b.until(f"{RELAY_INPUTS}.some(v=>/bob\\.example/.test(v))")
        assert await b.js(f"{RELAY_INPUTS}.some(v=>/ext\\.example/.test(v))"), 'the NIP-05 import dropped the extension relay'
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))


SERVER = r'''
window.__api=[];window.__sentAll=[];
const __sx=WebSocket.prototype.send;
// The shared fixture relay ignores `until`, so a client paging back through its history (Delete all my posts
// does) is handed the same newest event for ever. A real relay honours it; these REQs are answered here.
WebSocket.prototype.send=function(r){let m=null;try{m=JSON.parse(r);}catch(_){}
  if(Array.isArray(m)&&m[0]==='EVENT')__sentAll.push(m[1]);
  if(Array.isArray(m)&&m[0]==='REQ'&&m.slice(2).some(f=>f&&f.until!=null)){
    setTimeout(()=>{for(const ev of window.__events||[]){if(m.slice(2).some(f=>(!f.kinds||f.kinds.includes(ev.kind))&&(!f.authors||f.authors.includes(ev.pubkey))
      &&(f.until==null||ev.created_at<=f.until)&&(f.since==null||ev.created_at>=f.since)))this.fire('message',['EVENT',m[1],ev]);}
      this.fire('message',['EOSE',m[1]]);},15);
    return;}
  return __sx.call(this,r)};
const __sf=window.fetch;
window.fetch=(u,o)=>{const s=String(u),m=String((o&&o.method)||'GET').toUpperCase(),json=v=>Promise.resolve(new Response(JSON.stringify(v),{status:200,headers:{'Content-Type':'application/json'}}));
  if(s.includes('/api/auth/api-keys')){__api.push(m+' api-keys');return json(m==='POST'?{key:'pcak_fixture_123'}:[{id:1,name:'laptop',prefix:'pcak_fix'}]);}
  if(s.includes('/client/sync-posts')){__api.push('sync-posts');return json({ok:true,started:true,private:false});}
  return __sf(u,o);};
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_api_key_mail_row_sync_and_delete_all_posts():
    """Settings: "Create key" makes an API key and shows it ONCE; "Add mail account" adds an empty account row;
    "Sync my data to this relay" asks the server to pull your data back; "Delete all my posts" (confirmed)
    publishes the deletions of what you wrote."""
    async def check(b):
        await desktop.login(b)
        await b.js("window.__publishOK=true; __PC.publish(1,'a post to delete later',[]); 1")
        await b.until("__sentAll.some(e=>e.kind===1)")
        await b.js("__PC.switchView('settings'); 1")
        await b.until("!!document.querySelector('#us-key-new')")
        await open_tab_holding(b, "#us-key-new")
        await b.js("document.querySelector('#us-key-name').value='laptop'; document.querySelector('#us-key-new').click(); 1")
        await b.until("!!document.querySelector('.uiprompt-in') && document.querySelector('.uiprompt-in').value==='pcak_fixture_123'")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click(); 1")
        await b.until("__api.filter(x=>x==='GET api-keys').length>=1 && /laptop/.test(document.querySelector('#us-key-list').textContent)")

        await open_tab_holding(b, "#us-mail-add")
        rows = await b.js("document.querySelectorAll('#us-mail-list .us-mail').length")
        await b.js("document.querySelector('#us-mail-add').click(); 1")
        await b.until(f"document.querySelectorAll('#us-mail-list .us-mail').length==={rows + 1}")

        await b.js("__PC.switchView('settings'); 1")
        await b.until("!!document.querySelector('#set-sync-posts')")
        await b.js("document.querySelector('#set-sync-posts').click(); 1")
        await b.until("__api.includes('sync-posts')")
        # The fixture relay acknowledges without keeping; give it the post, as a real relay holds it.
        await b.js("window.__events=(window.__events||[]).concat(__published.filter(e=>e.kind===1)); 1")
        await b.js("document.querySelector('#set-del-notes').click(); 1")
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click(); 1")
        # Second question, with the count it found: "Found 1 … Permanently request their deletion now?"
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]') && /Found 1 /.test(document.querySelector('.uiconfirm-msg').textContent)")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click(); 1")
        mine = await b.js("__sentAll.find(e=>e.kind===1).id")
        await b.until(f"__sentAll.some(e=>e.kind===5&&e.tags.some(t=>t[0]==='e'&&t[1]==={mine!r}))", )
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE + SERVER))
