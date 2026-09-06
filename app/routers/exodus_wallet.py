"""Multi-chain wallet routes — the wallet belonging to the person signed in.

Every route here is scoped to the CALLER'S OWN account, taken from their session. No route accepts a
user id, a pubkey or a wallet id: a pubkey parameter is an invitation to spend another person's
money by typing theirs. That is the same rule `monero_user_wallet.py` opens with, and it is the only
rule in this file that cannot be relaxed for convenience.

THE SEED NEVER LEAVES EXCEPT WHEN SOMEBODY ASKS FOR IT, ONCE, DELIBERATELY. `/reveal` is the single
route that returns a mnemonic, it is a POST so it cannot be linked or prefetched, and it is not part
of `/status` — a screen that shows the phrase as a side effect of loading is a phrase shoulder-surfed
by whoever walks past. Nothing here logs it, and the log lines that exist name chains and amounts,
never keys.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ExodusWallet, User
from app.routers import auth
from app.services import exodus_wallet_service as W

router = APIRouter(prefix="/api/wallet/exodus", tags=["exodus-wallet"])
CurrentUser = Annotated[User, Depends(auth.get_current_user)]


def _have_library() -> bool:
    """Is bip_utils installed? Asked with find_spec so nothing is imported to answer it — the
    import costs ~0.4s and every route here would pay it just to report a boolean."""
    from importlib.util import find_spec
    try:
        return find_spec("bip_utils") is not None
    except Exception:  # noqa: BLE001
        return False


def _library() -> None:
    """A missing optional dependency is a sentence, not a traceback.

    `bip_utils` is declared in requirements.txt, but an existing deployment that pulls code without
    re-running the dependency step will not have it. Every route that touches a key checks first, so
    the answer is "this node has not installed the wallet library" rather than a 500 whose cause is
    three frames deep in an import.
    """
    if not _have_library():
        raise HTTPException(status_code=503, detail=(
            "This node has not installed the wallet library (bip_utils). "
            "Run the installer's dependency step, or `pip install -r requirements.txt`."))


def _seckey(db: Session, user: User) -> bytes:
    from app.services.nostr_store import user_storage_seckey
    try:
        return user_storage_seckey(db, user)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="this account's storage key is unavailable") from exc


def _settings(_db: Session | None = None) -> dict:
    """The operator's per-chain endpoint overrides, read once per request.

    Missing settings are not an error: every chain falls back to a public endpoint, which is a
    fallback and not a plan — a node that matters should point at its own.
    """
    try:
        from app.services.settings_store import all_settings
        return dict(all_settings() or {})
    except Exception:  # noqa: BLE001
        return {}


def _row(db: Session, user: User) -> ExodusWallet | None:
    return db.query(ExodusWallet).filter(ExodusWallet.user_id == user.id).first()


def _open(db: Session, user: User) -> tuple[ExodusWallet, str]:
    """The caller's wallet and its decrypted phrase, or a refusal that says which problem it is.

    404 means there is no wallet and one can be made. 503 means there IS one and it cannot be
    opened. Collapsing those into one status is how an app offers to generate a second seed over the
    top of somebody's first.
    """
    row = _row(db, user)
    if not row:
        raise HTTPException(status_code=404, detail="no wallet on this account yet")
    try:
        return row, W.unseal(row.seed_enc, _seckey(db, user))
    except W.WalletLocked as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class CreateReq(BaseModel):
    # An imported phrase arrives here and is validated before anything is written. A phrase with one
    # wrong word derives a perfectly valid and completely different wallet, so "looks like words" is
    # not enough — the BIP-39 checksum is the test.
    mnemonic: str | None = Field(default=None, max_length=1024)
    label: str | None = Field(default=None, max_length=80)


class LabelReq(BaseModel):
    label: str = Field(default="", max_length=80)


@router.get("/status")
def status(user: CurrentUser, db: Session = Depends(get_db)):
    """Enough to draw the screen, and nothing secret.

    Deliberately answers even when the library is missing, so the client can say why the wallet is
    unavailable instead of showing an empty page.
    """
    row = _row(db, user)
    return {
        "ok": True,
        "library": _have_library(),
        "exists": bool(row),
        "label": (row.label if row else None),
        "addressIndex": (row.address_index if row else 0),
        "backedUp": bool(row and row.backed_up_at),
        "chains": W.supported(),
        "excluded": W.EXCLUDED,
        # Said here as well as in the UI copy, so an API consumer cannot miss it.
        "custody": "This node holds the keys for this wallet.",
    }


@router.post("/create")
def create(req: CreateReq, user: CurrentUser, db: Session = Depends(get_db)):
    """Make a wallet, or restore one from a phrase.

    REFUSES TO OVERWRITE. A second create on an account that already has a wallet is answered 409,
    never by replacing the row: the old seed would be unrecoverable and the coins behind it gone,
    and "create" is exactly the button somebody presses twice when a page looks unresponsive.
    """
    _library()
    if _row(db, user):
        raise HTTPException(status_code=409, detail="this account already has a wallet")
    phrase = (req.mnemonic or "").strip()
    if phrase:
        if not W.validate_mnemonic(phrase):
            raise HTTPException(status_code=400, detail=(
                "that is not a valid BIP-39 recovery phrase — check the words and their order"))
    else:
        phrase = W.new_mnemonic()
    row = ExodusWallet(user_id=user.id, seed_enc=W.seal(phrase, _seckey(db, user)),
                       label=(req.label or "").strip()[:80] or None, address_index=0)
    db.add(row)
    db.commit()
    # Never the phrase. An imported wallet is already written down; a generated one is shown once,
    # through /reveal, because the person has to ask for it.
    return {"ok": True, "imported": bool(req.mnemonic), "backedUp": False}


@router.get("/addresses")
def addresses(user: CurrentUser, db: Session = Depends(get_db)):
    """Receive addresses for every supported chain, at the wallet's current index."""
    _library()
    row, phrase = _open(db, user)
    return {"ok": True, "index": row.address_index,
            "addresses": W.addresses(phrase, row.address_index)}


@router.get("/balances")
async def balances(user: CurrentUser, db: Session = Depends(get_db)):
    """What the wallet holds, per chain — and which chains could not be asked.

    Every row carries `known`. A client that reads `amount` without it prints "0" for a chain whose
    provider was down, which is the difference between "you have nothing" and "I could not find
    out". Deliberately not cached: a stale balance shown as current is the same lie one refresh
    later.
    """
    _library()
    row, phrase = _open(db, user)
    addrs = W.addresses(phrase, row.address_index)
    from app.services import exodus_chain_service as C
    return {"ok": True, "index": row.address_index,
            "balances": await C.balances(addrs, _settings(db))}


@router.post("/reveal")
def reveal(user: CurrentUser, db: Session = Depends(get_db)):
    """The recovery phrase, because somebody asked for it.

    A POST on purpose: a GET is linkable, prefetchable and lands in history. Asking also marks the
    wallet backed up, which is what stops the app nagging — and the nag exists because a seed nobody
    has written down is one node failure away from gone.
    """
    _library()
    row, phrase = _open(db, user)
    if not row.backed_up_at:
        row.backed_up_at = datetime.utcnow()
        db.commit()
    return {"ok": True, "mnemonic": phrase,
            "warning": "Anyone with these words can spend this wallet. Write them down offline."}


@router.post("/label")
def label(req: LabelReq, user: CurrentUser, db: Session = Depends(get_db)):
    row = _row(db, user)
    if not row:
        raise HTTPException(status_code=404, detail="no wallet on this account yet")
    row.label = req.label.strip()[:80] or None
    db.commit()
    return {"ok": True, "label": row.label}
