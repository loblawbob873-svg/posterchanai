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

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.routers import auth
from app.services import exodus_vault as V
from app.services import exodus_wallet_service as W

logger = logging.getLogger(__name__)

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


async def _doc(db: Session, user: User):
    """The account's wallet document, or None. Raises 503 when the relay could not be asked.

    THREE ANSWERS, NOT TWO, and this is the only place they are separated. "There is no wallet",
    "there is one and the relay is unreachable", and "there is one" mean entirely different things
    to the screen above — collapsing the first two is how an app offers to generate a second seed
    over the top of somebody's first.
    """
    try:
        return await V.load(db, user)
    except V.VaultUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except W.WalletLocked as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _open(db: Session, user: User) -> tuple[dict, str]:
    doc = await _doc(db, user)
    if not doc:
        raise HTTPException(status_code=404, detail="no wallet on this account yet")
    try:
        return doc, V.mnemonic_of(db, user, doc)
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
async def status(user: CurrentUser, db: Session = Depends(get_db)):
    """Enough to draw the screen, and nothing secret.

    Deliberately answers even when the library is missing, so the client can say why the wallet is
    unavailable instead of showing an empty page.
    """
    doc = await _doc(db, user)
    return {
        "ok": True,
        "library": _have_library(),
        "exists": bool(doc),
        "label": (doc.get("label") if doc else None),
        "addressIndex": (int(doc.get("addressIndex") or 0) if doc else 0),
        "backedUp": bool(doc and doc.get("backedUpAt")),
        "chains": W.supported(),
        "excluded": W.EXCLUDED,
        # Said here as well as in the UI copy, so an API consumer cannot miss it.
        "custody": "This node holds the keys for this wallet.",
    }


@router.post("/create")
async def create(req: CreateReq, user: CurrentUser, db: Session = Depends(get_db)):
    """Make a wallet, or restore one from a phrase.

    REFUSES TO OVERWRITE. A second create on an account that already has a wallet is answered 409,
    never by replacing the row: the old seed would be unrecoverable and the coins behind it gone,
    and "create" is exactly the button somebody presses twice when a page looks unresponsive.
    """
    _library()
    phrase = (req.mnemonic or "").strip()
    if phrase:
        if not W.validate_mnemonic(phrase):
            raise HTTPException(status_code=400, detail=(
                "that is not a valid BIP-39 recovery phrase — check the words and their order"))
    else:
        phrase = W.new_mnemonic()
    try:
        await V.save_new(db, user, phrase, (req.label or "").strip()[:80] or None)
    except V.VaultUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except W.WalletError as exc:
        # "already has a wallet" comes from the vault, which checked by READING — so an unreachable
        # relay raised above rather than letting this look like an empty account.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Never the phrase. An imported wallet is already written down; a generated one is shown once,
    # through /reveal, because the person has to ask for it.
    return {"ok": True, "imported": bool(req.mnemonic), "backedUp": False}


@router.get("/addresses")
async def addresses(user: CurrentUser, db: Session = Depends(get_db)):
    """Receive addresses for every supported chain, at the wallet's current index."""
    _library()
    doc, phrase = await _open(db, user)
    index = int(doc.get("addressIndex") or 0)
    return {"ok": True, "index": index, "addresses": W.addresses(phrase, index)}


@router.get("/balances")
async def balances(user: CurrentUser, db: Session = Depends(get_db)):
    """What the wallet holds, per chain — and which chains could not be asked.

    Every row carries `known`. A client that reads `amount` without it prints "0" for a chain whose
    provider was down, which is the difference between "you have nothing" and "I could not find
    out". Deliberately not cached: a stale balance shown as current is the same lie one refresh
    later.
    """
    _library()
    doc, phrase = await _open(db, user)
    addrs = W.addresses(phrase, int(doc.get("addressIndex") or 0))
    from app.services import exodus_chain_service as C
    return {"ok": True, "index": int(doc.get("addressIndex") or 0),
            "balances": await C.balances(addrs, _settings(db))}


@router.post("/reveal")
async def reveal(user: CurrentUser, db: Session = Depends(get_db)):
    """The recovery phrase, because somebody asked for it.

    A POST on purpose: a GET is linkable, prefetchable and lands in history. Asking also marks the
    wallet backed up, which is what stops the app nagging — and the nag exists because a seed nobody
    has written down is one node failure away from gone.
    """
    _library()
    doc, phrase = await _open(db, user)
    if not doc.get("backedUpAt"):
        await V.update(db, user, backedUpAt=int(datetime.utcnow().timestamp()))
    return {"ok": True, "mnemonic": phrase,
            "warning": "Anyone with these words can spend this wallet. Write them down offline."}


class SendReq(BaseModel):
    symbol: str = Field(min_length=2, max_length=8)
    to: str = Field(min_length=4, max_length=128)
    amount: str = Field(min_length=1, max_length=64)


@router.post("/send")
async def send(req: SendReq, user: CurrentUser, db: Session = Depends(get_db)):
    """Move money. Every refusal below happens BEFORE anything is signed.

    The one answer this route will not give is "it failed" when it does not know. A broadcast that
    timed out comes back as `unsure` with the nonce to look for, because the alternative invites a
    retry that pays twice — the exact mistake this codebase already made once on Monero.
    """
    _library()
    from app.services import exodus_send_service as S
    symbol = str(req.symbol or "").upper().strip()
    spec = W.CHAINS.get(symbol)
    if not spec:
        raise HTTPException(status_code=400, detail=f"unsupported chain {symbol!r}")
    if spec["kind"] != "evm":
        raise HTTPException(status_code=501, detail=(
            f"sending {symbol} is not supported yet. Receiving works; a {symbol} spend needs "
            f"transaction building this wallet does not do, and a half-built one loses coins."))

    doc, phrase = await _open(db, user)
    index = int(doc.get("addressIndex") or 0)
    try:
        units = W.to_base_units(req.amount, symbol)
    except W.WalletError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # THE CEILING, CHECKED HERE AND NOT IN THE CLIENT. A cap a page enforces is a cap anybody can
    # skip by calling the endpoint.
    cap_text = str(_settings().get(f"exodus_cap_{symbol.lower()}", "") or "").strip()
    if cap_text:
        try:
            cap = W.to_base_units(cap_text, symbol)
        except W.WalletError:
            # A misconfigured ceiling must not become NO ceiling.
            raise HTTPException(status_code=503, detail=(
                f"this node's {symbol} per-transfer limit is not a valid amount, so no send can be "
                f"checked against it")) from None
        if units > cap:
            raise HTTPException(status_code=400, detail=(
                f"that is over this node's {symbol} limit of {cap_text} per transfer"))

    from app.services import exodus_chain_service as C
    try:
        got = await S.send_evm(symbol=symbol, private_key=W.private_key_for(phrase, symbol, index),
                               to=req.to, units=units,
                               endpoint=C.endpoint_for(symbol, _settings()),
                               from_address=W.address_for(phrase, symbol, index))
    except S.SendUnsure as exc:
        # 202: taken, outcome unknown. NOT an error status — a client that sees 4xx/5xx offers a
        # retry, and a retry here is a second real payment.
        return JSONResponse(status_code=202, content={"ok": False, "unsure": True, "msg": str(exc)})
    except S.SendRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except W.WalletError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("[exodus] sent %s %s (nonce %s)", req.amount, symbol, got.get("nonce"))
    return {"ok": True, **got}


@router.post("/label")
async def label(req: LabelReq, user: CurrentUser, db: Session = Depends(get_db)):
    if not await _doc(db, user):
        raise HTTPException(status_code=404, detail="no wallet on this account yet")
    doc = await V.update(db, user, label=req.label.strip()[:80] or None)
    return {"ok": True, "label": doc.get("label")}
