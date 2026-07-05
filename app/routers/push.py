"""Web Push subscription endpoints for the Nostr web-client PWA.

The client fetches the VAPID public key, subscribes with the browser's PushManager, and POSTs the
resulting subscription here (keyed by its Nostr pubkey). The push watcher (nostr_push_service) reads
these rows to deliver mentions/zaps/replies as OS notifications when the app is closed.
"""
import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import PushSubscription
from app.services import push_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/push", tags=["push"])


@router.get("/vapid")
async def vapid_key(db: Session = Depends(get_db)):
    """Public application-server key the browser needs to subscribe (generated + persisted on first call)."""
    _, pub = await push_service.ensure_vapid(db)
    return {"publicKey": pub}


@router.post("/subscribe")
async def subscribe(request: Request, db: Session = Depends(get_db)):
    """Store (or refresh) a browser push subscription for a Nostr pubkey. Idempotent by endpoint."""
    body = await request.json()
    pubkey = (body.get("pubkey") or "").strip().lower()
    sub = body.get("subscription") or {}
    endpoint = (sub.get("endpoint") or "").strip()
    keys = sub.get("keys") or {}
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not (pubkey and endpoint and p256dh and auth):
        return {"ok": False, "error": "missing pubkey/endpoint/keys"}
    row = db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).first()
    if row:
        row.pubkey, row.p256dh, row.auth = pubkey, p256dh, auth   # device re-subscribed / rotated keys
    else:
        db.add(PushSubscription(pubkey=pubkey, endpoint=endpoint, p256dh=p256dh, auth=auth))
    db.commit()
    return {"ok": True}


@router.post("/unsubscribe")
async def unsubscribe(request: Request, db: Session = Depends(get_db)):
    """Drop a subscription (user turned notifications off / the browser revoked it)."""
    body = await request.json()
    endpoint = (body.get("endpoint") or "").strip()
    if endpoint:
        db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).delete()
        db.commit()
    return {"ok": True}
