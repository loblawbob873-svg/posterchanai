"""Where the wallet lives: ONE NIP-44-ENCRYPTED NOSTR DOCUMENT PER ACCOUNT.

`pcai:exodus:wallet`, kind 30078, signed and encrypted with the account's server-held storage key —
the same shape as every other private document in this app, and the shape the operator asked for.

WHAT THAT COSTS, AND WHAT IS DONE ABOUT IT. A kind-30078 document is REPLACEABLE: whatever is
written next IS the document. This codebase has recorded that accident more than once — an
unreachable relay answers an empty read, the empty read is written back, and the document is gone.
For a mute list that costs a re-follow. For a wallet seed it costs every coin behind it, with
nothing anywhere able to reconstruct it. So three rules stand between this store and that, and none
of them is optional:

  1. EVERY READ IS STRICT. `get_doc(..., strict=True)` RAISES when the relay cannot be reached,
     instead of returning None like a document that does not exist. A caller that cannot tell those
     apart offers to generate a second seed over the top of the first.
  2. NOTHING IS EVER WRITTEN WITHOUT READING FIRST, and a write that would replace a seed with a
     different one — or with nothing — is refused here, not left to the caller. `save()` will not
     overwrite an existing mnemonic; `update()` carries the stored mnemonic forward verbatim and
     only ever changes the metadata beside it.
  3. THE MNEMONIC IS ALSO KEPT ENCRYPTED AT REST IN A ROW. Not as a second source of truth — the
     document is authoritative and the row is never read unless the document is ABSENT — but because
     "the relay lost it" and "there was never a wallet" are indistinguishable from the outside, and
     one of those two answers is somebody's money. The row is the seed's backup, nothing else reads
     it, and it is written in the same call that writes the document.

The blob in both places is the same AES-GCM ciphertext from `exodus_wallet_service.seal`, so the
relay never holds a mnemonic that the account's storage key alone cannot open, and neither does the
database.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.services import exodus_wallet_service as W

logger = logging.getLogger(__name__)

D_TAG = "pcai:exodus:wallet"


class VaultUnavailable(W.WalletLocked):
    """The relay could not be asked. NEVER the same answer as 'there is no wallet'."""


def _port(db) -> int:
    from app.services.settings_store import get as _get
    try:
        return int(str(_get("nostr_relay_port", "3052") or "3052"))
    except Exception:  # noqa: BLE001
        return 3052


async def load(db, user) -> dict[str, Any] | None:
    """The account's wallet document, or None when there genuinely is not one.

    Raises `VaultUnavailable` when the relay could not be asked — which is the whole reason this
    function exists rather than a bare `get_doc`.
    """
    from app.services import nostr_store as store
    sk = store.user_storage_seckey(db, user)
    try:
        doc = await store.get_doc(_port(db), D_TAG, seckey=sk, strict=True)
    except Exception as exc:  # noqa: BLE001
        raise VaultUnavailable("this account's wallet could not be read from the relay") from exc
    if doc:
        if isinstance(doc, str):
            try:
                doc = json.loads(doc)
            except Exception:  # noqa: BLE001
                raise VaultUnavailable("the stored wallet document is unreadable")
        if isinstance(doc, dict) and doc.get("seed"):
            return doc
        # A document that exists but carries no seed is NOT an empty account. Something wrote over
        # it, and saying "no wallet" here is what would invite a second one on top.
        raise VaultUnavailable("the stored wallet document is missing its seed")

    # The document is genuinely absent. Only now is the row consulted — it is the backup for exactly
    # this case, and restoring from it republishes the document.
    row = _row(db, user)
    if not row or not row.seed_enc:
        return None
    logger.warning("[exodus] wallet document absent for user %s — restoring from the local backup",
                   getattr(user, "id", "?"))
    doc = {"seed": bytes(row.seed_enc).hex(), "label": row.label,
           "addressIndex": int(row.address_index or 0),
           "backedUpAt": int(row.backed_up_at.timestamp()) if row.backed_up_at else 0,
           "createdAt": int(row.created_at.timestamp()) if row.created_at else int(time.time())}
    await _publish(db, user, doc)
    return doc


def _row(db, user):
    from app.models import ExodusWallet
    return db.query(ExodusWallet).filter(ExodusWallet.user_id == user.id).first()


async def _publish(db, user, doc: dict[str, Any]) -> None:
    from app.services import nostr_store as store
    if not doc.get("seed"):
        # The guard that matters most in this file. A seedless write IS the wipe.
        raise W.WalletError("refusing to write a wallet document with no seed")
    sk = store.user_storage_seckey(db, user)
    ok = await store.put_doc(_port(db), sk, D_TAG, doc)
    if not ok:
        raise VaultUnavailable("the wallet could not be saved to the relay")


def _mirror(db, user, doc: dict[str, Any]) -> None:
    """Keep the encrypted backup row in step. Never read unless the document is absent."""
    from datetime import datetime, timezone
    from app.models import ExodusWallet
    blob = bytes.fromhex(str(doc["seed"]))
    row = _row(db, user)
    if not row:
        row = ExodusWallet(user_id=user.id, seed_enc=blob)
        db.add(row)
    row.seed_enc = blob
    row.label = doc.get("label") or None
    row.address_index = int(doc.get("addressIndex") or 0)
    stamp = int(doc.get("backedUpAt") or 0)
    row.backed_up_at = datetime.fromtimestamp(stamp, tz=timezone.utc).replace(tzinfo=None) if stamp else None
    db.commit()


async def save_new(db, user, mnemonic: str, label: str | None) -> dict[str, Any]:
    """Create the account's wallet. REFUSES if one already exists.

    The existence check reads through `load`, so an unreachable relay raises rather than answering
    "no wallet" — the one answer that would let this replace somebody's seed.
    """
    if await load(db, user) is not None:
        raise W.WalletError("this account already has a wallet")
    from app.services import nostr_store as store
    sk = store.user_storage_seckey(db, user)
    doc = {"seed": W.seal(mnemonic, sk).hex(), "label": (label or None),
           "addressIndex": 0, "backedUpAt": 0, "createdAt": int(time.time())}
    await _publish(db, user, doc)
    _mirror(db, user, doc)
    return doc


async def update(db, user, **fields) -> dict[str, Any]:
    """Change the metadata beside the seed, never the seed.

    Read-modify-write, and the mnemonic is carried forward VERBATIM from what was read. A caller
    cannot pass a new seed through here even by accident, which is what keeps every metadata change
    — a label, a rotated receive index, marking it backed up — incapable of losing a wallet.
    """
    doc = await load(db, user)
    if not doc:
        raise W.WalletError("this account has no wallet")
    seed = doc["seed"]
    for key in ("label", "addressIndex", "backedUpAt"):
        if key in fields:
            doc[key] = fields[key]
    doc["seed"] = seed
    await _publish(db, user, doc)
    _mirror(db, user, doc)
    return doc


def mnemonic_of(db, user, doc: dict[str, Any]) -> str:
    from app.services import nostr_store as store
    return W.unseal(bytes.fromhex(str(doc["seed"])), store.user_storage_seckey(db, user))
