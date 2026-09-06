"""Independent Exodus keychain outputs for the public BIP-39 test mnemonic."""
import json
from pathlib import Path
import pytest
from app.services import exodus_derivation as D, exodus_wallet_service as W

DATA = json.loads((Path(__file__).parent / 'fixtures/exodus/keychain-v12.json').read_text())


@pytest.mark.parametrize('vector', DATA['vectors'], ids=lambda v: f"{v['symbol']}-portfolio{v['account']}")
def test_keys_match_official_exodus_keychain(vector):
    from bip_utils import Ed25519PrivateKey, Secp256k1PrivateKey
    key = D.private_key(DATA['mnemonic'], vector['symbol'], account=vector['account'], purpose=vector.get('purpose',44), change=vector.get('change',0), index=vector.get('index',0))
    assert key.hex() == vector['privateKey']
    cls = Ed25519PrivateKey if vector['symbol'] == 'SOL' else Secp256k1PrivateKey
    public = cls.FromBytes(key).PublicKey().RawCompressed().ToBytes()
    if vector['symbol'] == 'SOL':
        public = public[-32:]  # bip_utils' tagged encoding includes a leading zero.
    assert public.hex() == vector['publicKey']


def test_old_wallet_formats_keep_every_previously_issued_address():
    old = D.addresses({}, DATA['mnemonic'])
    assert old == W.addresses(DATA['mnemonic'])
    new = D.addresses({'derivation': D.EXODUS}, DATA['mnemonic'])
    assert new['SOL'] != old['SOL'] and new['XRP'] != old['XRP']
    assert all(new[s] == old[s] for s in W.CHAINS if s not in ('BTC', 'SOL', 'XRP'))


def test_unknown_key_format_never_falls_back_to_another_wallet():
    with pytest.raises(W.WalletLocked):
        D.addresses({'derivation': 'future-format'}, DATA['mnemonic'])


def test_default_bitcoin_is_the_published_bip84_vector():
    assert D.address(DATA['mnemonic'], 'BTC') == 'bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu'
    assert D.address(DATA['mnemonic'], 'BTC', purpose=44) == W.address_for(DATA['mnemonic'], 'BTC')
    assert D.address(DATA['mnemonic'], 'BTC', purpose=86).startswith('bc1p')
    assert D.address(DATA['mnemonic'], 'BTC', change=1) != D.address(DATA['mnemonic'], 'BTC')
