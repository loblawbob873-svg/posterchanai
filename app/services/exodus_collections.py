"""Account-owned wallets and BIP-44 portfolios, without replacing the original seed."""
import asyncio
import json
import re
import time
import uuid

from app.models import ExodusWalletRecord
from app.services import exodus_vault as legacy, exodus_wallet_service as wallet, nostr_store

# Existing private, non-expiring application document kind; do not use a public kind.
KIND = 30078
PREFIX = 'pcai:exodus:collection:'
_locks = {}


def validate_id(value):
    if value != 'default' and not re.fullmatch(r'[0-9a-f]{32}', value or ''):
        raise wallet.WalletError('invalid wallet identifier')
    return value


def _key(db, user):
    return nostr_store.user_storage_seckey(db, user)


def _row(db, user, wallet_id):
    return db.query(ExodusWalletRecord).filter(
        ExodusWalletRecord.user_id == user.id,
        ExodusWalletRecord.wallet_id == wallet_id).first()


def _mirror(db, user, wallet_id, doc):
    row = _row(db, user, wallet_id)
    if row is None:
        row = ExodusWalletRecord(user_id=user.id, wallet_id=wallet_id)
        db.add(row)
    row.document_enc = wallet.seal(json.dumps(doc), _key(db, user))
    db.commit()


async def _publish(db, user, wallet_id, doc):
    if not await nostr_store.put_doc(legacy._port(db), _key(db, user),
                                    PREFIX + wallet_id, doc, kind=KIND):
        raise legacy.VaultUnavailable('the wallet could not be saved to the relay')


def _validate(doc):
    if not isinstance(doc, dict) or not doc.get('seed'):
        raise legacy.VaultUnavailable('the stored wallet document is unreadable')
    return doc


async def load(db, user, wallet_id='default'):
    validate_id(wallet_id)
    try:
        doc = await nostr_store.get_doc(legacy._port(db), PREFIX + wallet_id,
                                       seckey=_key(db, user), kind=KIND, strict=True)
        if doc is not None:
            doc = _validate(doc)
            backup = _row(db, user, wallet_id)
            if getattr(backup, 'document_enc', None):
                saved = _validate(json.loads(wallet.unseal(bytes(backup.document_enc), _key(db, user))))
                if saved['seed'] != doc['seed']:
                    raise legacy.VaultUnavailable('the wallet seed does not match its encrypted backup')
        else:
            row = _row(db, user, wallet_id)
            if getattr(row, 'document_enc', None):
                doc = _validate(json.loads(wallet.unseal(bytes(row.document_enc), _key(db, user))))
                await _publish(db, user, wallet_id, doc)
        if wallet_id == 'default':
            original = await legacy.load(db, user)
            if original:
                if doc and doc['seed'] != original['seed']:
                    raise legacy.VaultUnavailable('the original wallet backup does not match')
                return {**(doc or {}), **original}
        return doc
    except wallet.WalletError:
        raise
    except Exception as error:
        raise legacy.VaultUnavailable('this wallet could not be read') from error


def portfolios(doc):
    entries = doc.get('portfolios') or [{'id': 0, 'name': 'Main portfolio'}]
    if not isinstance(entries, list) or not entries or len(entries) > 16:
        raise legacy.VaultUnavailable('the portfolio list is unreadable')
    seen = set()
    for entry in entries:
        number = entry.get('id') if isinstance(entry, dict) else None
        if type(number) is not int or not 0 <= number < 16 or number in seen:
            raise legacy.VaultUnavailable('the portfolio list is unreadable')
        seen.add(number)
    return entries


def require_portfolio(doc, portfolio):
    if portfolio not in {item['id'] for item in portfolios(doc)}:
        raise wallet.WalletError('this portfolio does not exist in the selected wallet')


def summary(wallet_id, doc):
    return {'id': wallet_id, 'name': doc.get('label') or ('Main wallet' if wallet_id == 'default' else 'Wallet'),
            'portfolios': portfolios(doc), 'backedUp': bool(doc.get('backedUpAt'))}


async def list_wallets(db, user):
    try:
        ids = set()
        cursor = None
        while True:
            documents = await nostr_store.list_docs(
                legacy._port(db), PREFIX, seckey=_key(db, user), kind=KIND,
                strict=True, limit=1000, with_meta='cursor', cursor=cursor)
            ids.update(validate_id(tag.removeprefix(PREFIX)) for tag in documents)
            if len(documents) < 1000:
                break
            # Equal timestamps must still advance: this relay sorts by timestamp AND event ID.
            next_cursor = min((stamp, event_id) for _, stamp, event_id in documents.values())
            if cursor is not None and next_cursor >= cursor:
                raise legacy.VaultUnavailable('the wallet list could not be completely read')
            cursor = next_cursor
        ids.update(row.wallet_id for row in db.query(ExodusWalletRecord).filter(
            ExodusWalletRecord.user_id == user.id).all())
        ids.add('default')
        result = []
        for wallet_id in sorted(ids, key=lambda item: (item != 'default', item)):
            doc = await load(db, user, wallet_id)
            if doc:
                result.append(summary(wallet_id, doc))
        return result
    except wallet.WalletError:
        raise
    except Exception as error:
        raise legacy.VaultUnavailable('the wallet list could not be read') from error


async def create(db, user, phrase, label):
    # The server generates a new identity for every new wallet. No caller-selected identifier
    # can turn this endpoint into a write over an existing wallet's seed.
    wallet_id = uuid.uuid4().hex
    doc = {'seed': wallet.seal(phrase, _key(db, user)).hex(), 'label': label or 'Wallet',
           'addressIndex': 0, 'backedUpAt': 0, 'createdAt': int(time.time()),
           'portfolios': [{'id': 0, 'name': 'Main portfolio'}]}
    # Keep a discoverable encrypted backup even if publication fails after key generation.
    _mirror(db, user, wallet_id, doc)
    await _publish(db, user, wallet_id, doc)
    return summary(wallet_id, doc)


async def update(db, user, wallet_id='default', *, label=None, backed_up_at=None,
                 new_portfolio=None):
    lock = _locks.setdefault((user.id, wallet_id), asyncio.Lock())
    async with lock:
        doc = await load(db, user, wallet_id)
        if not doc:
            raise wallet.WalletError('this wallet does not exist')
        # Deep-copy relay data before editing; failed writes cannot alter a caller's read cache.
        doc = json.loads(json.dumps(doc))
        if label is not None:
            doc['label'] = label
        if backed_up_at is not None:
            doc['backedUpAt'] = backed_up_at
        if new_portfolio is not None:
            entries = list(portfolios(doc))
            if len(entries) >= 16:
                raise wallet.WalletError('this wallet already has 16 portfolios')
            number = max(entry['id'] for entry in entries) + 1
            if number >= 16:
                raise wallet.WalletError('this wallet already has 16 portfolios')
            doc['portfolios'] = entries + [{'id': number, 'name': new_portfolio}]
        await _publish(db, user, wallet_id, doc)
        _mirror(db, user, wallet_id, doc)
        if wallet_id == 'default' and (label is not None or backed_up_at is not None):
            fields = {}
            if label is not None:
                fields['label'] = label
            if backed_up_at is not None:
                fields['backedUpAt'] = backed_up_at
            await legacy.update(db, user, **fields)
        return doc
