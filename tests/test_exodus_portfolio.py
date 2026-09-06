import asyncio
import copy
from types import SimpleNamespace

import pytest

from app.services import exodus_portfolio as P


def test_total_uses_decimal_arithmetic_and_only_current_known_values():
    balances = {'BTC': {'known': True, 'amount': '0.1'}, 'ETH': {'known': True, 'amount': '0.2'}}
    quotes = {'BTC': {'usd': '0.1', 'at': 1000}, 'ETH': {'usd': '0.1', 'at': 1000}}
    result = P.value(balances, quotes, now=1000)
    assert result['complete'] and result['total'] == '0.03'
    assert result['assets']['ETH']['usd'] == '0.02'


@pytest.mark.parametrize('bad', [None, 'NaN', 'Infinity', '-1', True, '1e1000', 'not a number'])
def test_invalid_or_unknown_balances_cannot_become_a_zero_total(bad):
    result = P.value({'BTC': {'known': True, 'amount': bad}}, {'BTC': {'usd': '100', 'at': 1000}}, now=1000)
    assert not result['complete'] and result['total'] is None and result['missing'] == ['BTC']


def test_missing_balance_or_stale_price_produces_a_partial_total():
    result = P.value({'BTC': {'known': False, 'amount': '0'}, 'ETH': {'known': True, 'amount': '2'},
                      'SOL': {'known': True, 'amount': '1'}},
                     {'ETH': {'usd': '100', 'at': 1000}, 'SOL': {'usd': '50', 'at': 1}}, now=1100)
    assert result['total'] is None and result['known_total'] == '200.00'
    assert result['missing'] == ['BTC', 'SOL']


def test_a_known_zero_needs_no_price_to_be_worth_zero():
    result = P.value({'BTC': {'known': True, 'amount': '0'}}, {}, now=1000)
    assert result['complete'] and result['total'] == '0.00'


def test_history_is_scoped_and_failed_reads_never_overwrite_it(monkeypatch):
    documents, writes = {}, []
    state = {'now': 10000, 'fail': False}
    monkeypatch.setattr(P.time, 'time', lambda: state['now'])
    monkeypatch.setattr(P.nostr_store, 'user_storage_seckey', lambda db, user: bytes([user.id])*32)
    monkeypatch.setattr(P.exodus_vault, '_port', lambda db: 3052)
    async def get(port, tag, *, seckey, strict, kind):
        assert strict and kind == 30078
        if state['fail']:
            raise ConnectionError('offline')
        return copy.deepcopy(documents.get((seckey, tag)))
    async def put(port, key, tag, doc, *, kind):
        writes.append((key, tag)); documents[key, tag] = copy.deepcopy(doc); return True
    monkeypatch.setattr(P.nostr_store, 'get_doc', get)
    monkeypatch.setattr(P.nostr_store, 'put_doc', put)
    async def exercise():
        user = SimpleNamespace(id=1)
        complete = {'complete': True, 'total': '123.45'}
        first = await P.history(None, user, 'default', 0, complete)
        assert first['points'] == [{'at': 10000, 'usd': '123.45'}]
        await P.history(None, user, 'default', 0, complete)
        assert len(writes) == 1
        state['now'] += 901
        second = await P.history(None, user, 'default', 0, complete)
        assert len(second['points']) == 2
        other = await P.history(None, user, 'default', 1, complete)
        assert len(other['points']) == 1
        another = await P.history(None, SimpleNamespace(id=2), 'default', 0, complete)
        assert len(another['points']) == 1
        before = copy.deepcopy(documents)
        state['fail'] = True
        failed = await P.history(None, user, 'default', 0, complete)
        assert not failed['available'] and documents == before
        state['fail'] = False; state['now'] += 901
        await P.history(None, user, 'default', 0, {'complete': False, 'total': None})
        assert documents == before
    asyncio.run(exercise())


def test_price_requests_are_batched_cached_and_keep_timestamped_quotes_on_failure(monkeypatch):
    import httpx
    requests = []
    state = {'now': 10000, 'fail': False}
    def transport(request):
        requests.append(request)
        if state['fail']:
            return httpx.Response(503)
        return httpx.Response(200, json={
            'bitcoin': {'usd': 100, 'last_updated_at': 9999},
            'ethereum': {'usd': 'NaN', 'last_updated_at': 9999},
            'solana': {'usd': 15, 'last_updated_at': 20000}})
    client = httpx.AsyncClient
    monkeypatch.setattr(P.httpx, 'AsyncClient', lambda **kwargs: client(
        transport=httpx.MockTransport(transport), **kwargs))
    monkeypatch.setattr(P.time, 'time', lambda: state['now'])
    monkeypatch.setattr(P, '_cache', {})
    monkeypatch.setattr(P, '_attempted', 0)
    monkeypatch.setattr(P, '_price_lock', asyncio.Lock())
    async def exercise():
        quotes = await asyncio.gather(*[P.prices() for _ in range(8)])
        assert len(requests) == 1
        assert set(requests[0].url.params['ids'].split(',')) == set(P.COINS.values())
        assert all(q == {'BTC': {'usd': '100', 'at': 9999}} for q in quotes)
        state.update(now=10100, fail=True)
        assert await P.prices() == quotes[0]
        assert len(requests) == 2
        state['now'] = 11000
        result = P.value({'BTC': {'known': True, 'amount': '1'}}, await P.prices())
        assert result['total'] is None and result['missing'] == ['BTC']
    asyncio.run(exercise())
