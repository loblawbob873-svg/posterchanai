"""Wallet RPC history reaches the real bundled UI, including failure and recovery.

Synthetic account data only; every wallet request is intercepted before network access.
"""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_channel_controls_full_app import click
from tests.test_monero_user_history import NPUB, _rows, wallet_with_transfers


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def history_payload():
    rows = []
    for i in range(60):
        row = _rows(1, format(i + 1, '064x'), amount=70000000000 if i == 59 else 1000000000)
        row['timestamp'] += i
        rows.append(row)
    incoming = _rows(1, 'a' * 64, amount=200000000000)
    incoming['timestamp'] += 100
    pending = _rows(1, 'b' * 64)
    pending['timestamp'] += 101
    return asyncio.run(wallet_with_transfers({
        'out': rows[::-1], 'pool': [incoming], 'pending': [pending],
    }).history(NPUB))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_recent_payments_render_and_history_errors_recover_without_losing_balance(width):
    payload = history_payload()

    async def check(browser):
        await browser.call('Emulation.setDeviceMetricsOverride', dict(
            width=width, height=850, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(browser)
        await browser.js("__PC.switchView('wallet')")
        await browser.until("document.querySelectorAll('.mw-tx').length===50")
        rows = await browser.js("[...document.querySelectorAll('.mw-tx')].map(e=>({text:e.textContent,id:e.querySelector('[data-tx]')?.dataset.tx}))")
        assert rows[0]['id'] == 'b' * 64
        assert 'Sent · unconfirmed' in rows[0]['text']
        assert rows[1]['id'] == 'a' * 64
        assert 'Received · unconfirmed' in rows[1]['text']
        assert '+0.2 XMR' in rows[1]['text']
        assert rows[2]['id'] == format(60, '064x')
        assert '−0.07 XMR' in rows[2]['text']
        assert format(1, '064x') not in [row['id'] for row in rows]
        assert '0.0262007255' in await browser.js("document.querySelector('.mw-balance').textContent")
        # Read the complete ID through an actual reachable copy button at both viewport sizes.
        await browser.js("document.querySelectorAll('.mw-tx-copy')[2].scrollIntoView({block:'center'})")
        await click(browser, '.mw-tx:nth-child(3) .mw-tx-copy')
        await browser.until("window.__walletCopied==='" + format(60, '064x') + "'")
        await browser.js("window.__historyFails=true;document.querySelector('#mw-refresh').scrollIntoView({block:'center'})")
        await click(browser, '#mw-refresh')
        await browser.until("document.querySelector('.mw-wrap')?.textContent.includes('transactions could not be loaded')")
        assert '0.0262007255' in await browser.js("document.querySelector('.mw-balance').textContent")
        assert not await browser.js("document.querySelector('.mw-wrap').textContent.includes('No transactions yet')")
        assert await browser.js("!!document.querySelector('#mw-me-send')&&!document.querySelector('#mw-me-send').disabled")
        await browser.js("window.__historyFails=false")
        await click(browser, '#mw-refresh')
        await browser.until("document.querySelectorAll('.mw-tx').length===50")
        assert not await browser.js("document.querySelector('.mw-wrap').textContent.includes('transactions could not be loaded')")
        assert await browser.js("__walletRequests.every(r=>r.method==='GET')"), 'viewing history attempted a wallet mutation'

    asyncio.run(desktop.with_browser('online', '', check, extra_init=wallet_init(payload)))


def wallet_init(payload):
    return 'window.__walletHistory=' + json.dumps(payload) + ';' + r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__walletRequests=[];window.__historyFails=false;
Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{window.__walletCopied=value;}}});
const walletFetch=window.fetch;
window.fetch=(url,opts)=>{
  const path=String(url);
  if(!path.includes('/api/wallet/xmr/'))return walletFetch(url,opts);
  __walletRequests.push({path,method:opts?.method||'GET'});
  let body={},status=200;
  if(!path.includes('/me/')){status=403;body={detail:'not the node operator'};}
  else if(path.includes('/status'))body={enabled:true,network:'mainnet'};
  else if(path.includes('/balance'))body={address:'4'+'A'.repeat(94),balance:'0.0262007255',unlocked_balance:'0.0262007255',outputs:4};
  else if(path.includes('/history')){status=__historyFails?503:200;body=__historyFails?{detail:'fixture history temporarily unavailable'}:__walletHistory;}
  else throw Error('Unexpected wallet request: '+path);
  return Promise.resolve(new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}}));
};
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_your_address_copies_and_receive_shows_a_qr_that_copies_the_same_address():
    """A member's wallet: Copy under "Your receiving address" copies it; Receive opens a QR of the same address, and
    its Copy address copies exactly that."""
    address = '4' + 'A' * 94

    async def check(browser):
        await desktop.login(browser)
        await browser.js("__PC.switchView('wallet')")
        await browser.until("!!document.querySelector('#mw-me-copy')")
        await browser.js("document.querySelector('#mw-me-copy').click()")
        await browser.until(f"window.__walletCopied==={address!r}")
        await browser.js("window.__walletCopied=null; document.querySelector('#mw-me-receive').click()")
        await browser.until("!!document.querySelector('#mw-me-qcopy') && !!document.querySelector('.mw-qr svg, .mw-qr img, .mw-qr canvas')")
        await browser.js("document.querySelector('#mw-me-qcopy').click()")
        await browser.until(f"window.__walletCopied==={address!r}")

    asyncio.run(desktop.with_browser('online', '', check, extra_init=wallet_init(history_payload())))


def node_wallet_init(payload):
    """The NODE's wallet answering (an operator's view): status names a block explorer, history has payments."""
    return 'window.__walletHistory=' + json.dumps(payload) + ';' + r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{window.__walletCopied=value;}}});
const nodeFetch=window.fetch;
window.fetch=(url,opts)=>{
  const path=String(url);
  if(!path.includes('/api/wallet/xmr/'))return nodeFetch(url,opts);
  const addr='4'+'B'.repeat(94);let body={};
  if(path.includes('/status'))body={enabled:true,network:'mainnet',explorer_tx_base:'https://explorer.example/tx/'};
  else if(path.includes('/balance'))body={address:addr,balance:'1.5',unlocked_balance:'1.5',outputs:3};
  else if(path.includes('/address'))body={address:addr};
  else if(path.includes('/history'))body=__walletHistory;
  else if(path.includes('/sync-state'))body={scanning:false};
  return Promise.resolve(new Response(JSON.stringify(body),{status:200,headers:{'Content-Type':'application/json'}}));
};
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_node_wallet_copy_receive_send_and_explorer_copy():
    """The operator's wallet: Copy copies the receive address; Receive shows a QR whose Copy copies the same
    address; Send opens the send sheet; a payment's ↗ asks before going to an explorer, and "Copy the transaction
    ID instead" copies the id and goes nowhere."""
    address = '4' + 'B' * 94

    async def check(browser):
        await desktop.login(browser)
        await browser.js("window.__opened=[];window.open=u=>{__opened.push(String(u));return null;};__PC.switchView('wallet')")
        await browser.until("!!document.querySelector('#mw-copy')")
        await browser.js("document.querySelector('#mw-copy').click()")
        await browser.until(f"window.__walletCopied==={address!r}")
        await browser.js("window.__walletCopied=null;document.querySelector('#mw-receive').click()")
        await browser.until("!!document.querySelector('#mw-modal-copy')")
        await browser.js("document.querySelector('#mw-modal-copy').click()")
        await browser.until(f"window.__walletCopied==={address!r}")
        await browser.js("__PC.closeModal(); document.querySelector('#mw-send').click()")
        await browser.until("/Send/i.test((document.querySelector('.mw-modal h3')||{}).textContent||'')")
        await browser.js("__PC.closeModal()")
        await browser.until("!!document.querySelector('.mw-tx-open')")
        tx = await browser.js("document.querySelector('.mw-tx-open').getAttribute('data-tx')")
        await browser.js("document.querySelector('.mw-tx-open').click()")
        await browser.until("!!document.querySelector('#mw-exp-copy')")
        await browser.js("document.querySelector('#mw-exp-copy').click()")
        await browser.until(f"window.__walletCopied==={tx!r}")
        assert await browser.js("__opened.length") == 0, "the explorer was opened without being asked to"

    asyncio.run(desktop.with_browser('online', '', check, extra_init=node_wallet_init(history_payload())))
