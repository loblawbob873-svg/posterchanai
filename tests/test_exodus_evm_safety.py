"""Sign real public-fixture transactions through controlled JSON-RPC responses; never broadcast."""
import httpx
import pytest
from eth_account import Account
from eth_utils import keccak
from app.services import exodus_send_service as S

@pytest.fixture
def anyio_backend():
    return "asyncio"


KEY = bytes.fromhex('01' * 32)
FROM = Account.from_key(KEY).address
TO = Account.from_key(bytes.fromhex('02' * 32)).address


def transport(monkeypatch, *, chain='0x1', broadcast=None, estimates=None):
    calls = []
    signed = []
    def handler(request):
        import json
        call = json.loads(request.content)
        method = call['method']; calls.append(method)
        if method == 'eth_sendRawTransaction':
            raw = bytes.fromhex(call['params'][0][2:]); signed.append(raw)
            assert Account.recover_transaction(raw) == FROM
            if broadcast is not None:
                return broadcast(request, raw)
            return httpx.Response(200, json={'result': '0x' + keccak(raw).hex()})
        values = {'eth_chainId': chain, 'eth_getTransactionCount': '0x7',
                  'eth_getBalance': hex(10**20), 'eth_maxPriorityFeePerGas': '0x1',
                  'eth_getBlockByNumber': {'baseFeePerGas': '0x1'},
                  'eth_estimateGas': '0x5208', 'eth_gasPrice': '0x1'}
        if estimates and method == 'eth_estimateGas':
            return httpx.Response(200, json={'error': {'code': -32000, 'message': 'execution reverted'}})
        return httpx.Response(200, json={'result': values[method]})
    monkeypatch.setattr(S, '_client', lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return calls, signed


async def send(symbol='ETH'):
    return await S.send_evm(symbol=symbol, private_key=KEY, to=TO, units=10**16,
                            endpoint='https://fixture.invalid', from_address=FROM)


@pytest.mark.anyio
@pytest.mark.parametrize('symbol,wrong', [('ETH','0x89'),('MATIC','0x1'),('BNB','0x1'),('AVAX','0x1')])
async def test_wrong_chain_is_refused_before_any_signing(monkeypatch, symbol, wrong):
    calls, signed = transport(monkeypatch, chain=wrong)
    with pytest.raises(S.SendRefused, match='network'):
        await send(symbol)
    assert not signed
    assert calls == ['eth_chainId']


@pytest.mark.anyio
@pytest.mark.parametrize('kind', ['http503', 'json-error', 'missing', 'bad-hash', 'timeout'])
async def test_every_unconfirmed_broadcast_keeps_local_hash_and_refuses_to_call_it_failed(monkeypatch, kind):
    def reply(request, raw):
        if kind == 'timeout': raise httpx.ReadTimeout('lost acknowledgement', request=request)
        if kind == 'http503': return httpx.Response(503)
        if kind == 'json-error': return httpx.Response(200, json={'error': {'message': 'already known'}})
        if kind == 'missing': return httpx.Response(200, json={})
        return httpx.Response(200, json={'result': '0x' + 'ff'*32})
    calls, signed = transport(monkeypatch, broadcast=reply)
    with pytest.raises(S.SendUnsure) as caught:
        await send()
    assert len(signed) == 1
    assert '0x' + keccak(signed[0]).hex() in str(caught.value)
    assert calls.count('eth_sendRawTransaction') == 1


@pytest.mark.anyio
async def test_matching_hash_is_returned_for_actual_signed_transaction(monkeypatch):
    _, signed = transport(monkeypatch)
    result = await send()
    assert result['hash'] == '0x' + keccak(signed[0]).hex()
    assert result['nonce'] == 7


@pytest.mark.anyio
async def test_failed_gas_estimation_never_guesses_a_spend(monkeypatch):
    _, signed = transport(monkeypatch, estimates=True)
    with pytest.raises(S.SendRefused): await send()
    assert not signed
