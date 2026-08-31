from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import get_admin_user
from app.models import User
from app.services.monero_wallet_service import MoneroWallet, WalletError, atomic_to_xmr, transfer_gate, xmr_to_atomic

router = APIRouter(prefix="/api/wallet/monero", tags=["monero-wallet"])
WalletOwner = Annotated[User, Depends(get_admin_user)]


@router.get("/status")
async def wallet_status(user: WalletOwner):
    wallet = _wallet()
    return {
        "network": wallet.config.network,
        "mainnet": wallet.config.network == "mainnet",
        "transfer_cap": atomic_to_xmr(wallet.config.transfer_cap_atomic),
        "daily_cap": atomic_to_xmr(wallet.config.daily_cap_atomic),
        "warning": "MAINNET hot wallet — keep only small tipping funds here" if wallet.config.network == "mainnet"
                   else "Stagenet testing wallet — funds have no value",
    }


@router.get("/node-status")
async def node_status(user: WalletOwner):
    try:
        return await _wallet().node_status()
    except WalletError as exc:
        raise _bad(exc) from exc


class PaymentRequest(BaseModel):
    address: str = Field(min_length=95, max_length=106)
    amount: str = Field(min_length=1, max_length=32)
    description: str = Field(default="", max_length=140)


class ConfirmRequest(BaseModel):
    confirmation: str = Field(min_length=32, max_length=128)


def _wallet() -> MoneroWallet:
    try:
        return MoneroWallet()
    except WalletError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _bad(exc: WalletError) -> HTTPException:
    status = 503 if "unavailable" in str(exc) else 400
    return HTTPException(status_code=status, detail=str(exc))


@router.get("/balance")
async def balance(user: WalletOwner):
    try:
        return await _wallet().balance()
    except WalletError as exc:
        raise _bad(exc) from exc


@router.get("/address")
async def address(user: WalletOwner):
    try:
        return await _wallet().address()
    except WalletError as exc:
        raise _bad(exc) from exc


@router.get("/history")
async def history(user: WalletOwner, limit: int = 50):
    try:
        return await _wallet().history(limit=limit)
    except WalletError as exc:
        raise _bad(exc) from exc


@router.post("/make-uri")
async def make_uri(body: PaymentRequest, user: WalletOwner):
    try:
        return await _wallet().make_uri(body.address, xmr_to_atomic(body.amount), body.description)
    except WalletError as exc:
        raise _bad(exc) from exc


@router.post("/transfer/prepare")
async def prepare_transfer(body: PaymentRequest, user: WalletOwner):
    try:
        wallet = _wallet()
        amount = xmr_to_atomic(body.amount)
        token, expires = await transfer_gate.prepare(wallet, user.id, body.address, amount)
        return {"confirmation": token, "expires_at": expires, "address": body.address, "amount_atomic": amount}
    except WalletError as exc:
        raise _bad(exc) from exc


@router.post("/transfer/confirm")
async def confirm_transfer(body: ConfirmRequest, user: WalletOwner):
    try:
        return await transfer_gate.confirm(_wallet(), user.id, body.confirmation)
    except WalletError as exc:
        raise _bad(exc) from exc
