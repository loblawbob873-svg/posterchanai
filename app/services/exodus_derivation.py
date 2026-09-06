"""Exodus key formats, versioned so previously issued CloudOS addresses stay recoverable."""
from app.services import exodus_wallet_service as W

LEGACY = 'cloudos-v1'
EXODUS = 'exodus-v1'


def profile(doc):
    value = doc.get('derivation', LEGACY)
    if value not in (LEGACY, EXODUS):
        raise W.WalletLocked('This wallet uses an unsupported key format')
    return value


def private_key(phrase, symbol, *, account=0, index=0, format=EXODUS, purpose=84, change=0):
    if format not in (LEGACY, EXODUS):
        raise W.WalletError('unsupported key format')
    if type(account) is not int or not 0 <= account < 16 or type(index) is not int or index < 0:
        raise W.WalletError('invalid address index')
    if symbol == 'BTC' and format == EXODUS:
        return bitcoin_node(phrase, account=account, index=index, change=change, purpose=purpose).PrivateKey().Raw().ToBytes()
    if format == LEGACY or symbol not in ('SOL', 'XRP'):
        return W.private_key_for(phrase, symbol, index, account=account)
    from bip_utils import Bip32Slip10Secp256k1
    coin = 501 if symbol == 'SOL' else 144
    tail = f"0/{index}" if symbol == 'SOL' else f"0'/{index}'"
    return Bip32Slip10Secp256k1.FromSeedAndPath(
        W._seed_bytes(phrase), f"m/44'/{coin}'/{account}'/{tail}").PrivateKey().Raw().ToBytes()


def address(phrase, symbol, *, account=0, index=0, format=EXODUS, purpose=84, change=0):
    if format not in (LEGACY, EXODUS):
        raise W.WalletError('unsupported key format')
    if symbol == 'BTC' and format == EXODUS:
        return bitcoin_node(phrase, account=account, index=index, change=change, purpose=purpose).PublicKey().ToAddress()
    if format == LEGACY or symbol not in ('SOL', 'XRP'):
        return W.address_for(phrase, symbol, index, account=account)
    key = private_key(phrase, symbol, account=account, index=index, format=format)
    if symbol == 'SOL':
        from bip_utils import Ed25519PrivateKey, SolAddrEncoder
        return SolAddrEncoder.EncodeKey(Ed25519PrivateKey.FromBytes(key).PublicKey())
    from bip_utils import Secp256k1PrivateKey, XrpAddrEncoder
    return XrpAddrEncoder.EncodeKey(Secp256k1PrivateKey.FromBytes(key).PublicKey())


def addresses(doc, phrase, *, account=0):
    format = profile(doc)
    index = int(doc.get('addressIndex') or 0)
    return {symbol: address(phrase, symbol, account=account, index=index, format=format)
            for symbol in W.CHAINS}


def bitcoin_node(phrase, *, account=0, index=0, change=0, purpose=84):
    """Exodus receive/change families; address discovery must retain all three purposes."""
    if type(account) is not int or not 0 <= account < 16:
        raise W.WalletError('invalid portfolio')
    if type(index) is not int or not 0 <= index < 2**31 or type(change) is not int or change not in (0, 1):
        raise W.WalletError('invalid address index')
    from bip_utils import Bip44, Bip44Coins, Bip44Changes, Bip84, Bip84Coins, Bip86, Bip86Coins
    choices = {44: (Bip44, Bip44Coins.BITCOIN), 84: (Bip84, Bip84Coins.BITCOIN),
               86: (Bip86, Bip86Coins.BITCOIN)}
    if type(purpose) is not int or purpose not in choices:
        raise W.WalletError('unsupported Bitcoin address family')
    cls, coin = choices[purpose]
    branch = Bip44Changes.CHAIN_EXT if change == 0 else Bip44Changes.CHAIN_INT
    return cls.FromSeed(W._seed_bytes(phrase), coin).Purpose().Coin().Account(account).Change(branch).AddressIndex(index)
