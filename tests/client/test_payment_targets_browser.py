"""Exercise the shipped payment editor and chooser with signed NIP-A3 events in Chromium."""
import base64
import json
import os
import re
from pathlib import Path

import pytest
from tests.client.test_emoji_pack_tabs_layout import chrome

ROOT=Path(__file__).resolve().parents[2]


def page():
    app=Path(os.environ.get('PC_PAYMENT_TARGET_APP_SOURCE',ROOT/'static/js/client/app.js')).read_text()
    helper=app[app.index('  const _paymentTargetCache='):app.index('  // base58',app.index('  const _paymentTargetCache='))]
    def function(name):
        start=re.search(r'^  (?:async )?function '+name+r'\(',app,re.M).start()
        end=re.search(r'^  (?:async )?function ',app[start+3:],re.M)
        return app[start:start+3+end.start()]
    functions='\n'.join(function(n) for n in ('isXmrAddr','_tipMethodSheet','doTip','doZap','doXmrTip','doBchTip','_runZap'))
    functions+=app[app.index('  const _CA_CHARSET='):app.index('  const _BCH_CASHADDR=')]
    script=r'''
const $=(s,r=document)=>r.querySelector(s),$$=(s,r=document)=>[...r.querySelectorAll(s)];
const enc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const key=Uint8Array.from({length:32},(_,i)=>i===31?1:0),otherKey=Uint8Array.from({length:32},(_,i)=>i===31?2:0);
const owner=NostrTools.getPublicKey(key),other=NostrTools.getPublicKey(otherKey),xmr='8'+'a'.repeat(94);
let ME={pubkey:owner},GUEST=false,signMode='normal',holdSign=false,releaseSign=null,signCalls=0,publishes=[],publishOK=true,complete=true;
let remote=[],stored=[],lastTip=null,toastMessage='',confirmCalls=0;
const signed=(tags,created_at=100,sk=key)=>NostrTools.finalizeEvent({kind:10133,content:'',tags,created_at},sk);
const original=signed([['payto','monero',xmr],['payto','newnetwork','alice'],['custom','retain-me']]);remote=[original];
const Store={query:filters=>stored.filter(e=>e.pubkey===filters[0].authors[0]&&filters[0].kinds.includes(e.kind)),saveEvent:e=>{stored=[...stored.filter(x=>x.id!==e.id),e];},get:()=>null};
const DISCOVERY_RELAYS=['wss://discovery.example'];
const Relay={query:async()=>{const rows=remote.slice();Object.defineProperty(rows,'complete',{value:complete});return rows;},queryFrom:async()=>[],publish:async e=>{publishes.push(e);if(publishOK)remote=[e];return {ok:publishOK,msg:'Relay unavailable'};}};
const sign=async(kind,content,tags,created_at=Math.floor(Date.now()/1000))=>{signCalls++;if(signMode==='reject')throw new Error('Signer refused');const event=NostrTools.finalizeEvent({kind,content,tags:signMode==='change'?[['payto','lightning','wrong@test.test']]:tags,created_at},key);if(holdSign)await new Promise(r=>releaseSign=r);return event;};
let profile={name:'Alice'};const profOf=()=>profile,xmrOf=p=>p.monero_address||'',bchOf=()=>'',xmrForNote=()=>'';
const qrSrc=()=>'',copyValue=()=>{},toast=s=>toastMessage=s,uiConfirm=async()=>{confirmCalls++;return true;};
const ClientSettings={get:(_k,d)=>d},xmrPresets=()=>[0.00001];
window.PCMoneroWallet={tip:async opts=>{lastTip=opts;return true;}};
const _lightningAmountSheet=(_p,go)=>go(21);let lastResolved='',switchDuringLnurl=false,switchDuringInvoice=false;
const CFG={relay_url:'wss://relay.example'},lnurlResolve=async address=>{lastResolved=address;if(switchDuringLnurl)ME={pubkey:other};return {callback:'https://invoice.test/pay',allowsNostr:true};},corsJson=async()=>{if(switchDuringInvoice)ME={pubkey:other};return {pr:'fixture-invoice'};};
const Nwc={configured:()=>true,payInvoice:async()=>{lastTip={address:lastResolved};}};
const bchPresets=()=>[0.001],qrImg=()=>'',_tipTellOnDismiss=()=>({});
function modal(html,bind){document.querySelector('#modal-root').innerHTML='<div class="modal-bg"><div class="modal glass">'+html+'</div></div>';bind&&bind(document.querySelector('.modal'));}
function closeModal(){document.querySelector('#modal-root').innerHTML='';}
function editor(){modal('<details id="pf-payment-details" open><summary>Payment targets</summary><div id="pf-payment-editor"></div></details>',root=>{_bindPaymentTargetEditor(root);$('#pf-payment-details').ontoggle();});}
const settle=async()=>{for(let i=0;i<15;i++)await new Promise(r=>setTimeout(r,5));};
'''
    css=(ROOT/'static/css/client.css').read_text()
    return ('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><style>'+css+'</style><div id="modal-root"></div>'
            +''.join('<script>'+s+'</script>' for s in [(ROOT/'static/vendor/nostr/nostr.bundle.js').read_text(),
              (ROOT/'static/js/client/payment-targets.js').read_text(),script,helper,functions,'editor();']))


@pytest.mark.parametrize('width',[380,1280])
@pytest.mark.parametrize('case',['hydrate','save','clear','publish_failure','offline','account_switch','conflict','chooser','monero_tip','lightning_choice','invalid_monero','signer_refusal','signer_changed_targets','chooser_account_switch','cold_outbox','lazy_module','legacy_card','pinned_card','stored_note','bch_prefixed','lightning_account_switch','invoice_account_switch','bch_account_switch'])
def test_payment_targets_in_browser(chrome,width,case):
    target=chrome.command('Target.createTarget',{'url':'about:blank'})['targetId']
    chrome.session=chrome.command('Target.attachToTarget',{'targetId':target,'flatten':True})['sessionId']
    try:
        chrome.command('Emulation.setDeviceMetricsOverride',{'width':width,'height':850,'deviceScaleFactor':1,'mobile':width<500})
        chrome.command('Page.navigate',{'url':'data:text/html;base64,'+base64.b64encode(page().encode()).decode()})
        chrome.evaluate('settle()')
        assert chrome.evaluate("$$('.pt-edit-row').length")==2
        if case=='hydrate':
            assert chrome.evaluate("$$('.pt-type').map(e=>e.value)")==['monero','newnetwork']
            assert chrome.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
            assert chrome.evaluate('signCalls')==0
        elif case in ('save','publish_failure','account_switch','conflict'):
            chrome.evaluate("$$('.pt-address')[1].value='new-alice'")
            if case=='publish_failure':chrome.evaluate('publishOK=false')
            if case=='account_switch':chrome.evaluate('holdSign=true')
            if case=='conflict':chrome.evaluate("remote=[signed([['payto','monero',xmr],['payto','newnetwork','elsewhere']],102)]")
            chrome.click('#pt-save');chrome.evaluate('settle()')
            if case=='account_switch':chrome.evaluate('ME={pubkey:other};releaseSign();settle()')
            if case=='save':
                assert chrome.evaluate('publishes.length')==1
                assert chrome.evaluate('NostrTools.verifyEvent(publishes[0])')
                assert ['payto','newnetwork','new-alice'] in chrome.evaluate('publishes[0].tags')
                assert ['custom','retain-me'] in chrome.evaluate('publishes[0].tags')
                chrome.evaluate('editor();settle()')
                assert chrome.evaluate("$$('.pt-address')[1].value")=='new-alice'
            elif case=='publish_failure':
                assert 'Relay unavailable' in chrome.evaluate("$('#pt-status').textContent")
                assert chrome.evaluate("$$('.pt-address')[1].value")=='new-alice'
                assert chrome.evaluate('_paymentTargetCache.get(owner).event.id===original.id')
            else:
                assert chrome.evaluate('publishes.length')==0
                if case=='conflict':assert 'another device' in chrome.evaluate("$('#pt-status').textContent")
        elif case=='lazy_module':
            chrome.evaluate("window.savedPaymentModule=PCPaymentTargets;delete window.PCPaymentTargets;window._withModule=async()=>{window.PCPaymentTargets=savedPaymentModule;return savedPaymentModule;};editor();settle()")
            assert chrome.evaluate("$$('.pt-edit-row').length")==2
        elif case=='cold_outbox':
            chrome.evaluate("remote=[NostrTools.finalizeEvent({kind:10002,content:'',tags:[['r','wss://alice-own.example','write']],created_at:100},key)];stored=[];_paymentTargetCache.clear();window.outboxAsked=false;Relay.queryFrom=async urls=>{if(urls.includes('wss://alice-own.example')){outboxAsked=true;return [original];}return [];};editor();settle()")
            assert chrome.evaluate('outboxAsked')
            assert chrome.evaluate("$$('.pt-type').map(e=>e.value)")==['monero','newnetwork']
        elif case in ('signer_refusal','signer_changed_targets'):
            chrome.evaluate("signMode="+json.dumps('reject' if case=='signer_refusal' else 'change'))
            chrome.click('#pt-save');chrome.evaluate('settle()')
            assert chrome.evaluate('publishes.length')==0
            assert chrome.evaluate("$$('.pt-edit-row').length")==2
            assert not chrome.evaluate("$('#pt-save').disabled")
            assert ('Signer refused' if case=='signer_refusal' else 'different payment targets') in chrome.evaluate("$('#pt-status').textContent")
        elif case=='chooser_account_switch':
            chrome.evaluate('showPaymentTargets(owner);settle()');chrome.evaluate('ME={pubkey:other}')
            chrome.click('[data-pay-target="0"]');chrome.evaluate('settle()')
            assert chrome.evaluate('lastTip') is None
        elif case=='invalid_monero':
            chrome.evaluate("remote=[signed([['payto','monero',xmr],['payto','monero','not-an-address']],103)];_paymentResolver().load(owner,{force:true}).then(()=>showPaymentTargets(owner));settle()")
            chrome.click('[data-pay-target="1"]');chrome.evaluate('settle()')
            assert chrome.evaluate('lastTip') is None
            assert 'not a valid address' in chrome.evaluate('toastMessage')
        elif case=='clear':
            chrome.evaluate("$$('.pt-remove').forEach(b=>b.click())")
            chrome.click('#pt-save');chrome.evaluate('settle()')
            assert chrome.evaluate('publishes.length')==1
            assert chrome.evaluate('PCPaymentTargets.parse(publishes[0]).length')==0
            assert chrome.evaluate('confirmCalls')==1
            chrome.evaluate('editor();settle()');assert chrome.evaluate("$$('.pt-edit-row').length")==0
        elif case=='offline':
            chrome.evaluate("remote=[];complete=false;$$('.pt-address')[1].value='keep-this-edit'")
            chrome.click('#pt-save');chrome.evaluate('settle()')
            assert chrome.evaluate('signCalls')==0
            assert 'did not answer' in chrome.evaluate("$('#pt-status').textContent")
            assert chrome.evaluate("$$('.pt-address')[1].value")=='keep-this-edit'
        elif case=='chooser':
            chrome.evaluate('showPaymentTargets(owner);settle()')
            assert chrome.evaluate("$$('[data-pay-target]').length")==2
            chrome.click('[data-pay-target="1"]');chrome.evaluate('settle()')
            assert chrome.evaluate("$('.modal a').getAttribute('href')")=='payto://newnetwork/alice'
        elif case in ('legacy_card','pinned_card','stored_note'):
            chrome.evaluate("profile.monero_address='8'+'b'.repeat(94)")
            if case=='stored_note':
                chrome.evaluate("Store.get=()=>({tags:[['monero_address',profile.monero_address]]})")
            chrome.evaluate('doTip("note",owner,profile.monero_address,'+('true' if case=='pinned_card' else 'false')+');settle()')
            chrome.click('[data-m="xmr"]');chrome.evaluate('settle()')
            assert chrome.evaluate('lastTip.address')==chrome.evaluate('xmr' if case=='legacy_card' else 'profile.monero_address')
            chrome.evaluate('lastTip=null;doXmrTip("note",owner,profile.monero_address,'+('true' if case=='pinned_card' else 'false')+');settle()')
            assert chrome.evaluate('lastTip.address')==chrome.evaluate('xmr' if case=='legacy_card' else 'profile.monero_address')
        elif case=='monero_tip':
            chrome.evaluate('doTip(null,owner);settle()')
            chrome.click('[data-m="xmr"]');chrome.evaluate('settle()')
            assert chrome.evaluate('lastTip.address===xmr')
            assert chrome.evaluate('lastTip.pubkey')==chrome.evaluate('owner')
        elif case=='bch_prefixed':
            chrome.evaluate("remote=[signed([['payto','bitcoincash','bitcoincash:qpm2qsznhks23z7629mms6s4cwef74vcwvy22gdx6a']],103)];_paymentResolver().load(owner,{force:true}).then(()=>showPaymentTargets(owner));settle()")
            chrome.click('[data-pay-target="0"]');chrome.evaluate('settle()')
            assert chrome.evaluate("$('#bch-open').getAttribute('href')")=='bitcoincash:qpm2qsznhks23z7629mms6s4cwef74vcwvy22gdx6a'
            chrome.evaluate("$('#bch-amt').value='0.001';$('#bch-amt').dispatchEvent(new Event('input'))")
            assert chrome.evaluate("$('#bch-open').getAttribute('href')").endswith('?amount=0.001')
            assert chrome.evaluate("$('#bch-addr').textContent")=='qpm2qsznhks23z7629mms6s4cwef74vcwvy22gdx6a'
        elif case in ('lightning_account_switch','invoice_account_switch'):
            chrome.evaluate(('switchDuringLnurl' if case=='lightning_account_switch' else 'switchDuringInvoice')+'=true;_runZap(null,owner,21,"alice@test.test");settle()')
            assert chrome.evaluate('lastTip') is None
            if case=='lightning_account_switch':assert chrome.evaluate('signCalls')==0
        elif case=='bch_account_switch':
            chrome.evaluate("window.releaseDiscovery=null;Relay.query=()=>new Promise(r=>releaseDiscovery=r);_paymentTargetCache.clear();doBchTip(owner);settle()")
            chrome.evaluate('ME={pubkey:other};releaseDiscovery([]);settle()')
            assert chrome.evaluate("$('#bch-open')===null")
        elif case=='lightning_choice':
            chrome.evaluate("remote=[signed([['payto','lightning','one@test.test'],['payto','lightning','two@test.test']],103)];_paymentResolver().load(owner,{force:true}).then(()=>showPaymentTargets(owner));settle()")
            chrome.click('[data-pay-target="1"]');chrome.evaluate('settle()')
            assert chrome.evaluate('lastTip') is not None,chrome.evaluate('toastMessage')
            assert chrome.evaluate('lastTip.address')=='two@test.test'
    finally:
        chrome.session=None;chrome.command('Target.closeTarget',{'targetId':target})
