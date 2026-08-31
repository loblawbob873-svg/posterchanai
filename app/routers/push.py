"""Web Push subscription endpoints for the Nostr web-client PWA.

The client fetches the VAPID public key, subscribes with the browser's PushManager, and POSTs the
resulting subscription here (keyed by its Nostr pubkey). The push watcher (nostr_push_service) reads
these rows to deliver mentions/zaps/replies as OS notifications when the app is closed.
"""
import asyncio
import base64
from datetime import datetime
import json
import logging
import re
import secrets
import time

from fastapi import APIRouter, Depends, Request, WebSocket
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import PushSubscription
from app.services import push_service
from app.services import direct_push_service
from app.services.nostr import event as nostr_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/push", tags=["push"])
_DEVICE_ID = re.compile(r"^[A-Za-z0-9._-]{16,128}$")


def _direct_auth(auth_b64: str, pubkey: str, action: str, device_id: str) -> bool:
    """Verify a recent proof bound to this exact mutation and device.

    Reusing the generic self-auth check would prove key ownership but would also let a captured proof
    for an unrelated upload rotate or delete a notification device for five minutes.
    """
    try:
        raw = (auth_b64 or "").encode("ascii")
        raw += b"=" * (-len(raw) % 4)
        ev = json.loads(base64.urlsafe_b64decode(raw))
        return (nostr_event.verify_event(ev)
                and ev.get("pubkey") == pubkey
                and abs(int(ev.get("created_at", 0)) - int(time.time())) <= 300
                and ev.get("content") == f"posterchan-direct:{action}:{device_id}")
    except Exception:
        return False


@router.get("/vapid")
async def vapid_key(db: Session = Depends(get_db)):
    """Public application-server key the browser needs to subscribe (generated + persisted on first call)."""
    _, pub = await push_service.ensure_vapid(db)
    return {"publicKey": pub}


@router.post("/subscribe")
async def subscribe(request: Request, db: Session = Depends(get_db)):
    """Store (or refresh) a browser push subscription for a Nostr pubkey. Idempotent by endpoint.

    `auth` is a base64 Nostr event signed by `pubkey` — proof the caller holds that key. It is NOT
    optional: this endpoint took a pubkey and a delivery endpoint on the caller's word, so anyone
    could register THEIR browser under YOUR npub and receive your notifications from then on. Those
    carry sender display names and, for channel messages, 80 characters of the message body. A read
    of someone's notification stream, for the price of knowing their npub.
    """
    body = await request.json()
    pubkey = (body.get("pubkey") or "").strip().lower()
    if not nostr_event.verify_self_auth(body.get("auth") or "", pubkey):
        return {"ok": False, "error": "auth required"}
    sub = body.get("subscription") or {}
    endpoint = (sub.get("endpoint") or "").strip()
    keys = sub.get("keys") or {}
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    # Native clients use PosterChan Direct below. Keyless endpoints used to mean UnifiedPush; accepting
    # one here would silently restore the third-party transport and its arbitrary-URL POST/SSRF surface.
    if not (pubkey and endpoint and p256dh and auth):
        return {"ok": False, "error": "a complete Web Push subscription is required"}
    if not endpoint.startswith(("http://", "https://")):
        # Name what was rejected. A distributor that hands out something other than an https URL
        # (a nostr: URI, a bare host) is otherwise indistinguishable from a broken registration, and
        # the scheme is the one detail that says which. The rest of the URL is a capability — it is
        # deliberately NOT echoed back or logged.
        from urllib.parse import urlparse
        return {"ok": False,
                "error": f"bad endpoint: expected an http(s) URL, got scheme "
                         f"{(urlparse(endpoint).scheme or '(none)')!r}"}
    # A Web Push endpoint is NOT ours to second-guess: the BROWSER chose it, it is always the vendor's
    # own push service, and pywebpush is what talks to it. Running it through the same guard broke
    # subscribing outright on this deployment, because the LAN DNS here answers fcm.googleapis.com with
    # 0.0.0.0 — which the guard correctly reads as unroutable, and which is every Chrome and Android
    # Chrome user. A protection that rejects the single most common push endpoint is a bug, not safety.
    row = db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).first()
    if row:
        row.pubkey, row.p256dh, row.auth = pubkey, p256dh, auth   # device re-subscribed / rotated keys
        row.transport, row.device_id, row.token_hash = "webpush", None, None
    else:
        db.add(PushSubscription(pubkey=pubkey, endpoint=endpoint, transport="webpush",
                                p256dh=p256dh, auth=auth))
    db.commit()
    return {"ok": True}


@router.post("/direct/register")
async def register_direct(request: Request, db: Session = Depends(get_db)):
    """Create/rotate one first-party Android device token after a device-bound Nostr proof."""
    body = await request.json()
    pubkey = (body.get("pubkey") or "").strip().lower()
    device_id = (body.get("device_id") or "").strip()
    if not _DEVICE_ID.fullmatch(device_id):
        return {"ok": False, "error": "invalid device_id"}
    if not _direct_auth(body.get("auth") or "", pubkey, "register", device_id):
        return {"ok": False, "error": "auth required"}

    token = secrets.token_urlsafe(32)
    digest = direct_push_service.token_digest(token)
    row = db.query(PushSubscription).filter(
        PushSubscription.pubkey == pubkey,
        PushSubscription.device_id == device_id,
        PushSubscription.transport == direct_push_service.TRANSPORT,
    ).first()
    if row:
        old_id = row.id
        row.token_hash = digest
        row.last_seen = datetime.utcnow()
    else:
        old_id = None
        row = PushSubscription(pubkey=pubkey,
                               endpoint=f"direct:{pubkey}:{device_id}",
                               transport=direct_push_service.TRANSPORT,
                               device_id=device_id, token_hash=digest,
                               last_seen=datetime.utcnow())
        db.add(row)
        db.flush()

    # A signed owner may register several phones/tablets, but not grow this table without bound.
    stale = db.query(PushSubscription).filter(
        PushSubscription.pubkey == pubkey,
        PushSubscription.transport == direct_push_service.TRANSPORT,
        PushSubscription.id != row.id,
    ).order_by(PushSubscription.last_seen.desc().nullslast(),
               PushSubscription.created_at.desc()).offset(9).all()
    stale_ids = [s.id for s in stale]
    for s in stale:
        db.delete(s)
    db.commit()
    if old_id is not None:
        direct_push_service.disconnect(old_id)
    for sid in stale_ids:
        direct_push_service.disconnect(sid)
    return {"ok": True, "device_id": device_id, "token": token,
            # Relative on purpose: it resolves correctly through an instance subdomain/reverse proxy
            # without trusting Forwarded headers to construct a bearer-adjacent URL.
            "websocket_url": "/api/push/direct/ws"}


@router.post("/direct/unregister")
async def unregister_direct(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    pubkey = (body.get("pubkey") or "").strip().lower()
    device_id = (body.get("device_id") or "").strip()
    if not _DEVICE_ID.fullmatch(device_id):
        return {"ok": False, "error": "invalid device_id"}
    if not _direct_auth(body.get("auth") or "", pubkey, "unregister", device_id):
        return {"ok": False, "error": "auth required"}
    row = db.query(PushSubscription).filter(
        PushSubscription.pubkey == pubkey,
        PushSubscription.device_id == device_id,
        PushSubscription.transport == direct_push_service.TRANSPORT,
    ).first()
    if row:
        sid = row.id
        db.delete(row)
        db.commit()
        direct_push_service.disconnect(sid)
    return {"ok": True}


@router.websocket("/direct/ws")
async def direct_socket(websocket: WebSocket):
    """Authenticate in the first frame so bearer tokens never enter URLs or access logs."""
    await websocket.accept()
    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        token = hello.get("token") if isinstance(hello, dict) and hello.get("type") == "auth" else ""
        if not isinstance(token, str) or not (32 <= len(token) <= 128):
            await websocket.close(code=4401)
            return
        digest = direct_push_service.token_digest(token)
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            row = db.query(PushSubscription).filter(
                PushSubscription.token_hash == digest,
                PushSubscription.transport == direct_push_service.TRANSPORT,
            ).first()
            if not row:
                await websocket.close(code=4401)
                return
            sid = row.id
            device_id = row.device_id
            row.last_seen = datetime.utcnow()
            db.commit()
        finally:
            db.close()
        await websocket.send_json({"type": "ready", "device_id": device_id})
        await direct_push_service.serve(websocket, sid)
    except Exception:
        try:
            await websocket.close(code=4401)
        except Exception:
            pass


@router.post("/test")
async def test_push(request: Request, db: Session = Depends(get_db)):
    """Send a real notification through the real path, and report exactly what happened.

    "Notifications don't work" is unactionable — for the user and for whoever they ask. Every way this
    breaks is silent: permission never granted, the PWA opened in a Safari tab instead of installed,
    the subscription registered against a different key, the device asleep under a battery setting, a
    push service that has since expired the endpoint. This turns all of that into one answer.

    Deliberately NOT a local `showNotification()` — that proves only that the browser can draw a
    notification, which is never the part that fails. This goes server → push service → device, the
    same journey a real message takes.
    """
    body = await request.json()
    pubkey = (body.get("pubkey") or "").strip().lower()
    if not nostr_event.verify_self_auth(body.get("auth") or "", pubkey):
        return {"ok": False, "error": "auth required"}
    rows = db.query(PushSubscription).filter(PushSubscription.pubkey == pubkey).all()
    if not rows:
        return {"ok": False, "devices": 0,
                "error": "This key has no device registered on this server. Turn notifications on first."}
    payload = {"title": "🔔 Notifications are working",
               "body": "This is the test you just asked for.", "type": "test"}
    delivered, dead = 0, []
    for r in rows:
        sub = direct_push_service.subscription_dict(r)
        if push_service.send(sub, payload):
            delivered += 1
        else:
            dead.append(r)
    # A rejected endpoint is a dead one — the browser dropped the subscription, or it expired. Prune
    # it here so the count the user sees is the truth on the next press rather than a stale hope.
    for r in dead:
        db.delete(r)
    if dead:
        db.commit()
    # Distinguish "your device is gone" from "this SERVER cannot reach the push service" — they need
    # completely different fixes and both otherwise read as "notifications are broken". The second is
    # real and easy to miss: a node whose DNS sinkholes fcm.googleapis.com (ad-blocking resolvers do)
    # can never deliver to Chrome or an Android Chrome PWA, no matter what the phone does.
    # Only when something FAILED, and off the event loop: can_reach does a blocking getaddrinfo that
    # can sit for ~10s against a dead resolver, and this process is a single uvicorn worker — every
    # other request would wait behind a diagnostic nobody needs when delivery already worked.
    reachable = True
    if not delivered and rows:
        from starlette.concurrency import run_in_threadpool
        reachable = await run_in_threadpool(push_service.can_reach, rows[0].endpoint)
    if delivered:
        err = ""
    elif not reachable:
        err = ("This server cannot reach the push service for that device — check the node's DNS "
               "and outbound network, not your phone.")
    else:
        err = "Every registered device rejected the push. Turn notifications off and on again."
    return {"ok": delivered > 0, "devices": len(rows), "delivered": delivered, "expired": len(dead),
            "reachable": reachable, "error": err}


@router.post("/unsubscribe")
async def unsubscribe(request: Request, db: Session = Depends(get_db)):
    """Drop a subscription (user turned notifications off / the browser revoked it)."""
    body = await request.json()
    endpoint = (body.get("endpoint") or "").strip()
    if endpoint:
        db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).delete()
        db.commit()
    return {"ok": True}
