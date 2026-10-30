"""ETHEREUM ON PROFILES: SET IT IN EDIT PROFILE, TIP IT FROM SOMEBODY ELSE'S.

"Can you add support for ethereum zaps in users profile? make sure edit profile works for it" (2026-10-30). The
shipped client against a stub relay that records what it is sent:
  * Edit Profile has an Ethereum field; an invalid address is refused with the sheet left open and nothing published;
  * a valid one is published as `ethereum` and in `cryptocurrency_addresses` -- and a BCH alias another client wrote
    (`bch_address`) is left alone;
  * clearing it removes every Ethereum alias;
  * on another person's profile the Ξ chip and the Tip button open the pay sheet with an `ethereum:` wallet link.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
ADDR = '0x52908400098527886E0F7030069857D2E4169EE7'
OTHER_ADDR = '0xde709f2102306220921060314715629080e2fb77'

INIT = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse("
        "localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));") + r'''
window.__published = [];
const __SK2 = new Uint8Array(32).fill(2);
function __other(){
  const T = window.NostrTools, pk = T.getPublicKey(__SK2), now = Math.floor(Date.now()/1000);
  window.__OTHER = pk;
  return { pk, k0: T.finalizeEvent({ kind:0, created_at:now-60, tags:[], content:JSON.stringify({ name:'Eth Person', ethereum:'%OTHER%' }) }, __SK2) };
}
function __mine(pk){ const T=window.NostrTools; return T.finalizeEvent({ kind:0, created_at:Math.floor(Date.now()/1000)-3600, tags:[],
  content:JSON.stringify({ name:'Me', bch_address:'qpm2qsznhks23z7629mms6s4cwef74vcwvy22gdx6a' }) }, new Uint8Array(32).fill(1)); }
class FS{constructor(u){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},20);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;}
  const out=o=>{if(this.readyState===1&&this.onmessage)this.onmessage({data:JSON.stringify(o)});};
  if(m[0]==='EVENT'){ window.__published.push(m[1]); out(['OK', m[1].id, true, '']); return; }
  if(m[0]!=='REQ') return; const id=m[1];
  let O=null; try{ O=__other(); }catch(_){}
  for(const f of m.slice(2)){
    if(O && (f.authors||[]).includes(O.pk) && (f.kinds||[]).includes(0)) out(['EVENT',id,O.k0]);
    try{ const mine=__mine(); if((f.authors||[]).includes(mine.pubkey) && (f.kinds||[]).includes(0) && !window.__published.some(e=>e.kind===0)) out(['EVENT',id,mine]); }catch(_){}
    for(const e of window.__published) if((f.kinds||[]).includes(e.kind) && (f.authors||[]).includes(e.pubkey)) out(['EVENT',id,e]);
  }
  out(['EOSE',id]); }
 close(){this.readyState=3;} }
FS.OPEN=1;FS.CONNECTING=0;FS.CLOSED=3;window.WebSocket=FS;
'''.replace('%OTHER%', OTHER_ADDR)

LAST_K0 = "(()=>{const k=window.__published.filter(e=>e.kind===0).pop();return k?JSON.parse(k.content):null})()"


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _edit(b, value):
    # this device already holds its own profile (as a signed-in device does): the cache-first paint, not the cold path
    await b.js("(()=>{const k=window.__published.filter(e=>e.kind===0).pop()||__mine(); Store.saveProfile(k); return true})()")
    await b.js("__PC.openProfile(__PC.me().pubkey); true")
    await b.until("!!document.querySelector('#edit-prof')")
    await b.js("document.querySelector('#edit-prof').click(); true")
    await b.until("!!document.querySelector('#pf-eth')")
    await b.js("(()=>{const i=document.querySelector('#pf-eth');i.value=%s;i.dispatchEvent(new Event('input',{bubbles:true}));return true})()" % json.dumps(value))
    n = await b.js("window.__published.filter(e=>e.kind===0).length")
    await b.js("document.querySelector('#pf-save').click(); true")
    return n


@CHROME
def test_ethereum_is_set_in_edit_profile_and_tipped_from_a_profile():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}; true")
        await asyncio.sleep(1.5)
        # an invalid address: refused, the sheet stays, nothing published
        n = await _edit(b, '0x1234')
        await asyncio.sleep(1)
        got['bad'] = {'published': await b.js("window.__published.filter(e=>e.kind===0).length") - n,
                      'open': await b.js("!!document.querySelector('#pf-eth')")}
        await b.js("try{ __PC.closeModal ? __PC.closeModal() : document.querySelector('.modal .x, .modal-close')?.click(); }catch(_){}; true")
        await asyncio.sleep(.5)
        # a valid one
        await _edit(b, 'ethereum:' + ADDR)
        await b.until("window.__published.some(e=>e.kind===0)")
        await asyncio.sleep(.5)
        got['set'] = await b.js(LAST_K0)
        await asyncio.sleep(1)
        # cleared
        await _edit(b, '')
        await b.until("window.__published.filter(e=>e.kind===0).length>=2")
        await asyncio.sleep(.5)
        got['clear'] = await b.js(LAST_K0)
        # somebody else's profile: chip + Tip -> pay sheet with an ethereum: link
        await b.js("try{ Store.saveProfile(__other().k0); }catch(_){}; true")
        await b.until("!!window.__OTHER")
        await b.js("__PC.openProfile(window.__OTHER); true")
        await b.until("!!document.querySelector('#ethtip-prof') && !!document.querySelector('#prof-eth')")
        await b.js("document.querySelector('#ethtip-prof').click(); true")
        await b.until("!!document.querySelector('.modal a[href^=\"ethereum:\"]')")
        got['sheet'] = await b.js("({href:document.querySelector('.modal a[href^=\"ethereum:\"]').getAttribute('href'), text:document.querySelector('.modal').textContent})")

    asyncio.run(desktop.with_browser('online', '', check, INIT))
    assert got['bad'] == {'published': 0, 'open': True}, ('an invalid address was saved or the sheet closed', got['bad'])
    s = got['set']
    assert s['ethereum'] == ADDR and s.get('cryptocurrency_addresses', {}).get('ethereum') == ADDR, s
    assert s.get('bch_address') == 'qpm2qsznhks23z7629mms6s4cwef74vcwvy22gdx6a', ("another client's BCH alias was touched", s)
    c = got['clear']
    assert not any(k in c for k in ('ethereum', 'eth', 'ethereum_address', 'eth_address')), c
    assert 'ethereum' not in (c.get('cryptocurrency_addresses') or {}), c
    assert got['sheet']['href'].lower().startswith('ethereum:' + OTHER_ADDR.lower()), got['sheet']
    assert OTHER_ADDR.lower() in got['sheet']['text'].lower(), got['sheet']
