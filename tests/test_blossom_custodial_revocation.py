from types import SimpleNamespace
from app.services import blossom_service as bs, bot_table, user_table, users_store
from app.database import NoSQL


def test_stored_key_does_not_bypass_revocation(monkeypatch):
    user=SimpleNamespace(nostr_nsec='key',access_revoked=True,is_admin=False,can_blossom=False)
    # the accounts and bots tables (relay DocTables, #161): this one account, no bots
    monkeypatch.setattr(user_table,'filter_users',lambda db,pred:[u for u in [user] if pred(u)])
    monkeypatch.setattr(bot_table,'all_bots',lambda db:[])
    db=NoSQL()
    monkeypatch.setattr(bs,'_operator_cache',{'ts':0,'set':frozenset()})
    monkeypatch.setattr(bs.nostr_service,'decode_seckey',lambda _: b'key')
    monkeypatch.setattr(bs.nostr_service,'derive_pubkey',lambda _: 'pubkey')
    assert 'pubkey' not in bs._operator_pubkeys(db)
    user.can_blossom=True
    bs.invalidate_operator_cache()
    assert 'pubkey' in bs._operator_pubkeys(db)
    user.can_blossom=False; user.is_admin=True
    bs.invalidate_operator_cache()
    assert 'pubkey' in bs._operator_pubkeys(db)


def test_revocation_survives_account_record_sync():
    assert 'access_revoked' in users_store.ACCOUNT_FIELDS
    assert users_store._record(SimpleNamespace(nostr_npub='npub',access_revoked=True))['access_revoked'] is True
