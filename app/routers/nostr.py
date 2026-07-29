"""Nostr account linking — connect by pasting a secret key (no OAuth/instance).

Unlike Pleroma there is no instance URL or browser flow: the user pastes
their secret key (nsec/hex), optionally customizes relays and the media host, and
we derive + store the npub. The secret key is stored like the other platform
credentials (per-node SQLite); it is never returned to the client.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.auth import get_current_user
from app.services.nostr import nostr_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/nostr", tags=["nostr"])


class NostrConnectRequest(BaseModel):
    secret_key: str                       # nsec1… or 64-char hex
    relays: str | None = None             # comma/newline list; blank = defaults
    media_service: str | None = None      # "blossom" | "nip96"
    media_endpoint: str | None = None     # blank = service default


class NostrConnectResponse(BaseModel):
    npub: str
    relays: list[str]


@router.post("/connect", response_model=NostrConnectResponse)
async def connect_nostr(
    data: NostrConnectRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Validate + store a user's Nostr secret key and posting config."""
    try:
        seckey = nostr_service.decode_seckey(data.secret_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    npub = nostr_service.npub_of(nostr_service.derive_pubkey(seckey))
    relays = nostr_service.relay.normalize_relays(data.relays) if data.relays else []
    media_service = (data.media_service or "blossom").lower()
    if media_service not in ("blossom", "nip96"):
        raise HTTPException(status_code=400, detail="media_service must be 'blossom' or 'nip96'")

    current_user.nostr_enabled = True
    current_user.nostr_nsec = data.secret_key.strip()
    current_user.nostr_npub = npub
    current_user.nostr_relays = "\n".join(relays) if relays else None
    current_user.nostr_media_service = media_service
    current_user.nostr_media_endpoint = (data.media_endpoint or "").strip() or None
    db.commit()

    return NostrConnectResponse(npub=npub, relays=relays or nostr_service.DEFAULT_RELAYS)


@router.post("/disconnect")
async def disconnect_nostr(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove the user's Nostr credentials + notification cursor (clean reconnect)."""
    current_user.nostr_enabled = False
    current_user.nostr_nsec = None
    current_user.nostr_npub = None
    current_user.nostr_relays = None
    current_user.nostr_media_service = None
    current_user.nostr_media_endpoint = None
    current_user.nostr_notif_since = None
    db.commit()
    return {"ok": True, "message": "Nostr account disconnected"}


@router.post("/backfill-relay")
async def backfill_to_relay(
    current_user: User = Depends(get_current_user),
):
    """Sync the user's own Nostr post history into the built-in relay (User Settings button).
    Writes straight to the relay store, so the user's old posts are NOT re-broadcast."""
    nsec = getattr(current_user, "nostr_nsec", None)
    if not nsec:
        raise HTTPException(status_code=400, detail="Connect a Nostr key first.")
    try:
        pk = nostr_service.derive_pubkey(nostr_service.decode_seckey(nsec))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    from app.services.nostr_relay.thread import trigger_backfill
    result = trigger_backfill(pk)
    if not result.get("ok"):
        raise HTTPException(status_code=503,
                            detail=result.get("error") or "relay not available")
    return {"ok": True, "message": "Your post history is syncing to the relay in the background."}
