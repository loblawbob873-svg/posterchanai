"""Files buttons no test pressed, each pressed in the SHIPPED bundle and checked for what it says it does (2026-10-22:
every button gets a test). Files is somebody's encrypted drive, so every endpoint is a fixture and every destructive
button is shown to ASK first and then to send exactly the one intended request.

  * Admin → a user's files: '#badm-purge' asks; Cancel sends nothing and stays on that user; OK posts ONE signed
    purge naming THAT user to /client/blossom-purge and says how many went. '#badm-back' returns to the user list.
  * A big folder: the '[data-id="bl-more"]' button (bl-more) shows the next page of 60, and says how many are left.
  * Offline: the "kept on this device" note's '#bl-offline-retry' asks the server again and, with it back, shows
    the fresh list without the note.
  * Drive check → '#fx-ck-history' asks the server for the retained folder-list versions (signed, history only)
    and lists them; '#fx-history-cancel' closes that list and restores nothing.
  * A synced folder: the sidebar's Trash header '#fx-del-toggle' opens and closes the list of deletions; inside
    the folder's .pc-trash, '#tr-reconcile' reconciles THAT folder's trash and re-reads it from the device.
"""
import asyncio
import json
from pathlib import Path

import pytest

from app.services.nostr import bip340, nostr_service
from tests.client import test_desktop_offline_full_app as desktop

ME_HEX = bip340.pubkey_from_seckey(b"\x01" * 32).hex()          # desktop.login's key
ME = nostr_service.npub_of(ME_HEX)
CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# Plain layout; every toast recorded; the drive's server answered (and recorded, with its body) at the boundary.
FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__toasts=[];window.__calls=[];window.__listAsked=0;
window.__dead=localStorage.getItem('__dead')==='1';
new MutationObserver(ms=>ms.forEach(m=>m.addedNodes.forEach(n=>{if(n.classList&&n.classList.contains('toast'))__toasts.push(n.textContent);}))).observe(document,{childList:true,subtree:true});
window.__srv=Object.assign({list:null,purge:{ok:true,deleted:3},
  backups:[{slot:2,n:12,created_at:1760000000},{slot:3,n:7,created_at:1759000000}]},window.__srv||{});
{const prev=window.fetch;window.fetch=async function(url,opts={}){
  const s=String(url),m=String(opts.method||'GET').toUpperCase(),J=v=>new Response(JSON.stringify(v),{status:200,headers:{'Content-Type':'application/json'}});
  let body=opts.body;try{body=JSON.parse(body);}catch(_){}
  const rec=()=>__calls.push({url:s,method:m,body:body??null});
  if(s.includes('/client/config')){const r=await prev.apply(this,arguments);const d=await r.json();return J({...d,admin_npubs:[%(me)r]});}
  if(s.includes('/client/admin-blossom-usage')){
    const users=(window.__badmUsers||[]).map((x,i)=>({pubkey:x.pk,npub:NostrTools.nip19.npubEncode(x.pk),count:3+i,size:(1+i)*1048576}));
    return J({ok:true,users,total:users.reduce((a,x)=>a+x.size,0)});}
  if(s.includes('/client/blossom-purge')){rec();return J(__srv.purge);}
  if(s.includes('/client/files-index')&&body&&(body.history||body.restore!=null)){rec();return J({ok:true,backups:__srv.backups});}
  if(/\/list\/[0-9a-f]{64}/.test(s)){__listAsked++;rec();
    if(window.__dead)return Promise.reject(new TypeError('Failed to fetch'));
    return J(typeof __srv.list==='function'?__srv.list(s):(__srv.list||[]));}
  return prev.apply(this,arguments);};}
''' % {'me': ME}

TOASTS = "__toasts.join(' | ')"
OK = ".uiconfirm-btns [data-uc=\"1\"]"
CANCEL = ".uiconfirm-btns [data-uc=\"0\"]"


def calls_to(path):
    return f"__calls.filter(c=>c.url.includes({json.dumps(path)}))"


async def click(b, sel):
    await b.js(f"document.querySelector({json.dumps(sel)}).click(); 1")


# Three uploaders with real signed profiles, so no relay is needed to name them.
SEED_USERS = r"""(()=>{const mk=(n,c)=>{const sk=new Uint8Array(32).fill(n),pk=NostrTools.getPublicKey(sk);
  Store.saveProfile(NostrTools.finalizeEvent({kind:0,created_at:Math.floor(Date.now()/1000),tags:[],content:JSON.stringify(c)},sk));
  return {pk};};
  window.__badmUsers=[mk(11,{name:'Alice'}),mk(12,{name:'Bob'}),mk(13,{name:'Carol'})];
  return window.__badmUsers.map(x=>x.pk);})()"""


@CHROME
def test_admin_purge_asks_then_purges_that_user_and_back_returns():
    async def check(b):
        await desktop.login(b)
        pks = await b.js(SEED_USERS)
        alice = pks[0]
        await b.js("window.__srv.list=[{sha256:'c'.repeat(64),size:2048,type:'image/png',uploaded:1760000000}]; 1")
        await b.js("__PC.switchView('blossom'); 1")
        await b.until("!!document.querySelector('[data-files-mode=\"admin\"]')")
        await click(b, '[data-files-mode="admin"]')
        await b.until("document.querySelectorAll('.badm-row').length===3")
        await click(b, f'.badm-row[data-pk="{alice}"]')
        await b.until("!!document.querySelector('#badm-back') && !!document.querySelector('#badm-purge') && "
                      "document.querySelectorAll('#badm-grid a').length===1")
        assert await b.js("/Alice/.test(document.querySelector('#files-special').textContent)")

        # Cancel: nothing is purged, and the admin stays on Alice.
        await click(b, '#badm-purge')
        await b.until(f"!!document.querySelector({json.dumps(CANCEL)})")
        assert await b.js("/Purge ALL of this user.s files/.test(document.querySelector('.uiconfirm-msg').textContent)")
        await click(b, CANCEL)
        await b.until("!document.querySelector('.uiconfirm-btns') && !!document.querySelector('#badm-purge')")
        await asyncio.sleep(.3)
        assert await b.js(f"{calls_to('/client/blossom-purge')}.length") == 0, 'Cancel still purged'
        assert await b.js("!!document.querySelector('#badm-back')"), 'Cancel left the user'

        # OK: ONE purge, naming Alice, signed by this admin for this action.
        await click(b, '#badm-purge')
        await b.until(f"!!document.querySelector({json.dumps(OK)})")
        await click(b, OK)
        await b.until(f"{calls_to('/client/blossom-purge')}.length===1 && /purged 3 file\\(s\\)/.test({TOASTS})")
        call = (await b.js(calls_to('/client/blossom-purge')))[0]
        assert call['method'] == 'POST' and call['body']['target'] == alice, call
        auth = await b.js(f"JSON.parse(atob({json.dumps(call['body']['auth'])}))")
        assert auth['kind'] == 27235 and auth['pubkey'] == ME_HEX and auth['content'] == 'blossom-purge', auth
        assert ['action', 'purge'] in auth['tags'] and ['p', alice] in auth['tags'], auth
        assert await b.js("NostrTools.verifyEvent(" + json.dumps(auth) + ")"), 'the proof is not a valid signature'
        await asyncio.sleep(.5)
        assert await b.js(f"{calls_to('/client/blossom-purge')}.length") == 1, 'purged more than once'

        # ‹ Back: the user list again.
        await click(b, '#badm-back')
        await b.until("!document.querySelector('#badm-back') && document.querySelectorAll('.badm-row').length===3")
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))


MANY = r'''
window.__srv=Object.assign(window.__srv||{},{list:()=>{const out=[];for(let i=0;i<150;i++){const h=(i.toString(16).padStart(4,'0')).repeat(16);
  out.push({sha256:h,size:1234+i,type:'application/pdf',uploaded:1700000000+i,url:location.origin+'/'+h+'.pdf'});}return out;}});
window.pcShell.windowContext={role:'app',view:'blossom'};window.pcShell.backgroundOwner=false;
'''
CARDS = "document.querySelectorAll('.file-card[data-sha]').length"
MORE = '[data-id="bl-more"]'


@CHROME
def test_load_more_shows_the_next_page():
    async def check(b):
        # A phone: the page does not load itself until scrolled (test_files_all_does_not_load_itself_on_a_phone).
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 393, 'height': 851, 'deviceScaleFactor': 2.75, 'mobile': True})
        await desktop.login(b)
        await b.until("!!document.querySelector('.folder-chip[data-folder=\"\"]')")
        await b.js("document.querySelector('.folder-chip[data-folder=\"\"]').click(); 1")
        await b.until(CARDS + " === 60 && !!document.querySelector('" + MORE.replace('"', '\\"') + "')")
        assert 'Load 60 more · 90 left' in await b.js(f"document.querySelector({json.dumps(MORE)}).textContent")
        await click(b, MORE)
        await b.until(CARDS + " === 120")
        assert 'Load 30 more · 30 left' in await b.js(f"document.querySelector({json.dumps(MORE)}).textContent")
        await click(b, MORE)
        await b.until(CARDS + " === 150")
        assert not await b.js(f"!!document.querySelector({json.dumps(MORE)})"), 'a Load more with nothing left'
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '?pcwin=blossom', check, FIXTURE + MANY))


SHA = 'ab' * 32
SEED_INDEX = """(() => { const F = __PC.filesIdx();
  F.data = { folders: ['Receipts'], files: { '%s': { name: 'tax-2026.pdf', folder: 'Receipts', type: 'application/pdf' } }, encFolders: [] };
  F._norm(); F._pullDone = true; F._pullOk = true; F._pullBlocked = false; F.saveLocal(); return true; })()""" % SHA
LIST_ONE = "window.__srv.list=[{sha256:'%s',size:4096,type:'application/pdf',uploaded:1760000000}]; 1" % SHA
SHOWN = "!!document.querySelector('.file-card[data-sha=\"%s\"]')" % SHA
DEAD = r'''
if(window.__dead){
  Object.defineProperty(Navigator.prototype,'onLine',{configurable:true,get:()=>!window.__dead});
  window.WebSocket=class extends EventTarget{static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
    constructor(u){super();this.url=String(u);this.readyState=0;} send(){} close(){this.readyState=3;}};
}
'''


async def _open_receipts(b):
    await b.js("__PC.switchView('blossom'); 1")
    vis = "[...document.querySelectorAll('[data-folder=\"Receipts\"]')].find(e=>e.getClientRects().length&&e.offsetParent)"
    await b.until("!!" + vis)
    await b.js(vis + ".click(); 1")


@CHROME
def test_offline_retry_asks_again_and_shows_the_fresh_list():
    async def check(b):
        await desktop.login(b)
        await b.js(SEED_INDEX)
        await b.js(LIST_ONE)
        await _open_receipts(b)
        await b.until(SHOWN)
        await asyncio.sleep(1.0)                        # the device copy is written behind the paint

        await b.js("localStorage.setItem('__dead','1')")
        await b.call('Page.reload')
        await b.until('!!window.__PC')
        await b.until('!!__PC.me()')
        await b.js(LIST_ONE)
        await _open_receipts(b)
        await b.until("!!document.querySelector('#bl-offline-retry') && " + SHOWN)
        assert await b.js("/kept on this device/.test(document.querySelector('#bl-offline').textContent)")
        asked = await b.js('__listAsked')

        # The server is back: Retry asks it again, and the fresh answer replaces the kept one.
        await b.js("localStorage.removeItem('__dead'); window.__dead=false; "
                   "window.__srv.list.push({sha256:'cd'.repeat(32),size:10,type:'text/plain',uploaded:1760000001}); 1")
        await click(b, '#bl-offline-retry')
        await b.until(f"__listAsked>{asked} && !document.querySelector('#bl-offline') && " + SHOWN)
        assert not await b.js("!!document.querySelector('#bl-offline-retry')")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE + DEAD))


@CHROME
def test_drive_check_reviews_retained_lists_and_cancel_restores_nothing():
    async def check(b):
        await desktop.login(b)
        await b.js(SEED_INDEX)
        await b.js(LIST_ONE)
        await b.js("__PC.switchView('blossom'); 1")
        await b.until("!!document.querySelector('.fx-check')")
        await click(b, '.fx-check')
        await b.until("!!document.querySelector('#fx-ck-history')")
        assert await b.js("/Everything checks out/.test(document.querySelector('#modal-root').textContent)")

        await click(b, '#fx-ck-history')
        await b.until("document.querySelectorAll('.fx-history-row').length===2 && !!document.querySelector('#fx-history-cancel')")
        hist = await b.js(calls_to('/client/files-index'))
        assert len(hist) == 1 and hist[0]['body']['history'] is True and hist[0]['body']['pubkey'] == ME_HEX, hist
        assert 'restore' not in hist[0]['body'], hist
        assert await b.js("/12 files/.test(document.querySelector('.fx-history-list').textContent)")

        await click(b, '#fx-history-cancel')
        await b.until("!document.querySelector('#fx-history-cancel') && !document.querySelector('.fx-history-row')")
        await asyncio.sleep(.3)
        assert await b.js(f"{calls_to('/client/files-index')}.filter(c=>c.body.restore!=null).length") == 0, 'Cancel restored'
        assert not await b.js("/restor/.test(" + TOASTS + ")"), await b.js(TOASTS)
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))


# One synced folder, "Camera": a live file, one deletion that can be restored and one that cannot, and a
# .pc-trash on this device holding one file. PCSync's account calls and the device's trash are the boundary.
SYNC = r"""(() => {
  window.__sync = {reconciled: [], listTrash: 0, trash: [{at: '.pc-trash/2026-10-20/beach.jpg', to: 'beach.jpg', size: 900, mtime: 1760000000000}]};
  const S = window.PCSync;
  S.accountFolders = async () => false;
  S.acct = () => [{key: 'Camera', n: 1}];
  S.folders = () => [{id: 'f1', key: 'Camera'}];
  S.docs.state = async () => ({state: {
    'dog.jpg': {sha: 'aa'.repeat(32), size: 500, mtime: 1760000000000},
    'old.txt': {sha: 'bb'.repeat(32), size: 5, mtime: 1750000000000, deletedAt: 1755000000000},
    'gone.txt': {size: 5, mtime: 1750000000000, deletedAt: 1755000000000}}});
  S.reconcileTrash = async (id) => { __sync.reconciled.push(id); __sync.trash = []; return {restored: [], purged: 1, kept: 0}; };
  window.pcFs = Object.assign(window.pcFs || {}, {listTrash: async (id) => { __sync.listTrash++; return id === 'f1' ? __sync.trash.slice() : []; }});
  window.dispatchEvent(new Event('pc:sync-folders'));
  return true; })()"""


@CHROME
def test_synced_folder_trash_toggle_and_reconcile():
    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('blossom'); 1")
        await b.until("!!document.querySelector('#files-pane')")
        await b.js(SYNC)
        await b.until("!!document.querySelector('.folder-chip[data-synckey=\"Camera\"]')")
        await click(b, '.folder-chip[data-synckey="Camera"]')
        # The FIRST visit must show the Trash: the sidebar is drawn before the folder's record arrives, and it
        # used to stay drawn without it until something else repainted (found by this test, 2026-10-22).
        await b.until("/dog\\.jpg/.test(document.querySelector('#bl-grid').textContent)")
        await asyncio.sleep(.5)
        assert await b.js("!!document.querySelector('#fx-del-toggle')"), 'the first visit to a synced folder hides its Trash'

        # The Trash header opens the list of deletions, and closes it again.
        body = "document.querySelector('.fx-trash-body')"
        assert await b.js(body + ".classList.contains('hidden')")
        assert await b.js("document.querySelector('#fx-del-toggle .fx-n').textContent") == '2'
        await click(b, '#fx-del-toggle')
        await b.until("!!" + body + " && !" + body + ".classList.contains('hidden')")
        assert await b.js(body + ".getClientRects().length>0"), 'opened but not on screen'
        assert await b.js("!!document.querySelector('[data-undelete=\"old.txt\"]')")
        assert await b.js("/1 of 2 can be put back/.test(" + body + ".textContent)")
        await click(b, '#fx-del-toggle')
        await b.until(body + ".classList.contains('hidden')")

        # Into this device's .pc-trash: Reconcile Trash runs for THIS folder, then the trash is read again.
        await b.until("!!document.querySelector('.file-card[data-dir=\".pc-trash\"]')")
        await click(b, '.file-card[data-dir=".pc-trash"]')
        await b.until("!!document.querySelector('#tr-reconcile') && !!document.querySelector('.file-card[data-dir=\"2026-10-20\"]')")
        await click(b, '.file-card[data-dir="2026-10-20"]')
        await b.until("!!document.querySelector('#tr-reconcile') && !!document.querySelector('.tr-back[data-at=\".pc-trash/2026-10-20/beach.jpg\"]')")
        reads = await b.js("__sync.listTrash")
        await click(b, '#tr-reconcile')
        await b.until(f"__sync.reconciled.length===1 && __sync.listTrash>{reads} && !document.querySelector('.tr-back')")
        assert await b.js("__sync.reconciled") == ['f1']
        assert await b.js("/is empty/.test(document.querySelector('#bl-grid').textContent)")
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
