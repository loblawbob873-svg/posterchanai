"""Drive the shipped per-user Send UI; every network request is stubbed (no funds)."""
import json
import subprocess

import pytest

from tests.client.test_monero_wallet_send_flow import BOOT, MAINNET

SETUP = r'''
const assert=require('node:assert/strict');
const nodes=new Map();
document.getElementById=id=>id==='feed'?feed:(nodes.get(id)||((nodes.set(id,el())),nodes.get(id)));
window.__PC.VIEW='wallet';
failures['/api/wallet/xmr/status']={status:403,detail:'operator only'};
replies['/api/wallet/xmr/me/status']={enabled:true,network:'mainnet',fee_percent:'2'};
replies['/api/wallet/xmr/me/balance']={address:'4'+'A'.repeat(94),balance:'2',unlocked_balance:'1'};
const pay='/api/wallet/xmr/me/pay';
const receipt={tx_hash_list:['a'.repeat(64)],amount:'0.1',fee:'0.0001',service_fee:'0.002'};
const normalFetch=globalThis.fetch;
let mode='ok',release,posts=[];
globalThis.fetch=async (url,opts)=>{
 if(url!==pay)return normalFetch(url,opts);
 posts.push(JSON.parse(opts.body));
 if(mode==='hold')return new Promise(resolve=>release=()=>resolve(new Response(JSON.stringify(receipt))));
 if(mode==='disconnect')throw new TypeError('Failed to fetch');
 if(mode==='malformed')return new Response('<html>broken proxy</html>',{status:200});
 if(mode==='empty')return new Response('{}',{status:200});
 if(mode==='refusal')return new Response(JSON.stringify({detail:'Amount must be positive with at most 12 decimal places'}),{status:400});
 if(mode==='502'||mode==='504')return new Response('Bad gateway',{status:Number(mode)});
 return new Response(JSON.stringify(receipt),{status:200});
};
async function openUserSend(raw='0.1',address='4'+'A'.repeat(94)){
 await window.PCMoneroWallet.render();
 assert.match(feed.innerHTML,/mw-me-send/);
 nodes.get('mw-me-send').onclick();
 const sheet=modals.at(-1);
 sheet.querySelector('#mw-ms-to').value=address;
 sheet.querySelector('#mw-ms-amount').value=raw;
 sheet.querySelector('#mw-ms-review').onclick();
 const confirm=modals.at(-1),box=confirm.querySelector('#mw-ms-understand'),go=confirm.querySelector('#mw-ms-go');
 // Existing DOM stub does not parse initial HTML boolean attributes.
 go.disabled=/<button[^>]*id="mw-ms-go"[^>]*\bdisabled\b/.test(confirm.html);
 return {sheet,confirm,box,go};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function check(box,value){box.checked=value;box.onchange();}
function click(go){if(!go.disabled)return go.onclick();}
'''


def run(script):
    result = subprocess.run(['node', '-e', BOOT + SETUP + '\n(async()=>{'+script+'\ndone({ok:true});})().catch(e=>{console.error(e);process.exitCode=1;});'],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-4500:]
    assert json.loads(result.stdout)['ok']


def test_pending_checkbox_cannot_resubmit_payment():
    run('''const {box,go}=await openUserSend();mode='hold';
    assert(go.disabled);check(box,true);const job=click(go);await tick();
    assert.equal(posts.length,1);check(box,false);check(box,true);
    assert(go.disabled,'checkbox reopened pending payment');await click(go);
    assert.equal(posts.length,1);release();await job;''')


@pytest.mark.parametrize('mode',['disconnect','502','504','malformed','empty'])
def test_uncertain_response_is_terminal_even_after_checkbox_toggle(mode):
    run('''const {box,go}=await openUserSend();mode=%s;check(box,true);await click(go);
    assert.equal(posts.length,1);assert(go.disabled);assert.match(go.textContent,/history/i);
    assert(!toasts.some(t=>/payment not sent|payment sent$/i.test(t)),JSON.stringify(toasts));
    check(box,false);check(box,true);assert(go.disabled,'checkbox reopened uncertain payment');
    await click(go);assert.equal(posts.length,1);''' % json.dumps(mode))


@pytest.mark.parametrize('raw',['0.123456789012','0.000000000001'])
def test_valid_receipt_success_and_exact_decimal_payload(raw):
    run('''const raw=%s;const {box,go}=await openUserSend(raw);
    check(box,true);await click(go);assert.equal(posts.length,1);
    assert.equal(posts[0].payments[0].amount,raw);assert.equal(posts[0].payments[0].address,'4'+'A'.repeat(94));
    assert(toasts.some(t=>/payment sent/i.test(t)),JSON.stringify(toasts));''' % json.dumps(raw))


def test_explicit_validation_refusal_can_retry_once_corrected():
    run('''const {box,go}=await openUserSend();mode='refusal';check(box,true);await click(go);
    assert.equal(posts.length,1);assert(!go.disabled);assert(toasts.some(t=>/not sent/i.test(t)));
    mode='ok';await click(go);assert.equal(posts.length,2);assert(toasts.some(t=>/payment sent/i.test(t)));''')


@pytest.mark.parametrize('raw,address',[('0',MAINNET),('-1',MAINNET),('0.0000000000001',MAINNET),('1.000000000001',MAINNET),('0.1','bad-address')])
def test_invalid_or_over_unlocked_amount_does_not_reach_confirmation(raw,address):
    run('''const {sheet,confirm}=await openUserSend(%s,%s);
    assert.equal(sheet,confirm);assert.equal(posts.length,0);assert(toasts.length>0);''' % (json.dumps(raw),json.dumps(address)))


def test_confirmation_discloses_service_deduction_and_extra_network_fee():
    run(r'''const {confirm}=await openUserSend('0.1');
    assert.match(confirm.html,/2\s*%/);assert.match(confirm.html,/network|min(?:er|ing)/i);
    assert.match(confirm.html,/receiv|deduct|service fee/i);''')
