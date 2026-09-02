"""Per-user Monero wallet routes — a wallet for the person signed in, not for the operator.

Every route here is scoped to the CALLER'S OWN pubkey, taken from their session. No route accepts a
pubkey or an account index as input: an index is an integer somebody could get wrong, and a pubkey
parameter is an invitation to spend another person's money by typing theirs.

The operator's own wallet lives in monero_wallet.py behind `get_admin_user` and is a different
daemon on a different port. Nothing here can reach it.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.routers import auth
from app.services.monero_user_wallets import user_wallets, zap_fee_percent
from app.services.monero_wallet_service import WalletError, xmr_to_atomic

router = APIRouter(tags=["monero-user-wallet"])
CurrentUser = Annotated[User, Depends(auth.get_current_user)]


def _bad(exc: WalletError) -> HTTPException:
    status = 503 if "unavailable" in str(exc) or "busy" in str(exc) else 400
    return HTTPException(status_code=status, detail=str(exc))


def _pubkey(user: User) -> str:
    """The caller's own key, and the ONLY thing that selects a wallet.

    `User.nostr_npub` holds the hex pubkey despite its name (see the model). An account with no key
    gets a clear refusal rather than a wallet keyed on an empty string, which would be one shared
    wallet for everybody who has not signed in with Nostr."""
    key = str(getattr(user, "nostr_npub", "") or "").strip().lower()
    if not key:
        raise HTTPException(status_code=400,
                            detail="This account has no Nostr key, so it cannot have a wallet")
    return key


class Payment(BaseModel):
    address: str = Field(min_length=95, max_length=106)
    amount: str = Field(min_length=1, max_length=32)


class PayRequest(BaseModel):
    #: Fifteen is what one Monero transaction can carry; beyond that the service batches. The cap
    #: here is a sanity bound on a single request, not a limit on how many people can be tipped.
    payments: list[Payment] = Field(min_length=1, max_length=60)


class WithdrawRequest(BaseModel):
    address: str = Field(min_length=95, max_length=106)


@router.get("/status")
async def status(user: CurrentUser):
    """Whether this node offers per-user wallets at all. Cheap: no RPC.

    Carries the service fee so the tip sheet can state it BEFORE anybody sends. A cut a payer only
    discovers afterwards, by noticing the recipient got less than they chose, is indistinguishable
    from the wallet being broken — which is a support question at best and an accusation at worst."""
    fee = zap_fee_percent()
    return {"enabled": user_wallets.enabled(), "network": user_wallets.network,
            "fee_percent": str(fee) if fee > 0 else "0"}


@router.get("/address")
async def address(user: CurrentUser, db: Session = Depends(get_db)):
    """This user's receiving address, creating the wallet the first time they ask."""
    try:
        return {"address": await user_wallets.address(_pubkey(user))}
    except WalletError as exc:
        raise _bad(exc) from exc


@router.get("/balance")
async def balance(user: CurrentUser):
    try:
        return await user_wallets.balance(_pubkey(user))
    except WalletError as exc:
        raise _bad(exc) from exc


@router.post("/pay")
async def pay(body: PayRequest, user: CurrentUser):
    """Tip one person or many — in as few transactions as Monero allows.

    Amounts are parsed as exact decimals, never floats: a tip is money and 0.1 + 0.2 must not become
    0.30000000000000004 on its way to somebody's wallet."""
    try:
        payments = [(p.address, xmr_to_atomic(p.amount)) for p in body.payments]
        return await user_wallets.pay(_pubkey(user), payments)
    except WalletError as exc:
        raise _bad(exc) from exc


@router.post("/withdraw")
async def withdraw(body: WithdrawRequest, user: CurrentUser):
    """Take everything out, to an address the user names.

    Present from the first commit on purpose: custody without a way out is not a wallet, it is an
    IOU. Nothing else in this router matters if this one does not work."""
    try:
        return await user_wallets.withdraw(_pubkey(user), body.address)
    except WalletError as exc:
        raise _bad(exc) from exc
