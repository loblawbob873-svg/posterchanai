"""Multi-chain wallets scoped to the authenticated account's encrypted namespace.

No route accepts another user's identifier. Recovery words are returned only by the
explicit POST /reveal and /reveal-monero actions, never ordinary status/balance reads.
Logs record assets and transfer amounts, never keys or recovery words.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.routers import auth
from app.services import exodus_vault as V
from app.services import exodus_collections as Collections
from app.services import exodus_wallet_service as W
from app.services import exodus_derivation as D

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/wallet/exodus", tags=["exodus-wallet"])
CurrentUser = Annotated[User, Depends(auth.get_current_user)]
WalletId = Annotated[str, Query(alias="wallet", pattern=r"^(default|[0-9a-f]{32})$")]
Portfolio = Annotated[int, Query(ge=0, le=15)]


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


async def _doc(db: Session, user: User, wallet_id="default"):
    """The account's wallet document, or None. Raises 503 when the relay could not be asked.

    THREE ANSWERS, NOT TWO, and this is the only place they are separated. "There is no wallet",
    "there is one and the relay is unreachable", and "there is one" mean entirely different things
    to the screen above — collapsing the first two is how an app offers to generate a second seed
    over the top of somebody's first.
    """
    try:
        return await Collections.load(db, user, wallet_id)
    except V.VaultUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except W.WalletLocked as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _open(db: Session, user: User, wallet_id="default", portfolio=0) -> tuple[dict, str]:
    doc = await _doc(db, user, wallet_id)
    if not doc:
        raise HTTPException(status_code=404, detail="no wallet on this account yet")
    try:
        Collections.require_portfolio(doc, portfolio)
        D.profile(doc)
        return doc, V.mnemonic_of(db, user, doc)
    except W.WalletLocked as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except W.WalletError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class CreateReq(BaseModel):
    # An imported phrase arrives here and is validated before anything is written. A phrase with one
    # wrong word derives a perfectly valid and completely different wallet, so "looks like words" is
    # not enough — the BIP-39 checksum is the test.
    mnemonic: str | None = Field(default=None, max_length=1024)
    label: str | None = Field(default=None, max_length=80)
    moneroMnemonic: str | None = Field(default=None, max_length=1024)
    derivation: Literal['exodus-v1', 'cloudos-v1'] = 'exodus-v1'


class LabelReq(BaseModel):
    label: str = Field(default="", max_length=80)


@router.get("/status")
async def status(user: CurrentUser, db: Session = Depends(get_db), wallet_id: WalletId = "default", portfolio: Portfolio = 0):
    """Enough to draw the screen, and nothing secret.

    Deliberately answers even when the library is missing, so the client can say why the wallet is
    unavailable instead of showing an empty page.
    """
    doc = await _doc(db, user, wallet_id)
    return {
        "ok": True,
        "library": _have_library(),
        "exists": bool(doc),
        "label": (doc.get("label") if doc else None),
        "addressIndex": (int(doc.get("addressIndex") or 0) if doc else 0),
        "backedUp": bool(doc and doc.get("backedUpAt")),
        "walletId": wallet_id,
        "portfolioId": portfolio,
        "portfolios": Collections.portfolios(doc) if doc else [],
        "chains": W.supported(),
        "excluded": W.EXCLUDED,
        # Said here as well as in the UI copy, so an API consumer cannot miss it.
        "custody": "Server-managed wallet. Export and keep your recovery backup.",
        "derivation": D.profile(doc) if doc else D.EXODUS,
        "separateMoneroBackup": bool(doc and doc.get('moneroRecovery')),
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
        await Collections.create(db, user, phrase, (req.label or "").strip()[:80] or None,
                                 default=True, imported=bool(req.mnemonic), monero_recovery=(req.moneroMnemonic or "").strip() or None,
                                 derivation=req.derivation)
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
async def addresses(user: CurrentUser, db: Session = Depends(get_db), wallet_id: WalletId = "default", portfolio: Portfolio = 0):
    """Receive addresses for every supported chain, at the wallet's current index."""
    _library()
    doc, phrase = await _open(db, user, wallet_id, portfolio)
    index = int(doc.get("addressIndex") or 0)
    addrs = D.addresses(doc, phrase, account=portfolio)
    addrs['XMR'] = W.monero_keys(phrase, portfolio, Collections.monero_recovery(doc, _seckey(db, user), portfolio)).PrimaryAddress()
    return {"ok": True, "index": index, "addresses": addrs}


@router.get("/balances")
async def balances(user: CurrentUser, db: Session = Depends(get_db), wallet_id: WalletId = "default", portfolio: Portfolio = 0, valuation: bool = False):
    """What the wallet holds, per chain — and which chains could not be asked.

    Every row carries `known`. A client that reads `amount` without it prints "0" for a chain whose
    provider was down, which is the difference between "you have nothing" and "I could not find
    out". Background Bitcoin discovery and Monero synchronization expose a timestamp and
    treat expired or incomplete results as unknown.
    """
    _library()
    doc, phrase = await _open(db, user, wallet_id, portfolio)
    addrs = D.addresses(doc, phrase, account=portfolio)
    from app.services import exodus_chain_service as C
    from app.services import exodus_bitcoin_discovery as B
    out = await C.balances({symbol: address for symbol, address in addrs.items() if symbol not in B.SYMBOLS}, _settings(db))
    for symbol in B.SYMBOLS:
        out[symbol] = await B.balance(user.id, doc, phrase, portfolio, _seckey(db, user), _settings(db), symbol=symbol)
    from app.services import exodus_monero as M
    out['XMR'] = await M.balance(user.id, wallet_id, portfolio, phrase, _seckey(db, user), doc.get('moneroHeight', 0), Collections.monero_recovery(doc, _seckey(db, user), portfolio))
    result = {"ok": True, "index": int(doc.get("addressIndex") or 0), "balances": out}
    if valuation:
        from app.services import exodus_portfolio as pricing
        expected = [c['symbol'] for c in W.supported()]
        result['valuation'] = pricing.value({symbol: out.get(symbol, {'known': False}) for symbol in expected}, await pricing.prices())
        result['history'] = await pricing.history(db, user, wallet_id, portfolio, result['valuation'])
    return result


@router.post("/reveal")
async def reveal(user: CurrentUser, db: Session = Depends(get_db), wallet_id: WalletId = "default", portfolio: Portfolio = 0):
    """The recovery phrase, because somebody asked for it.

    A POST on purpose: a GET is linkable, prefetchable and lands in history. Asking also marks the
    wallet backed up, which is what stops the app nagging — and the nag exists because a seed nobody
    has written down is one node failure away from gone.
    """
    _library()
    doc, phrase = await _open(db, user, wallet_id, portfolio)
    if not doc.get("backedUpAt"):
        await Collections.update(db, user, wallet_id, backed_up_at=int(datetime.utcnow().timestamp()))
    return {"ok": True, "mnemonic": phrase,
            "moneroMnemonic": Collections.monero_recovery(doc, _seckey(db, user)),
            "derivation": D.profile(doc),
            "warning": "Anyone with these words can spend this wallet. Write them down offline."}


class SendReq(BaseModel):
    requestId: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    symbol: str = Field(min_length=2, max_length=8)
    to: str = Field(min_length=4, max_length=128)
    amount: str = Field(min_length=1, max_length=64)
    destinationTag: int | None = Field(default=None, ge=0, le=2**32-1, strict=True)


@router.post("/send")
async def send(req: SendReq, user: CurrentUser, db: Session = Depends(get_db), wallet_id: WalletId = "default", portfolio: Portfolio = 0):
    """Move money, preserving uncertain broadcast outcomes across retries.

    The one answer this route will not give is "it failed" when it does not know. A broadcast that
    timed out comes back as `unsure` with the nonce to look for, because the alternative invites a
    retry that pays twice — the exact mistake this codebase already made once on Monero.
    """
    _library()
    from app.services import exodus_send_service as S
    symbol = str(req.symbol or "").upper().strip()
    spec = W.CHAINS.get(symbol)
    if not spec and symbol != "XMR":
        raise HTTPException(status_code=400, detail=f"unsupported chain {symbol!r}")

    doc, phrase = await _open(db, user, wallet_id, portfolio)
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
        if symbol == 'XMR':
            from app.services import exodus_monero as M
            got = await M.send(user.id, wallet_id, portfolio, phrase, _seckey(db, user),
                               req.requestId, req.to, units, doc.get('moneroHeight', 0), Collections.monero_recovery(doc, _seckey(db, user), portfolio))
        else:
            from app.services import exodus_transfers as T
            if spec['kind']=='utxo':
                from app.services import exodus_utxo_send as U
                source=U.scope_address(phrase,symbol,portfolio)
            else:
                source = D.address(phrase, symbol, index=index, account=portfolio, format=D.profile(doc))
            async def execute(before_broadcast):
                if spec['kind']=='utxo':
                    return await U.send(user_id=user.id,doc=doc,phrase=phrase,account=portfolio,key=_seckey(db,user),
                                        symbol=symbol,to=req.to,units=units,settings=_settings(),before_broadcast=before_broadcast)
                if symbol in ('SOL', 'XRP'):
                    from app.services import exodus_account_send as A
                    args = dict(private_key=D.private_key(phrase, symbol, index=index, account=portfolio, format=D.profile(doc)),
                                to=req.to, units=units, endpoint=C.endpoint_for(symbol, _settings()),
                                from_address=source, before_broadcast=before_broadcast)
                    if symbol == 'SOL':
                        return await A.send_solana(**args, request_id=req.requestId)
                    return await A.send_xrp(**args, destination_tag=req.destinationTag)
                return await S.send_evm(symbol=symbol,
                    private_key=D.private_key(phrase, symbol, index=index, account=portfolio, format=D.profile(doc)),
                    to=req.to, units=units, endpoint=C.endpoint_for(symbol, _settings()),
                    from_address=source, before_broadcast=before_broadcast)
            got = await T.send(T.scope(user.id, symbol, source), _seckey(db, user),
                               req.requestId, req.to, units, execute, symbol=symbol, destination_tag=req.destinationTag)
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=f'{symbol} transaction signing is not installed on this node') from exc
    except S.SendUnsure as exc:
        # 202: taken, outcome unknown. NOT an error status — a client that sees 4xx/5xx offers a
        # retry, and a retry here is a second real payment.
        return JSONResponse(status_code=202, content={"ok": False, "unsure": True, "msg": str(exc)})
    except S.SendRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except W.WalletError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("[exodus] sent %s %s (nonce %s)", req.amount, symbol, got.get("nonce"))
    return {"ok": got.get('state') != 'failed', **got}


@router.post("/label")
async def label(req: LabelReq, user: CurrentUser, db: Session = Depends(get_db), wallet_id: WalletId = "default"):
    if not await _doc(db, user, wallet_id):
        raise HTTPException(status_code=404, detail="no wallet on this account yet")
    doc = await Collections.update(db, user, wallet_id, label=req.label.strip()[:80])
    return {"ok": True, "label": doc.get("label")}


@router.get('/wallets')
async def list_wallets(user: CurrentUser, db: Session = Depends(get_db)):
    try:
        return {'wallets': await Collections.list_wallets(db, user)}
    except W.WalletError as error:
        raise HTTPException(503, str(error)) from error


@router.post('/wallets')
async def create_wallet(req: CreateReq, user: CurrentUser, db: Session = Depends(get_db)):
    _library()
    phrase = (req.mnemonic or '').strip()
    if phrase and not W.validate_mnemonic(phrase):
        raise HTTPException(400, 'Invalid recovery phrase; check its words and order')
    try:
        return await Collections.create(db, user, phrase or W.new_mnemonic(), (req.label or '').strip()[:80],
                                        imported=bool(phrase), monero_recovery=(req.moneroMnemonic or "").strip() or None,
                                        derivation=req.derivation)
    except W.WalletError as error:
        raise HTTPException(503, str(error)) from error


class PortfolioReq(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.post('/portfolios')
async def create_portfolio(req: PortfolioReq, user: CurrentUser, db: Session = Depends(get_db),
                           wallet_id: WalletId = 'default'):
    name = req.name.strip()
    if not name:
        raise HTTPException(400, 'Enter a portfolio name')
    try:
        doc = await Collections.update(db, user, wallet_id, new_portfolio=name)
        return {'portfolios': Collections.portfolios(doc)}
    except V.VaultUnavailable as error:
        raise HTTPException(503, str(error)) from error
    except W.WalletError as error:
        raise HTTPException(400, str(error)) from error


@router.post('/reveal-monero')
async def reveal_monero(user: CurrentUser, db: Session = Depends(get_db),
                        wallet_id: WalletId = 'default', portfolio: Portfolio = 0):
    _library()
    doc, phrase = await _open(db, user, wallet_id, portfolio)
    from app.services import exodus_monero as M
    return {'mnemonic': M.recovery_phrase(phrase, portfolio, Collections.monero_recovery(doc, _seckey(db, user), portfolio)),
            'warning': 'Anyone with these words can spend this Monero wallet.'}


class SendStatusReq(BaseModel):
    symbol: str = Field(min_length=2, max_length=8)


@router.post('/send-status')
async def send_status(req: SendStatusReq, user: CurrentUser, db: Session = Depends(get_db),
                      wallet_id: WalletId = 'default', portfolio: Portfolio = 0):
    _library()
    doc, phrase = await _open(db, user, wallet_id, portfolio)
    symbol = req.symbol.upper().strip()
    from app.services import exodus_chain_service as C, exodus_transfers as T, exodus_monero as M
    try:
        if symbol == 'XMR':
            keys = W.monero_keys(phrase, portfolio, Collections.monero_recovery(doc, _seckey(db, user), portfolio))
            return await M.send_status(M.identity(user.id, wallet_id, portfolio, keys.PrimaryAddress()), _seckey(db, user))
        kind=W.CHAINS.get(symbol, {}).get('kind')
        if symbol not in ('SOL', 'XRP') and kind not in ('evm','utxo'):
            raise HTTPException(400, 'Unsupported transfer status asset')
        if kind=='utxo':
            from app.services import exodus_utxo_send as U
            source=U.scope_address(phrase,symbol,portfolio)
        else:
            source = D.address(phrase, symbol, account=portfolio, index=int(doc.get('addressIndex') or 0), format=D.profile(doc))
        return await T.status(T.scope(user.id, symbol, source), _seckey(db, user), symbol, C.endpoint_for(symbol, _settings()))
    except W.WalletError as error:
        raise HTTPException(503, str(error)) from error
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(503, 'Transfer status could not be confirmed') from error
