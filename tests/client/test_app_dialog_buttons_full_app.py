"""App-wide dialog buttons no test pressed, each pressed in the SHIPPED bundle and checked for what it says it does
(2026-10-22: every button gets a test).

  * AI chat → `help` opens the command sheet: '#cmds-close' closes it and inserts nothing; '#cmds-fx' swaps it for
    the Effects studio (fetching the effect catalogue), whose '#fxs-clear' empties the command it was building.
  * Effects on a post when this account has no AI access: '#fx-close' closes the explanation and stays where you
    were; '#fx-req' closes it and takes you to the AI screen, where access is requested.
  * Sign in with Google, first time: the "your key lives on this server" notice's '#cust-ok' dismisses it, signed in.
  * Files → AI files with unreferenced artifacts: '#orphan-prune' ASKS first (Cancel sends nothing), then posts a
    signed 27235 for YOUR pubkey to /client/ai-files-prune and says what was freed.
  * Android signer (Settings → Signer): '#signer-start' hands the pairings to the native service and says it
    started; '#signer-batt' opens Android's battery settings and says what to choose.
"""
import asyncio
import json
from pathlib import Path

import pytest

from app.services.nostr import bip340
from tests.client import test_desktop_offline_full_app as desktop

ME_HEX = bip340.pubkey_from_seckey(b"\x01" * 32).hex()          # desktop.login's key
CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# Plain layout; every toast recorded; the server answered (and recorded, with its body) at the network boundary.
FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__toasts=[];window.__calls=[];
new MutationObserver(ms=>ms.forEach(m=>m.addedNodes.forEach(n=>{if(n.classList&&n.classList.contains('toast'))__toasts.push(n.textContent);}))).observe(document,{childList:true,subtree:true});
window.__srv=Object.assign({canAi:true,orphans:[],prune:{ok:true,bytes:3*1048576,deleted:2}},window.__srv||{});
{const prev=window.fetch;window.fetch=async function(url,opts={}){
  const s=String(url),m=String(opts.method||'GET').toUpperCase(),J=v=>new Response(JSON.stringify(v),{status:200,headers:{'Content-Type':'application/json'}});
  const rec=()=>{let b=opts.body;try{b=JSON.parse(b);}catch(_){}__calls.push({url:s,method:m,body:b??null});};
  if(s.includes('/auth/nostr-login'))return J({access_token:'fixture-token',user:{id:1,can_ai:__srv.canAi,can_blossom:true}});
  if(s.includes('/client/commands')){rec();return J({groups:[{title:'Pictures',items:[{name:'imagine',desc:'make a picture'},{name:'upscale',desc:'make it bigger'}]}],effects_count:2});}
  if(s.includes('/client/effects')){rec();return J({effects:[{name:'glow',desc:'Glow'},{name:'chimp',desc:'Chimp'}],motions:['zoom'],enhance:[]});}
  if(s.includes('/api/auth/handoff')){rec();return J({nsec:NostrTools.nip19.nsecEncode(new Uint8Array(32).fill(1)),created:true,provider:'google',account:'me@gmail.example'});}
  if(s.includes('/client/ai-files-prune')){rec();return J(__srv.prune);}
  if(s.includes('/client/ai-files')){rec();return J({ok:true,files:[],orphans:__srv.orphans,orphan_bytes:3*1048576});}
  return prev.apply(this,arguments);};}
'''

TOASTS = "__toasts.join(' | ')"


def calls_to(path):
    return f"__calls.filter(c=>c.url.includes({json.dumps(path)}))"


@CHROME
def test_help_sheet_close_effects_and_clear():
    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('ai'); 1")
        await b.until("!!document.querySelector('#ai-input') && !!document.querySelector('#ai-send')")
        say_help = ("(()=>{const t=document.querySelector('#ai-input');t.value='help';t.dispatchEvent(new Event('input'));"
                    "document.querySelector('#ai-send').click();})(); 1")

        # ✕ closes the sheet; nothing is inserted into the composer.
        await b.js(say_help)
        await b.until("!!document.querySelector('#cmds-close') && document.querySelectorAll('.cmds-item').length===2")
        await b.js("document.querySelector('#cmds-close').click(); 1")
        await b.until("!document.querySelector('#cmds-close')")
        assert await b.js("document.querySelector('#ai-input').value") == ''

        # Effects… closes the command sheet and opens the Effects studio from the effect catalogue.
        await b.js(say_help)
        await b.until("!!document.querySelector('#cmds-fx')")
        await b.js("document.querySelector('#cmds-fx').click(); 1")
        await b.until("!document.querySelector('#cmds-fx') && !!document.querySelector('#fxs-clear') && "
                      "document.querySelectorAll('#fxs-eff .fxs-chip').length===2")
        assert await b.js(f"{calls_to('/client/effects')}.length") == 1

        # Clear: pick an effect and a motion → the command shows them; Clear empties it and Apply goes dead.
        await b.js("document.querySelector('.fxs-chip[data-pick=\"glow\"]').click(); "
                   "document.querySelector('.fxs-chip[data-pick=\"zoom\"]').click(); "
                   "document.querySelector('#fxs-cap').value='hello'; document.querySelector('#fxs-cap').dispatchEvent(new Event('input')); 1")
        await b.until("/glow/.test(document.querySelector('#fxs-cmd').textContent) && !document.querySelector('#fxs-go').disabled")
        built = await b.js("document.querySelector('#fxs-cmd').textContent")
        assert 'zoom' in built and 'hello' in built, built
        await b.js("document.querySelector('#fxs-clear').click(); 1")
        await b.until("document.querySelector('#fxs-cmd').textContent==='nothing picked yet'")
        assert await b.js("document.querySelector('#fxs-go').disabled")
        assert await b.js("document.querySelector('#fxs-cap').value") == ''
        assert await b.js("document.querySelectorAll('.fxs-chip.on').length") == 0
        assert await b.js("!!document.querySelector('#fxs-clear')"), 'Clear closed the studio'
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))


# A post with a picture, so the note menu offers Effects.
POST = r'''
Object.assign(window.__srv,{canAi:false});
addEventListener('DOMContentLoaded',()=>{const key=new Uint8Array(32).fill(1);
  window.__events=[NostrTools.finalizeEvent({kind:1,created_at:Math.floor(Date.now()/1000),content:'look '+location.origin+'/fixture.png',tags:[]},key)];});
'''


@CHROME
def test_effects_without_ai_access_close_and_request():
    async def open_effects(b):
        await b.js("__PC.switchView('global'); 1")
        await b.until("!!document.querySelector('.note [data-a=menu]')")
        await b.js("document.querySelector('.note [data-a=menu]').click(); 1")
        await b.until("!!document.querySelector('.menu-pop [data-m=effect]')")
        await b.js("document.querySelector('.menu-pop [data-m=effect]').click(); 1")
        await b.until("!!document.querySelector('#fx-close') && !!document.querySelector('#fx-req')")

    async def check(b):
        await desktop.login(b)
        await open_effects(b)
        assert await b.js("/aren.t enabled for your account/.test(document.querySelector('.modal').textContent)")
        await b.js("document.querySelector('#fx-close').click(); 1")
        await b.until("!document.querySelector('#fx-close')")
        assert await b.js("__PC.isView('global')"), 'Close left the screen it was on'
        assert not await b.js("__PC.isView('ai')")

        await open_effects(b)
        await b.js("document.querySelector('#fx-req').click(); 1")
        await b.until("!document.querySelector('#fx-req') && __PC.isView('ai')")
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE + POST))


@CHROME
def test_first_google_sign_in_notice_dismisses():
    async def check(b):
        await b.until("!!document.querySelector('#cust-ok')")
        assert await b.js("__PC.me().pubkey") == ME_HEX
        assert await b.js(f"{calls_to('/api/auth/handoff')}.map(c=>c.body.code)") == ['one-time-code']
        assert 'login=' not in await b.js("location.href"), 'the single-use code stayed in the URL'
        assert await b.js("/Signed in with Google/.test(document.querySelector('.modal').textContent)")
        await b.js("document.querySelector('#cust-ok').click(); 1")
        await b.until("!document.querySelector('#cust-ok') && !document.querySelector('#modal-root .modal')")
        assert await b.js("__PC.me().pubkey") == ME_HEX, 'dismissing the notice signed you out'
    asyncio.run(desktop.with_browser('online', '?login=one-time-code', check, FIXTURE))


ORPHANS = r'''
Object.assign(window.__srv,{orphans:[{sha:'a'.repeat(64),size:1048576},{sha:'b'.repeat(64),size:2097152}]});
'''


@CHROME
def test_orphan_prune_asks_then_cleans_up():
    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('blossom'); 1")
        await b.until("!!document.querySelector('[data-files-mode=\"ai\"]')")
        await b.js("document.querySelector('[data-files-mode=\"ai\"]').click(); 1")
        await b.until("!!document.querySelector('#orphan-prune')")
        assert await b.js("/2.*unreferenced artifacts/.test(document.querySelector('.files-orphans').textContent)")

        # Cancel: nothing is deleted, nothing is sent.
        await b.js("document.querySelector('#orphan-prune').click(); 1")
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"0\"]')")
        assert await b.js("/Delete every unreferenced artifact/.test(document.querySelector('.uiconfirm-btns').closest('.modal,.uiconfirm,div').parentElement.textContent)")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"0\"]').click(); 1")
        await b.until("!document.querySelector('.uiconfirm-btns')")
        await asyncio.sleep(.3)
        assert await b.js(f"{calls_to('/client/ai-files-prune')}.length") == 0, 'Cancel still cleaned up'
        assert not await b.js("document.querySelector('#orphan-prune').disabled")

        # Confirm: one signed request for THIS account, then the result is said.
        await b.js("document.querySelector('#orphan-prune').click(); 1")
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click(); 1")
        await b.until(f"{calls_to('/client/ai-files-prune')}.length===1 && /freed 3(\\.0)? MB \\(2 files\\)/.test({TOASTS})")
        call = (await b.js(f"{calls_to('/client/ai-files-prune')}"))[0]
        assert call['method'] == 'POST' and call['body']['pubkey'] == ME_HEX, call
        auth = await b.js(f"JSON.parse(atob({json.dumps(call['body']['auth'])}))")
        assert auth['kind'] == 27235 and auth['pubkey'] == ME_HEX and ['p', ME_HEX] in auth['tags'], auth
        assert await b.js("NostrTools.verifyEvent(" + json.dumps(auth) + ")"), 'the proof is not a valid signature'
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE + ORPHANS))


# The Android app's Signer plugin: the background service is off until it is handed the pairings.
ANDROID = r'''
window.__sig={running:false,exempt:false,calls:[]};
window.Capacitor={isNativePlatform:()=>true,getPlatform:()=>'android',Plugins:{
  Signer:{status:async()=>({have:false,exposed:false,serviceWanted:false,serviceRunning:__sig.running,connected:__sig.running?1:0,batteryExempt:__sig.exempt}),
    arm:async o=>{__sig.calls.push(['arm']);return {};},
    sync:async o=>{__sig.calls.push(['sync',o.enabled]);__sig.running=true;return {running:true,connected:1};},
    openBatterySettings:async()=>{__sig.calls.push(['battery']);return {};}}}};
'''


@CHROME
def test_android_signer_start_and_battery():
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 412, "height": 900, "deviceScaleFactor": 2, "mobile": True})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){} __PC.switchView('signer'); 1")
        await b.until("!!document.querySelector('#signer-start')")
        assert await b.js("/Background signing is paused/.test(document.querySelector('#signer-apps').textContent)")
        assert not await b.js("!!document.querySelector('#signer-batt')")

        await b.js("document.querySelector('#signer-start').click(); 1")
        await b.until(f"/background signer started/.test({TOASTS}) && __sig.calls.some(c=>c[0]==='sync')")
        # It repaints from what the SERVICE now reports: running, and Android still optimising its battery.
        await b.until("!document.querySelector('#signer-start') && !!document.querySelector('#signer-batt')")
        assert await b.js("/Ready to sign while PosterChan is closed/.test(document.querySelector('#signer-apps').textContent)")
        assert not await b.js("__sig.calls.some(c=>c[0]==='battery')")

        await b.js("document.querySelector('#signer-batt').click(); 1")
        await b.until(f"__sig.calls.some(c=>c[0]==='battery') && /find PosterChan in the list and allow it/.test({TOASTS})")
        assert await b.js("__sig.calls.filter(c=>c[0]==='battery').length") == 1
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE + ANDROID))
