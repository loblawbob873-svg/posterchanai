"""Tipping a post with Lightning, pressed in the SHIPPED bundle: the post's ⚡ → a custom amount → "Zap" resolves the
author's lightning address, signs a NIP-57 zap request for exactly that amount, and (no WebLN, no wallet connected)
shows the invoice -- whose Copy copies exactly the invoice. The LNURL server is stubbed; everything else is real."""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


INVOICE = "lnbc210n1pfixtureinvoice0000000000000000000000000000000"

FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__copied=[];window.__lnurl=[];
try{Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async t=>{__copied.push(String(t));}}});}catch(_){}
const __zf=window.fetch;
window.fetch=(u,o)=>{const s=decodeURIComponent(String(u));const json=v=>Promise.resolve(new Response(JSON.stringify(v),{status:200,headers:{'Content-Type':'application/json'}}));
  if(s.includes('ln.example/.well-known/lnurlp/bob'))return json({callback:'https://ln.example/cb',allowsNostr:true,nostrPubkey:'a'.repeat(64),minSendable:1000,maxSendable:1e9,tag:'payRequest'});
  if(s.includes('ln.example/cb')){__lnurl.push(s);return json({pr:'INVOICE'});}
  return __zf(u,o);};
'''.replace('INVOICE', INVOICE)

SETUP = r'''(()=>{const sk=new Uint8Array(32).fill(7),now=Math.floor(Date.now()/1000);
  const prof=NostrTools.finalizeEvent({kind:0,created_at:now-60,tags:[],content:JSON.stringify({name:'Bob',lud16:'bob@ln.example'})},sk);
  const ev=NostrTools.finalizeEvent({kind:1,created_at:now-30,content:'tip me',tags:[]},sk);
  Store.saveProfile(prof);Store.saveEvent(ev);window.__post=ev.id;window.__author=NostrTools.getPublicKey(sk);
  __PC.openNote(ev.id);return 1;})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_custom_amount_zap_shows_the_invoice_and_copies_it():
    async def check(b):
        await desktop.login(b)
        await b.js(SETUP)
        tip = "article.note[data-id=\"'+__post+'\"] [data-a=tip]"
        await b.until(f"!!document.querySelector('{tip}')")
        await b.js(f"document.querySelector('{tip}').click(); 1")
        await b.until("!!document.querySelector('#zap-go')")
        await b.js("document.querySelector('#zap-custom').value='21'; document.querySelector('#zap-go').click(); 1")
        await b.until("!!document.querySelector('#z-copy')")
        assert await b.js("document.querySelector('#z-inv').textContent") == INVOICE
        req = await b.js("__lnurl[0]")
        assert "amount=21000" in req, req
        zr = json.loads(req.split("nostr=", 1)[1])
        assert zr["kind"] == 9734 and ["amount", "21000"] in zr["tags"], zr
        assert ["p", await b.js("__author")] in zr["tags"] and ["e", await b.js("__post")] in zr["tags"], zr["tags"]
        await b.js("document.querySelector('#z-copy').click(); 1")
        await b.until(f"__copied.includes({json.dumps(INVOICE)})")
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))


BCH = "qpm2qsznhks23z7629mms6s4cwef74vcwvy22gdx6a"     # the CashAddr spec's own example address


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_bitcoin_cash_tip_offers_the_address_and_copies_it():
    """An author whose only payment route is Bitcoin Cash: ⚡ opens the BCH sheet straight away, and Copy copies
    exactly that address (no bitcoincash: prefix to trip a wallet's paste)."""
    async def check(b):
        await desktop.login(b)
        await b.js(SETUP.replace("lud16:'bob@ln.example'", "bch:'bitcoincash:%s'" % BCH))
        tip = "article.note[data-id=\"'+__post+'\"] [data-a=tip]"
        await b.until(f"!!document.querySelector('{tip}')")
        await b.js(f"document.querySelector('{tip}').click(); 1")
        await b.until("!!document.querySelector('#bch-copy')")
        await b.js("document.querySelector('#bch-copy').click(); 1")
        await b.until(f"__copied.includes({json.dumps(BCH)})")
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
