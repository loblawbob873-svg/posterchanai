"""Imported LTC/DOGE/BCH funds survive spent addresses and moved change outputs."""
import json
import httpx
import pytest
from app.services import exodus_bitcoin_discovery as B, exodus_derivation as D

PHRASE = 'abandon ' * 11 + 'about'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def provider(symbol, states, calls, *, unreadable=None):
    def handle(request):
        if request.method == 'POST':
            body = json.loads(request.content)
            address = body.get('address') or body['addresses'][0]
        else:
            address = request.url.path.split('/')[-2] if symbol == 'DOGE' else request.url.path.split('/')[-1]
        calls.append((request.method, request.url.path, address))
        state = states.get(address, {'units': 0, 'used': False})
        if symbol == 'LTC':
            spent = 10 if state['used'] else 0
            payload = {'chain_stats': {'funded_txo_sum': state['units'] + spent, 'spent_txo_sum': spent,
                                      'tx_count': int(state['used'])},
                       'mempool_stats': {'funded_txo_sum': 0, 'spent_txo_sum': 0, 'tx_count': 0}}
            if address == unreadable:
                del payload['chain_stats']['tx_count']
        elif symbol == 'DOGE':
            assert request.method == 'GET' and request.url.path.endswith('/balance')
            payload = {'address': address, 'final_balance': state['units'], 'final_n_tx': int(state['used'])}
            if address == unreadable:
                del payload['final_n_tx']
        elif request.url.path.endswith('/bch/balance'):
            assert body == {'addresses': [address]}
            payload = {'success': True, 'balances': [{'address': address,
                       'balance': {'confirmed': state['units'], 'unconfirmed': 0}}]}
        else:
            assert request.url.path.endswith('/bch/txHistory')
            assert body == {'address': address, 'sortOrder': 'DESCENDING'}
            payload = {'success': True, 'address': address,
                       'txs': [{'height': 1, 'tx_hash': 'ab' * 32}] if state['used'] else []}
            if address == unreadable:
                del payload['txs']
        return httpx.Response(200, json=payload)
    return httpx.MockTransport(handle)


@pytest.mark.anyio
@pytest.mark.parametrize('symbol', ['LTC', 'DOGE', 'BCH'])
async def test_spent_receive_and_later_change_addresses_are_included(monkeypatch, symbol):
    states = {}
    for change, index, units in [(0, 0, 0), (0, 2, 123), (1, 1, 456)]:
        address = D.address(PHRASE, symbol, change=change, index=index)
        states[address] = {'units': units, 'used': True}
    calls = []
    transport = provider(symbol, states, calls)
    monkeypatch.setattr(B.C, '_client', lambda: httpx.AsyncClient(transport=transport))
    result = await B.scan({}, PHRASE, 0, 'https://fixture.invalid', symbol=symbol, gap=2, maximum=20)
    assert result['units'] == 579
    assert {row['address'] for row in result['addresses']} == set(states)
    assert all(any(call[2] == address for call in calls) for address in states)


@pytest.mark.anyio
@pytest.mark.parametrize('symbol', ['LTC', 'DOGE', 'BCH'])
async def test_missing_change_history_never_becomes_a_persisted_zero(monkeypatch, tmp_path, symbol):
    broken = D.address(PHRASE, symbol, change=1)
    transport = provider(symbol, {}, [], unreadable=broken)
    monkeypatch.setattr(B.C, '_client', lambda: httpx.AsyncClient(transport=transport))
    assert await B._refresh(tmp_path, {}, PHRASE, 0, b'1' * 32, 'https://fixture.invalid', symbol) is False
    assert not B._cache_file(tmp_path, symbol).exists()


@pytest.mark.anyio
async def test_custom_dogecoin_esplora_provider_keeps_its_history_contract(monkeypatch):
    calls = []
    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={'chain_stats': {'funded_txo_sum': 9, 'spent_txo_sum': 9, 'tx_count': 1},
                                        'mempool_stats': {'funded_txo_sum': 0, 'spent_txo_sum': 0, 'tx_count': 0}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await B.other_address_state(client, 'https://fixture.invalid', 'public-address', 'DOGE',
                                             {'exodus_rpc_doge': 'https://fixture.invalid', 'exodus_api_doge': 'esplora'})
    assert result == {'units': 0, 'used': True}
    assert calls == ['/address/public-address']
