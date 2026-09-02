from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import get_admin_user
from app.models import User
from app.services.monero_user_wallets import zap_fee_percent
from app.services.monero_wallet_service import MoneroWallet, WalletError, atomic_to_xmr, transfer_gate, xmr_to_atomic

# NO PREFIX HERE — main.py mounts this under two of them.
#
# Cloudflare's managed WAF blocks any path containing "monero" as a cryptomining pattern, with a
# 403 served by Cloudflare itself and NO CORS headers on it. The browser then rejects the response
# and the client sees a bare "Failed to fetch". Measured: /api/wallet/monero/status → 403 from
# cloudflare, /api/wallet/foo → 404 from us, /api/walletx/monero/status → 403. The trigger is the
# word, not the route.
#
# It only ever bit users coming through Cloudflare, so it looked like an Android-only bug: the
# operator's browser is on the LAN, where DNS points poster.place at the router and skips Cloudflare
# entirely, while the phone on cellular does not. "It worked on wifi" was the tell.
#
# `/api/wallet/xmr` is the canonical path now. The old one stays mounted because installed clients
# ask for it, and on a node that is NOT behind such a WAF it has always worked fine.
router = APIRouter(tags=["monero-wallet"])
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
        # The configured service fee, so the OPERATOR'S OWN send sheet can say it is in force and
        # that this wallet is not charged. Reported as "when I zap, i see nothing about the fee":
        # true, and correct — the admin sends from the node wallet, which is the one wallet a fee
        # would be taken from and handed straight back. Silence there is indistinguishable from the
        # setting not having saved, which is what actually prompted the question.
        "zap_fee_percent": str(zap_fee_percent()),
    }


@router.get("/sync")
async def sync_state(user: WalletOwner):
    """Deliberately its OWN route, asked for AFTER the wallet has painted. `refresh` does real work
    on a wallet that is behind, and the balance must never wait on it — the screen paints what it
    knows and this fills in the reason a 0 is a 0."""
    try:
        return await _wallet().sync_state()
    except WalletError as exc:
        raise _bad(exc) from exc


class SplitRequest(BaseModel):
    outputs: int = Field(ge=2, le=16)


@router.post("/split")
async def split_outputs(body: SplitRequest, user: WalletOwner):
    """Make the wallet able to pay several people in a row.

    A wallet holding ONE unspent output can send once and then nothing until the change unlocks —
    10 blocks, about twenty minutes. Splitting the balance into N outputs is what lets N tips follow
    each other. It is a real transaction with a real fee, so it is only ever done on request."""
    try:
        return await _wallet().split_outputs(body.outputs)
    except WalletError as exc:
        raise _bad(exc) from exc


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
