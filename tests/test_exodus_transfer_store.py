import asyncio
import json
import httpx
import pytest
from eth_utils import keccak
from app.services import exodus_transfers as T, exodus_send_service as S, exodus_wallet_service as W
from tests.test_exodus_evm_safety import KEY, FROM, TO, transport

STORE_KEY = b'z' * 32
TOKEN = '12' * 16
IDENTITY = T.scope(1, 'ETH', FROM)


@pytest.fixture
def anyio_backend(): return 'asyncio'


@pytest.fixture
def directory(tmp_path, monkeypatch):
    monkeypatch.setenv('EXODUS_TRANSFER_DIR', str(tmp_path))
    return tmp_path / IDENTITY


async def operation(checkpoint):
    return await S.send_evm(symbol='ETH', private_key=KEY, to=TO, units=10**16,
                            endpoint='https://fixture.invalid', from_address=FROM,
                            before_broadcast=checkpoint)


@pytest.mark.anyio
async def test_real_signed_transaction_is_checkpointed_before_broadcast_and_retry_does_not_send(directory, monkeypatch):
    def broadcast(request, raw):
        record = json.loads(W.unseal(bytes.fromhex((directory/(TOKEN+'.enc')).read_text()), STORE_KEY))
        assert record['state'] == 'broadcast' and record['hash'] == '0x'+keccak(raw).hex()
        return httpx.Response(200, json={'result':record['hash']})
    calls, signed = transport(monkeypatch, broadcast=broadcast)
    result = await T.send(IDENTITY, STORE_KEY, TOKEN, TO, 10**16, operation)
    assert await T.send(IDENTITY, STORE_KEY, TOKEN, TO, 10**16, operation) == result
    assert len(signed) == 1 and calls.count('eth_sendRawTransaction') == 1
    assert not (directory/'pending').exists()


@pytest.mark.anyio
async def test_lost_reply_survives_restart_and_blocks_a_new_payment_until_lookup(directory, monkeypatch):
    calls, signed = transport(monkeypatch, broadcast=lambda *_:httpx.Response(503))
    with pytest.raises(S.SendUnsure):
        await T.send(IDENTITY, STORE_KEY, TOKEN, TO, 10**16, operation)
    for token in (TOKEN, '34'*16):
        with pytest.raises(S.SendUnsure):
            await T.send(IDENTITY, STORE_KEY, token, TO, 10**16, operation)
    tx_hash = '0x'+keccak(signed[0]).hex()
    async def lookup(client, endpoint, method, params):
        if method == 'eth_chainId': return '0x1'
        assert method == 'eth_getTransactionByHash' and params == [tx_hash]
        return {'hash':tx_hash}
    monkeypatch.setattr(S, '_rpc', lookup)
    assert await T.status(IDENTITY, STORE_KEY, 'ETH', 'https://fixture.invalid') == {'state':'accepted','hash':tx_hash,'nonce':7}
    assert len(signed) == 1
    assert not (directory/'pending').exists()
    assert (await T.send(IDENTITY, STORE_KEY, TOKEN, TO, 10**16, operation))['hash'] == tx_hash


@pytest.mark.anyio
async def test_a_crash_before_checkpoint_is_confirmed_not_sent_without_broadcast(directory):
    async def crash(checkpoint): raise RuntimeError('worker stopped before signing')
    with pytest.raises(RuntimeError):
        await T.send(IDENTITY, STORE_KEY, TOKEN, TO, 10**16, crash)
    assert await T.status(IDENTITY, STORE_KEY, 'ETH', 'https://never-contact.invalid') == {'state':'not_sent'}
    assert not (directory/'pending').exists()


@pytest.mark.anyio
async def test_concurrent_worker_is_refused_without_entering_its_operation(directory):
    entered, release = asyncio.Event(), asyncio.Event()
    async def first(checkpoint):
        entered.set(); await release.wait(); raise S.SendRefused('fixture did not broadcast')
    async def forbidden(checkpoint): raise AssertionError('second worker reached signing')
    task = asyncio.create_task(T.send(IDENTITY, STORE_KEY, TOKEN, TO, 10**16, first))
    await entered.wait()
    with pytest.raises(S.SendRefused, match='already'):
        await T.send(IDENTITY, STORE_KEY, '34'*16, TO, 10**16, forbidden)
    release.set()
    with pytest.raises(S.SendRefused): await task
    assert not (directory/'pending').exists()
