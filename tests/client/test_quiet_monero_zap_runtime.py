"""Actual wallet + app tip handlers; every payment and public publish is intercepted."""
import json
import subprocess
from pathlib import Path
import pytest
from tests.client.test_monero_wallet_send_flow import BOOT, STAGENET
from tests.client.test_tip_tell_on_dismiss import _fn

ROOT=Path(__file__).resolve().parents[2]
APP=(ROOT/'static/js/client/app.js').read_text()
APP_HANDLERS='\n'.join(_fn(APP,name,opener) for name,opener in [
    ('_tipTellOnDismiss','function _tipTellOnDismiss(root, opts){'),
    ('doXmrTip','async function doXmrTip('),
    ('_postXmrTipNote','async function _postXmrTipNote(')])
SETUP=r'''
const assert=require('node:assert/strict'),ADDR='5'+'A'.repeat(94);
let ME={pubkey:'a'.repeat(64)};const GUEST=false,_TIP_DWELL_MS=10000;
const profOf=()=>({name:'recipient'}),Store={get:()=>null},isXmrAddr=s=>s===ADDR;
const xmrOf=()=>ADDR,_notePaymentXmr=()=>'',_paymentAddress=async()=>ADDR;
const enc=String,qrImg=()=>'<img alt="fixture QR">',xmrPresets=()=>[],_xmrFeePct=()=>0;
const _prefTouched=new Set(),prefsLocal=[],prefsNostr=[];
const ClientSettings={get:()=>'',set(k,v){prefsLocal.push([k,String(v)])}};
const saveClientPrefsNostr=patch=>{prefsNostr.push(patch)};
const publicPosts=[];const publish=async(kind,content,tags)=>{publicPosts.push({kind,content,tags});return{ok:true}};
const NT=()=>({nip19:{npubEncode:pk=>'npub-fixture-'+pk}});
const toast=window.__PC.toast,modal=window.__PC.modal;
const $=(selector,root)=>(root||document).querySelector(selector),$$=(selector,root)=>(root||document).querySelectorAll(selector);
const observers=[];globalThis.MutationObserver=class{constructor(fn){this.fn=fn;observers.push(this)}observe(){}disconnect(){this.closed=true}};
const realElement=document.getElementById;document.getElementById=id=>id==='modal-root'?{}:realElement(id);
const closeModal=()=>{closed++;const r=modals.at(-1);if(r)r.isConnected=false;for(const o of observers)if(!o.closed)o.fn()};
window.__PC.closeModal=closeModal;
let questions=0,answer=true;const uiConfirm=async()=>{questions++;return answer};
const copyValue=()=>{},_withModule=async()=>null;
const tick=()=>new Promise(r=>setImmediate(r)),settleDismiss=()=>new Promise(r=>setTimeout(r,110));
replies['/api/wallet/xmr/me/status']={enabled:true,network:'stagenet',fee_percent:'2'};
replies['/api/wallet/xmr/me/balance']={address:ADDR,balance:'1',unlocked_balance:'1'};
replies['/api/wallet/xmr/me/pay']={tx_hash_list:['b'.repeat(64)],amount:'0.01'};
const originalFetch=globalThis.fetch;let paymentMode='ok',releasePayment;
globalThis.fetch=async(url,opts)=>{
 const path=String(url).split('?')[0];
 if(['/api/wallet/xmr/transfer/confirm','/api/wallet/xmr/me/pay'].includes(path)){
  if(paymentMode==='hold'){const response=await originalFetch(url,opts);await new Promise(r=>releasePayment=r);return response;}
  if(paymentMode==='unknown'){requests.push({key:path,method:opts.method,body:JSON.parse(opts.body)});throw Object.assign(new Error('Aborted while awaiting response'),{name:'AbortError'});}
  if(paymentMode==='reject'){requests.push({key:path,method:opts.method,body:JSON.parse(opts.body)});return new Response(JSON.stringify({detail:'Amount must be positive'}),{status:400});}
 }
 return originalFetch(url,opts);
};
const payments=()=>requests.filter(r=>['/api/wallet/xmr/transfer/confirm','/api/wallet/xmr/me/pay'].includes(r.key));
async function openRoute(route,quiet){
 if(route==='user')window.PCMoneroWallet.tip=async()=>false;
 if(route==='external')window.PCMoneroWallet={tip:async()=>false,meTip:async()=>false};
 await doXmrTip('note-id','b'.repeat(64),ADDR);
 let sheet=modals.at(-1);assert(sheet,'real route opens a modal');
 if(route==='node'){
  sheet.querySelector('#mw-to').value=ADDR;sheet.querySelector('#mw-amount').value='0.01';sheet.querySelector('#mw-note').value='';
  sheet.querySelector('#mw-review').onclick();sheet=modals.at(-1);
  const check=sheet.querySelector('#mw-understand');check.checked=true;check.onchange();
 }else if(route==='user')sheet.querySelector('#mw-me-amt').value='0.01';else sheet.querySelector('#xmr-amt').value='0.01';
 assert.match(sheet.html,/Do not post this zap/);
 const choice=sheet.querySelector(route==='external'?'#xmr-quiet-zap':'#mw-quiet-zap');choice.checked=quiet;
 const button=sheet.querySelector(route==='node'?'#mw-confirm':route==='user'?'#mw-me-send':'#xmr-sent');
 return{sheet,choice,button};
}
'''


def run(script):
    result=subprocess.run(['node','-e',BOOT+SETUP+APP_HANDLERS+'\n(async()=>{'+script+'\ndone({ok:true});})().catch(e=>{console.error(e);process.exit(1);});'],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout)['ok']


@pytest.mark.parametrize('route',['node','user','external'])
@pytest.mark.parametrize('quiet',[False,True])
def test_all_three_routes_preserve_payment_and_honor_announcement_choice(route,quiet):
    run(f'''const route={json.dumps(route)},quiet={json.dumps(quiet)};
    const {{button}}=await openRoute(route,quiet);await button.onclick();await settleDismiss();
    assert.equal(payments().length,route==='external'?0:1);
    assert.equal(publicPosts.length,quiet?0:1);assert.equal(questions,0);
    if(publicPosts.length){{assert.equal(publicPosts[0].kind,1);assert.match(publicPosts[0].content,/Tipped 0.01 XMR/);}}
    if(route!=='external')assert(toasts.some(t=>/sent/.test(t)),JSON.stringify(toasts));''')


@pytest.mark.parametrize('route',['node','user'])
def test_pending_or_ambiguous_payment_never_announces_or_resends(route):
    run(f'''const route={json.dumps(route)};const {{sheet,choice,button}}=await openRoute(route,true);
    paymentMode='hold';const pending=button.onclick();await tick();assert(releasePayment);assert(choice.disabled);
    await button.onclick();assert.equal(payments().length,1,'pending double invocation cannot pay twice');
    if(route==='node'){{const check=sheet.querySelector('#mw-understand');check.checked=false;check.onchange();check.checked=true;check.onchange();assert(button.disabled);}}
    releasePayment();await pending;assert.equal(publicPosts.length,0);
    ''')


@pytest.mark.parametrize('route',['node','user'])
def test_ambiguous_payment_keeps_status_and_cannot_retry(route):
    run(f'''const route={json.dumps(route)};const {{sheet,choice,button}}=await openRoute(route,true);
    paymentMode='unknown';await button.onclick();assert.equal(payments().length,1);assert(button.disabled);assert(choice.disabled);
    assert.match(button.textContent,/history/i);assert.equal(publicPosts.length,0);
    if(route==='node'){{const check=sheet.querySelector('#mw-understand');check.checked=false;check.onchange();check.checked=true;check.onchange();assert(button.disabled);}}
    await button.onclick();assert.equal(payments().length,1);''')


@pytest.mark.parametrize('route',['node','user'])
def test_known_rejection_allows_explicit_retry_with_changed_choice(route):
    run(f'''const {{choice,button}}=await openRoute({json.dumps(route)},true);paymentMode='reject';await button.onclick();
    assert(!button.disabled);assert(!choice.disabled);assert.equal(publicPosts.length,0);
    choice.checked=false;paymentMode='ok';await button.onclick();assert.equal(payments().length,2);assert.equal(publicPosts.length,1);''')


@pytest.mark.parametrize('route',['node','user'])
def test_success_after_account_switch_never_announces_under_new_account(route):
    run(f'''const {{button}}=await openRoute({json.dumps(route)},false);paymentMode='hold';const pending=button.onclick();await tick();
    ME={{pubkey:'c'.repeat(64)}};releasePayment();await pending;assert.equal(payments().length,1);assert.equal(publicPosts.length,0);''')


@pytest.mark.parametrize('quiet',[False,True])
def test_external_dismiss_only_prompts_when_announcement_is_allowed(quiet):
    run(f'''const {{sheet}}=await openRoute('external',{json.dumps(quiet)});sheet.querySelector('#xmr-copy').onclick();closeModal();await settleDismiss();
    assert.equal(payments().length,0);assert.equal(questions,{0 if quiet else 1});assert.equal(publicPosts.length,{0 if quiet else 1});''')


def test_external_account_switch_suppresses_dismiss_prompt_and_post():
    run('''const {sheet}=await openRoute('external',false);sheet.querySelector('#xmr-copy').onclick();ME={pubkey:'c'.repeat(64)};closeModal();await settleDismiss();
    assert.equal(questions,0);assert.equal(publicPosts.length,0);assert.equal(payments().length,0);''')


def test_withdrawal_unknown_preserves_existing_status_without_tip_checkbox():
    run('''const nodes=new Map();document.getElementById=id=>id==='feed'?feed:(nodes.get(id)||((nodes.set(id,el())),nodes.get(id)));
    window.__PC.VIEW='wallet';failures['/api/wallet/xmr/status']={status:403,detail:'operator only'};
    await window.PCMoneroWallet.render(true);assert.match(feed.innerHTML,/mw-me-withdraw/);
    nodes.get('mw-me-withdraw').onclick();const sheet=modals.at(-1),button=sheet.querySelector('#mw-wd-go');sheet.querySelector('#mw-wd-to').value=ADDR;
    assert(!sheet.html.includes('quiet-zap'));
    const underlying=globalThis.fetch;let sweeps=0;globalThis.fetch=async(url,opts)=>{if(String(url).startsWith('/api/wallet/xmr/me/withdraw')){sweeps++;throw Object.assign(new Error('Aborted while awaiting response'),{name:'AbortError'})}return underlying(url,opts)};
    await button.onclick();assert.equal(sweeps,1);assert(button.disabled);assert.match(button.textContent,/history/i);
    assert(toasts.some(t=>/may have been sent|did not answer in time/i.test(t)),JSON.stringify(toasts));assert.equal(publicPosts.length,0);''')


def test_external_repeated_confirmation_posts_only_once():
    run('''const {sheet,button}=await openRoute('external',false);sheet.querySelector('#xmr-amt').value='';
    sheet.querySelector('#xmr-txid').value='d'.repeat(64);let release;answer=new Promise(r=>release=r);
    const first=button.onclick(),second=button.onclick();await tick();assert.equal(questions,2);
    release(true);await Promise.all([first,second]);await settleDismiss();
    assert.equal(publicPosts.length,1);assert.equal(payments().length,0);''')


def test_external_account_change_during_confirmation_does_not_post():
    run('''const {sheet,button}=await openRoute('external',false);sheet.querySelector('#xmr-amt').value='';
    sheet.querySelector('#xmr-txid').value='d'.repeat(64);let release;answer=new Promise(r=>release=r);
    const pending=button.onclick();await tick();assert.equal(questions,1);ME={pubkey:'c'.repeat(64)};
    release(true);await pending;assert.equal(publicPosts.length,0);assert.equal(payments().length,0);''')


@pytest.mark.parametrize('route',['node','user'])
@pytest.mark.parametrize('quiet',[False,True])
def test_forcing_the_choice_over_mid_flight_changes_nothing_and_pays_once(route,quiet):
    """THE INVARIANT THE TASK IS ABOUT. The announcement choice is read ONCE, before the money
    request goes out, and the send is already locked by then. So a choice moved while the transfer
    is in the air — a disabled box is still settable from script, and an ambiguous send leaves the
    sheet on screen for as long as somebody stares at it — decides nothing and, above all, cannot
    re-enter the send. Read after the await instead and BOTH directions break: a quiet zap
    announces, and a loud one goes silent."""
    run(f'''const {{choice,button}}=await openRoute({json.dumps(route)},{json.dumps(quiet)});
    paymentMode='hold';const pending=button.onclick();await tick();assert(releasePayment);
    choice.checked=!choice.checked;                       // the user changes their mind mid-transfer
    releasePayment();await pending;await settleDismiss();
    assert.equal(payments().length,1,'a changed choice re-entered the send');
    assert.equal(publicPosts.length,{0 if quiet else 1},'the choice captured at the press is what counts');''')


@pytest.mark.parametrize('route',['node','user','external'])
def test_a_quiet_zap_still_remembers_the_amount_it_sent(route):
    """THE CHECKBOX SUPPRESSES THE POST AND NOTHING ELSE. `xmrLastAmt` is the payer's own record on
    the non-custodial route — there is no wallet here to hold a transaction — and it is what
    pre-fills the next tip on every route. Skipping it under "do not post" made the amount box
    depend on a privacy choice: tip 0.01 quietly and the next sheet has forgotten it, while the same
    0.01 through either wallet is remembered."""
    run(f'''const {{button}}=await openRoute({json.dumps(route)},true);await button.onclick();await settleDismiss();
    assert.equal(publicPosts.length,0,'a quiet zap posted');
    assert(prefsLocal.some(([k,v])=>k==='xmrLastAmt'&&v==='0.01'),'the amount was not remembered: '+JSON.stringify(prefsLocal));
    assert(prefsNostr.some(p=>p&&p.xmrTip==='0.01'),'the amount did not sync: '+JSON.stringify(prefsNostr));''')


def test_a_plain_wallet_send_is_not_a_zap_and_offers_no_announcement_choice():
    """The wallet screen's own Send opens the SAME confirm dialog with no `onSent` — there is no
    recipient to credit and nothing would ever be published, so an announcement checkbox there is a
    control that does nothing. `tipPostChoice` is keyed on the tip callback for exactly that."""
    run('''assert.equal(await window.PCMoneroWallet.openSend(),true);
    const sheet=modals.at(-1);assert(sheet,'the send sheet opened');
    sheet.querySelector('#mw-to').value=ADDR;sheet.querySelector('#mw-amount').value='0.01';
    sheet.querySelector('#mw-review').onclick();const confirm=modals.at(-1);
    assert.match(confirm.html,/cannot be reversed/,'this is the confirm dialog');
    assert(!/quiet-zap|Do not post this zap/.test(confirm.html),'a plain send offered a zap choice');
    assert.equal(publicPosts.length,0);''')
