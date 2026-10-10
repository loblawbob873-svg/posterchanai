"""The Exodus wallet BACKUP rows (the old `exodus_wallets` and `exodus_wallet_records` tables) as DocTables on this
node's relay (#161, wave 3).

What these rows are, and what they are not: the wallet itself is the account's own NIP-44 document
(`pcai:exodus:wallet` / `pcai:exodus:collection:<id>`, signed with the account's storage key). These rows are its
BACKUP -- the same AES-GCM ciphertext (`exodus_wallet_service.seal`, a key derived from the storage key, which is
not in here) kept somewhere else, read only when the document is ABSENT, because "the relay lost it" and "there
was never a wallet" look identical from outside and one of them is somebody's money. Moving them to an
operator-signed DocTable keeps exactly that: a second, independent copy under a different key and a different
d-tag, encrypted twice (the seal, then NIP-44 to the operator).

  * exodus_wallets          -- `pcai:t:exodus_wallets:<user_id>`: {"user_id", "seed_enc" (hex), "label",
                               "address_index", "backed_up_at", "created_at"}
  * exodus_wallet_records   -- `pcai:t:exodus_wallet_records:<user_id>:<wallet_id>`: {"user_id", "wallet_id",
                               "document_enc" (hex)}

The rules the SQL rows got from the database, kept:
  * every read is STRICT -- "could not ask" raises Unavailable, never "no backup" (the vault turns that into
    VaultUnavailable, never into an offer to make a new seed);
  * a record is INSERTED, never upserted, when a wallet is created (`ainsert_record`): the unique constraint used
    to arbitrate two concurrent creates, now an existence check under this process's row lock does (only the
    app process serves the wallet routes) -- the loser is refused, and nothing overwrites an existing seed.

Relay-only (a node's old SQL rows are copied once by scripts/migrate_tables_to_nostr.py).
"""
import logging
from datetime import datetime
from types import SimpleNamespace

from app.services import app_tables, table_gate
from app.services.doc_table import DocTable

logger = logging.getLogger(__name__)

WALLETS = "exodus_wallets"
RECORDS = "exodus_wallet_records"


class Exists(ValueError):
    """The record being inserted already exists."""


def _hex(b):
    return bytes(b).hex() if b is not None else None


def _bytes(h):
    return bytes.fromhex(h) if h else None


def _wallet_obj(r: dict):
    return SimpleNamespace(user_id=int(r["user_id"]), seed_enc=_bytes(r.get("seed_enc")), label=r.get("label"),
                           address_index=int(r.get("address_index") or 0),
                           backed_up_at=app_tables.from_iso(r.get("backed_up_at")),
                           created_at=app_tables.from_iso(r.get("created_at")))


def _record_obj(r: dict):
    return SimpleNamespace(user_id=int(r["user_id"]), wallet_id=r["wallet_id"],
                           document_enc=_bytes(r.get("document_enc")))


def record_key(user_id, wallet_id) -> str:
    return "%d:%s" % (int(user_id), wallet_id)


# ------------------------------------------------------------------ wallets (one backup per account)
async def awallet(db, user_id):
    """The account's seed backup (attributes like the old row), or None. Unavailable when it could not be read."""
    r = await table_gate.aget_row(DocTable(WALLETS), str(int(user_id)))
    return _wallet_obj(r) if r is not None else None


async def aput_wallet(db, user_id, *, seed_enc: bytes, label, address_index: int, backed_up_at) -> None:
    """Write the account's seed backup."""
    t = DocTable(WALLETS)
    old = await table_gate.aget_row(t, str(int(user_id)))
    await t.aput(str(int(user_id)), {
        "user_id": int(user_id), "seed_enc": _hex(seed_enc), "label": label,
        "address_index": int(address_index or 0), "backed_up_at": app_tables.to_iso(backed_up_at),
        "created_at": (old or {}).get("created_at") or app_tables.to_iso(datetime.utcnow())})


# ------------------------------------------------------------------ records (one backup per wallet)
async def arecord(db, user_id, wallet_id):
    r = await table_gate.aget_row(DocTable(RECORDS), record_key(user_id, wallet_id))
    return _record_obj(r) if r is not None else None


async def arecord_ids(db, user_id) -> set:
    """The wallet ids this account has a backup record for."""
    uid = int(user_id)
    return {r["wallet_id"] for _k, r in await table_gate.arows(DocTable(RECORDS)) if r.get("user_id") == uid}


async def _awrite_record(db, user_id, wallet_id, document_enc: bytes, *, insert: bool) -> None:
    async with app_tables.row_lock(RECORDS):
        if insert and await arecord(db, user_id, wallet_id) is not None:
            raise Exists("this wallet already exists")
        await DocTable(RECORDS).aput(record_key(user_id, wallet_id),
                                     {"user_id": int(user_id), "wallet_id": wallet_id,
                                      "document_enc": _hex(document_enc)})


async def ainsert_record(db, user_id, wallet_id, document_enc: bytes) -> None:
    """A NEW wallet's backup. Raises Exists when one is already there (never replaces a seed)."""
    await _awrite_record(db, user_id, wallet_id, document_enc, insert=True)


async def aput_record(db, user_id, wallet_id, document_enc: bytes) -> None:
    """An existing wallet's backup, rewritten after its document changed (label, portfolios)."""
    await _awrite_record(db, user_id, wallet_id, document_enc, insert=False)


async def apurge_user(db, user_id) -> int:
    """A deleted account's backups (the old FK cascade). Returns rows removed."""
    uid, n = int(user_id), 0
    t = DocTable(WALLETS)
    if await table_gate.aget_row(t, str(uid)) is not None:
        await t.adelete(str(uid))
        n += 1
    t = DocTable(RECORDS)
    for k, r in await table_gate.arows(t):
        if r.get("user_id") == uid:
            await t.adelete(k)
            n += 1
    return n
