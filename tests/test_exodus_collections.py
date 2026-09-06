import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import User, ExodusWallet, ExodusWalletRecord
from app.routers import auth, exodus_wallet as routes
from app.services import exodus_collections as C, exodus_wallet_service as W, exodus_vault as V

VECTOR = 'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about'


@pytest.fixture
def world(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[User.__table__, ExodusWallet.__table__, ExodusWalletRecord.__table__])
    db = Session(engine)
    users = [User(id=i, username=f'user{i}', password_hash='unused', nostr_npub=f'{i:064x}') for i in [1, 2]]
    db.add_all(users); db.commit()
    state = SimpleNamespace(db=db, users=users, user=users[0], docs={}, reachable=True, publish=True, writes=0)
    async def get(port, tag, *, seckey=None, kind=30078, strict=False, **kwargs):
        assert strict
        if not state.reachable:
            raise ConnectionError('offline')
        return copy.deepcopy(state.docs.get((seckey, kind, tag)))
    async def put(port, key, tag, doc, *, kind=30078, **kwargs):
        if not state.publish:
            return False
        state.writes += 1
        state.docs[key, kind, tag] = copy.deepcopy(doc)
        return True
    async def listing(port, prefix, *, seckey, kind, strict, **kwargs):
        assert strict
        if not state.reachable:
            raise ConnectionError('offline')
        entries = [(tag, doc) for (key, stored_kind, tag), doc in state.docs.items()
                   if key == seckey and kind == stored_kind and tag.startswith(prefix)]
        entries.sort(reverse=True)
        cursor = kwargs.get('cursor')
        if cursor:
            entries = [(tag, doc) for tag, doc in entries if (100, tag) < tuple(cursor)]
        entries = entries[:kwargs.get('limit', 5000)]
        return {tag: (copy.deepcopy(doc), 100, tag) if kwargs.get('with_meta') == 'cursor'
                else copy.deepcopy(doc) for tag, doc in entries}
    monkeypatch.setattr(C.nostr_store, 'get_doc', get)
    monkeypatch.setattr(C.nostr_store, 'put_doc', put)
    monkeypatch.setattr(C.nostr_store, 'list_docs', listing)
    monkeypatch.setattr(C.nostr_store, 'user_storage_seckey', lambda db, user: bytes([user.id])*32)
    monkeypatch.setattr(routes, '_monero_row', AsyncMock(return_value={'known': True, 'amount': '1', 'address': 'node-monero'}))
    monkeypatch.setattr('app.services.exodus_chain_service.balances', AsyncMock(return_value={'ETH': {'known': True, 'amount': '2'}}))
    app = FastAPI(); app.include_router(routes.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[auth.get_current_user] = lambda: state.user
    with TestClient(app) as client:
        state.client = client
        yield state
    db.close(); engine.dispose()


def test_new_wallet_and_portfolio_preserve_original_addresses_and_seed(world):
    client = world.client
    assert client.post('/api/wallet/exodus/create', json={'mnemonic': VECTOR}).status_code == 200
    original = client.get('/api/wallet/exodus/addresses').json()['addresses']
    assert original['BTC'] == '1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA'
    response = client.post('/api/wallet/exodus/portfolios', json={'name': 'Savings'})
    assert response.status_code == 200, response.text
    assert response.json()['portfolios'][-1] == {'id': 1, 'name': 'Savings'}
    separate = client.get('/api/wallet/exodus/addresses?portfolio=1').json()['addresses']
    assert separate['BTC'] != original['BTC'] and separate['ETH'] != original['ETH']
    assert 'XMR' not in separate
    assert client.get('/api/wallet/exodus/addresses').json()['addresses'] == original
    made = client.post('/api/wallet/exodus/wallets', json={'label': 'Travel'})
    assert made.status_code == 200, made.text
    wallet_id = made.json()['id']
    addresses = client.get('/api/wallet/exodus/addresses', params={'wallet': wallet_id}).json()['addresses']
    assert addresses['BTC'] != original['BTC'] and 'XMR' not in addresses
    listed = client.get('/api/wallet/exodus/wallets').json()['wallets']
    assert {w['id'] for w in listed} == {'default', wallet_id}
    assert VECTOR not in str(listed)
    assert client.post('/api/wallet/exodus/reveal').json()['mnemonic'] == VECTOR
    assert client.post('/api/wallet/exodus/create', json={}).status_code == 409


def test_wallet_id_never_selects_another_users_wallet(world):
    made = world.client.post('/api/wallet/exodus/wallets', json={'mnemonic': VECTOR, 'label': 'Private'})
    wallet_id = made.json()['id']
    world.user = world.users[1]
    assert world.client.get('/api/wallet/exodus/wallets').json()['wallets'] == []
    for path in ['addresses', 'balances']:
        assert world.client.get('/api/wallet/exodus/'+path, params={'wallet': wallet_id}).status_code == 404
    assert world.client.post('/api/wallet/exodus/reveal', params={'wallet': wallet_id}).status_code == 404
    assert world.client.get('/api/wallet/exodus/addresses?wallet=../../other').status_code == 422


def test_strict_read_failure_does_not_rewrite_wallet_or_offer_an_empty_list(world):
    world.client.post('/api/wallet/exodus/wallets', json={'mnemonic': VECTOR})
    before = copy.deepcopy(world.docs)
    world.reachable = False
    assert world.client.get('/api/wallet/exodus/wallets').status_code == 503
    assert world.docs == before


def test_failed_publish_keeps_an_encrypted_discoverable_backup(world):
    world.publish = False
    response = world.client.post('/api/wallet/exodus/wallets', json={'mnemonic': VECTOR})
    assert response.status_code == 503
    row = world.db.query(ExodusWalletRecord).one()
    assert VECTOR.encode() not in row.document_enc
    world.publish = True
    listed = world.client.get('/api/wallet/exodus/wallets').json()['wallets']
    assert listed[0]['id'] == row.wallet_id
    assert world.client.post('/api/wallet/exodus/reveal', params={'wallet': row.wallet_id}).json()['mnemonic'] == VECTOR


def test_seed_mismatch_is_refused_instead_of_showing_a_different_wallet(world):
    made = world.client.post('/api/wallet/exodus/wallets', json={'mnemonic': VECTOR}).json()
    tag = (bytes([1])*32, C.KIND, C.PREFIX+made['id'])
    world.docs[tag]['seed'] = W.seal(W.new_mnemonic(), bytes([1])*32).hex()
    assert world.client.get('/api/wallet/exodus/addresses', params={'wallet': made['id']}).status_code == 503


def test_nonexistent_portfolio_cannot_derive_or_send_from_an_unselected_account(world):
    world.client.post('/api/wallet/exodus/create', json={'mnemonic': VECTOR})
    assert world.client.get('/api/wallet/exodus/addresses?portfolio=9').status_code == 404
    response = world.client.post('/api/wallet/exodus/send?portfolio=9', json={
        'symbol': 'ETH', 'amount': '1', 'to': '0x'+'11'*20})
    assert response.status_code == 404


def test_portfolio_derivation_matches_an_independent_raw_bip44_path():
    from bip_utils import Bip32Slip10Secp256k1, Bip39SeedGenerator
    seed = Bip39SeedGenerator(VECTOR).Generate()
    raw = Bip32Slip10Secp256k1.FromSeedAndPath(seed, "m/44'/60'/1'/0/0")
    assert W.private_key_for(VECTOR, 'ETH', account=1) == raw.PrivateKey().Raw().ToBytes()
    assert W.private_key_for(VECTOR, 'ETH', account=1) != W.private_key_for(VECTOR, 'ETH')


def test_send_uses_the_selected_wallet_and_portfolio_key(world, monkeypatch):
    from app.services import exodus_send_service
    made = world.client.post('/api/wallet/exodus/wallets', json={'mnemonic': VECTOR}).json()
    world.client.post('/api/wallet/exodus/portfolios', params={'wallet': made['id']}, json={'name': 'Second'})
    send = AsyncMock(return_value={'hash': 'fixture-tx', 'nonce': 0})
    monkeypatch.setattr(exodus_send_service, 'send_evm', send)
    result = world.client.post('/api/wallet/exodus/send', params={'wallet': made['id'], 'portfolio': 1},
                               json={'symbol': 'ETH', 'to': '0x'+'11'*20, 'amount': '0.01'})
    assert result.status_code == 200, result.text
    assert send.call_args.kwargs['private_key'] == W.private_key_for(VECTOR, 'ETH', account=1)
    assert send.call_args.kwargs['from_address'] == W.address_for(VECTOR, 'ETH', account=1)


def test_wallet_documents_keep_existing_private_retention_and_broadcast_rules(world):
    from app.services.nostr_relay.server import _broadcastable
    from app.services.nostr_relay.store import _NEVER_EXPIRE_KINDS
    assert C.KIND == 30078 and C.KIND in _NEVER_EXPIRE_KINDS
    made = world.client.post('/api/wallet/exodus/wallets', json={'mnemonic': VECTOR})
    assert made.status_code == 200
    for _, kind, tag in world.docs:
        if tag.startswith(C.PREFIX):
            event = {'kind': kind, 'tags': [['d', tag]], 'pubkey': '01' * 32}
            assert not _broadcastable(event, {'backup_datastore': True})


def test_wallet_discovery_pages_equal_timestamps_and_ignores_other_documents(world, monkeypatch):
    key = bytes([1]) * 32
    for index in range(1001):
        wallet_id = f'{index:032x}'
        world.docs[key, C.KIND, C.PREFIX + wallet_id] = {'seed': 'encrypted', 'label': wallet_id}
    for index in range(1005):
        world.docs[key, C.KIND, f'pcai:mail:{index}'] = {'body': 'unrelated'}
    async def load(db, user, wallet_id):
        return world.docs.get((key, C.KIND, C.PREFIX + wallet_id))
    monkeypatch.setattr(C, 'load', load)
    listed = asyncio.run(C.list_wallets(world.db, world.user))
    assert len(listed) == 1001
    assert {entry['id'] for entry in listed} == {f'{i:032x}' for i in range(1001)}
