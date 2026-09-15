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

    init = 'window.__walletHistory=' + json.dumps(payload) + ';' + r'''
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
    asyncio.run(desktop.with_browser('online', '', check, extra_init=init))
