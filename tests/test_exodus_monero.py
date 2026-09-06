"""Independent wallet files, synchronization and no-repeat Monero relay behavior."""
from contextlib import contextmanager
from pathlib import Path
import asyncio
import json
import os
import shutil
import pytest
from app.services import exodus_monero as M, exodus_wallet_service as W
from app.services.exodus_send_service import SendRefused, SendUnsure

PHRASE = 'abandon ' * 11 + 'about'
STORAGE = b'1' * 32


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('EXODUS_MONERO_DIR', str(tmp_path))
    monkeypatch.setenv('EXODUS_MONERO_DAEMON', 'http://blockchain.invalid:18081')
    monkeypatch.setattr(M, '_daemon_height', lambda: 1000)
    return tmp_path


class RpcFixture:
    def __init__(self, outcome='success'):
        self.calls = []; self.outcome = outcome
    def call(self, method, params=None, **kwargs):
        self.calls.append((method, params))
        if method == 'validate_address': return {'valid': True, 'nettype': 'mainnet'}
        if method == 'refresh': return {}
        if method == 'get_height': return {'height':1000}
        if method == 'get_balance': return {'balance': 2000, 'unlocked_balance': 1900}
        if method == 'transfer':
            assert params['do_not_relay'] is True and params['get_tx_metadata'] is True
            return {'tx_hash': 'ab'*32, 'tx_metadata': 'cd'*64, 'fee': 10, 'amount': 1000}
        if method == 'relay_tx':
            assert params == {'hex':'cd'*64}
            if self.outcome == 'lost': raise TimeoutError('acknowledgement lost')
            if self.outcome == 'wrong': return {'tx_hash':'ef'*32}
            return {'tx_hash':'ab'*32}
        if method == 'store': return {}
        raise AssertionError(method)


def rpc(monkeypatch, fixture):
    @contextmanager
    def open_wallet(*args, **kwargs): yield fixture
    monkeypatch.setattr(M, 'wallet_rpc', open_wallet)


def test_successful_retry_returns_the_same_transaction_without_relaying_again(isolated, monkeypatch):
    fixture = RpcFixture(); rpc(monkeypatch, fixture)
    keys = W.monero_keys(PHRASE)
    scope = M.identity(1, 'default', 0, keys.PrimaryAddress())
    result = M._send(scope, keys, STORAGE, '01'*16, keys.PrimaryAddress(), 1000)
    assert result['hash'] == 'ab'*32
    assert M._send(scope, keys, STORAGE, '01'*16, keys.PrimaryAddress(), 1000) == result
    assert [name for name, _ in fixture.calls].count('relay_tx') == 1
    assert 'cd'*64 not in (isolated/scope/('send-'+'01'*16+'.enc')).read_text()
    with pytest.raises(SendRefused, match='different payment'):
        M._send(scope, keys, STORAGE, '01'*16, keys.PrimaryAddress(), 1001)


@pytest.mark.parametrize('outcome', ['lost', 'wrong'])
def test_uncertain_relay_blocks_old_and_new_request_ids_across_restart(isolated, monkeypatch, outcome):
    fixture = RpcFixture(outcome); rpc(monkeypatch, fixture)
    keys = W.monero_keys(PHRASE); scope = M.identity(1,'default',0,keys.PrimaryAddress())
    with pytest.raises(SendUnsure, match='ab'*32):
        M._send(scope, keys, STORAGE, '01'*16, keys.PrimaryAddress(), 1000)
    for token in ['01'*16, '02'*16]:
        with pytest.raises(SendUnsure):
            M._send(scope, keys, STORAGE, token, keys.PrimaryAddress(), 1000)
    assert [name for name, _ in fixture.calls].count('transfer') == 1
    assert [name for name, _ in fixture.calls].count('relay_tx') == 1


def test_busy_wallet_never_starts_a_second_rpc_process(isolated):
    lock = isolated/'wallet.lock'
    with M._lock(lock):
        with pytest.raises(M.Unavailable):
            with M._lock(lock): pass
    with M._lock(lock): pass


def test_wallet_scopes_and_recovery_words_are_independent():
    from bip_utils import Monero, MoneroMnemonicDecoder
    a = W.monero_keys(PHRASE,0); b = W.monero_keys(PHRASE,1)
    assert a.PrimaryAddress() != b.PrimaryAddress()
    assert M.identity(1,'default',0,a.PrimaryAddress()) != M.identity(2,'default',0,a.PrimaryAddress())
    words = M.recovery_phrase(PHRASE,1)
    assert Monero.FromPrivateSpendKey(MoneroMnemonicDecoder().Decode(words)).PrimaryAddress() == b.PrimaryAddress()


def test_unconfigured_chain_never_claims_zero(isolated, monkeypatch):
    monkeypatch.setattr(M, '_binary', lambda: '/unused')
    monkeypatch.delenv('EXODUS_MONERO_DAEMON')
    row = asyncio.run(M.balance(1,'default',0,PHRASE,STORAGE))
    assert not row['known'] and row['amount'] is None
    assert row['address'] == W.monero_keys(PHRASE).PrimaryAddress()


def test_private_state_permissions_and_no_partial_configuration(isolated):
    path = isolated/'config'
    M._write(path, 'private fixture')
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(isolated.iterdir()) == [path]


def test_actual_offline_wallet_creation_reopen_and_recovery(isolated, monkeypatch):
    binary = os.environ.get('PC_TEST_MONERO_RPC') or shutil.which('monero-wallet-rpc')
    if not binary:
        pytest.skip('real monero-wallet-rpc not installed; offline wallet process test did not run')
    monkeypatch.setenv('EXODUS_MONERO_RPC_BINARY', binary)
    keys = W.monero_keys(PHRASE); scope = M.identity(1,'default',0,keys.PrimaryAddress())
    for _ in range(2):
        with M.wallet_rpc(scope, keys, STORAGE, offline=True) as instance:
            assert instance.call('get_address', {'account_index':0})['address'] == keys.PrimaryAddress()
            words = instance.call('query_key', {'key_type':'mnemonic'})['key']
            assert words == M.recovery_phrase(PHRASE)
        assert not (isolated/scope/'rpc.conf').exists()


def test_partial_chain_scan_never_persists_a_zero_balance(isolated, monkeypatch):
    fixture = RpcFixture(); rpc(monkeypatch, fixture)
    monkeypatch.setattr(M, '_daemon_height', lambda: 2000)
    keys = W.monero_keys(PHRASE)
    scope = M.identity(1, 'default', 0, keys.PrimaryAddress())
    (isolated/scope).mkdir()
    assert M._synchronize(scope, keys, STORAGE) is False
    assert not (isolated/scope/'balance.json').exists()
    assert all(method != 'get_balance' for method, _ in fixture.calls)


def test_duplicate_imports_share_the_same_spend_lock():
    address = W.monero_keys(PHRASE).PrimaryAddress()
    assert M.identity(1,'default',0,address) == M.identity(1,'ab'*16,0,address)


@pytest.mark.parametrize('outcome', ['success', 'lost'])
def test_relay_attempt_invalidates_the_pre_payment_balance(isolated, monkeypatch, outcome):
    fixture = RpcFixture(outcome)
    rpc(monkeypatch, fixture)
    keys = W.monero_keys(PHRASE)
    scope = M.identity(1, 'default', 0, keys.PrimaryAddress())
    (isolated / scope).mkdir()
    cache = isolated / scope / 'balance.json'
    cache.write_text(json.dumps({'units': 2000, 'unlocked': 1900, 'checkedAt': 1}))
    if outcome == 'lost':
        with pytest.raises(SendUnsure):
            M._send(scope, keys, STORAGE, '01' * 16, keys.PrimaryAddress(), 1000)
    else:
        M._send(scope, keys, STORAGE, '01' * 16, keys.PrimaryAddress(), 1000)
    assert not cache.exists()


@pytest.mark.parametrize('outcome', ['success', 'lost'])
def test_rpc_shutdown_error_cannot_report_relayed_payment_as_unsent(isolated, monkeypatch, outcome):
    fixture = RpcFixture(outcome)

    @contextmanager
    def shutdown_failure(*args, **kwargs):
        try:
            yield fixture
        finally:
            raise OSError('wallet process cleanup failed')

    monkeypatch.setattr(M, 'wallet_rpc', shutdown_failure)
    keys = W.monero_keys(PHRASE)
    scope = M.identity(1, 'default', 0, keys.PrimaryAddress())
    with pytest.raises(SendUnsure, match='ab' * 32):
        M._send(scope, keys, STORAGE, '01' * 16, keys.PrimaryAddress(), 1000)
    assert [name for name, _ in fixture.calls].count('relay_tx') == 1
    if outcome == 'success':
        assert M._send(scope, keys, STORAGE, '01' * 16, keys.PrimaryAddress(), 1000)['hash'] == 'ab' * 32
    else:
        with pytest.raises(SendUnsure):
            M._send(scope, keys, STORAGE, '02' * 16, keys.PrimaryAddress(), 1000)
    assert [name for name, _ in fixture.calls].count('relay_tx') == 1
