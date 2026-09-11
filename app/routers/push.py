"""Web Push subscription endpoints for the Nostr web-client PWA.

The client fetches the VAPID public key, subscribes with the browser's PushManager, and POSTs the
resulting subscription here (keyed by its Nostr pubkey). The push watcher (nostr_push_service) reads
these rows to deliver mentions/zaps/replies as OS notifications when the app is closed.
"""
import asyncio
import base64
from datetime import datetime, timedelta
import json
import logging
import re
import secrets
import time

from fastapi import APIRouter, Depends, Request, WebSocket
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import PushSubscription, PushSentWrap
from app.services import push_service
from app.services import direct_push_service
from app.services import push_prefs
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


@router.post("/prefs")
async def set_prefs(request: Request, db: Session = Depends(get_db)):
    """Mirror the app's notification toggles onto this account's push devices.

    The toggles themselves live in a kind-30078 document encrypted to the user's own key, so this
    node cannot read them and the push watcher would otherwise have to send everything — which it
    did. The client is the only party that can read that document, so it is the one that tells us.

    Authenticated exactly like /subscribe: without it, knowing somebody's npub would be enough to
    switch their notifications off, and a silenced alert is invisible to the person it belonged to.
    """
    body = await request.json()
    pubkey = (body.get("pubkey") or "").strip().lower()
    if not nostr_event.verify_self_auth(body.get("auth") or "", pubkey, "push-prefs"):
        return {"ok": False, "error": "auth required"}
    prefs = push_prefs.clean(body.get("prefs"))
    rows = db.query(PushSubscription).filter(PushSubscription.pubkey == pubkey)
    # NAMING THE DEVICE IS THE WHOLE POINT. These preferences are per-device on purpose — a phone
    # set to mentions-only must not silence the same events on a desktop — so a caller says which
    # of its owner's devices it is: `device_id` for the native transport, `endpoint` for Web Push
    # (whose rows carry no device id). Neither given is an explicit "all my devices".
    device_id = (body.get("device_id") or "").strip()
    endpoint = (body.get("endpoint") or "").strip()
    if device_id:
        if not _DEVICE_ID.fullmatch(device_id):
            return {"ok": False, "error": "invalid device_id"}
        rows = rows.filter(PushSubscription.device_id == device_id)
    elif endpoint:
        rows = rows.filter(PushSubscription.endpoint == endpoint)
    updated = 0
    for row in rows.all():
        row.prefs = json.dumps(prefs)
        updated += 1
    db.commit()
    return {"ok": True, "devices": updated}


#: A send is followed by its push within seconds. Anything unclaimed after this never will be, and
#: keeping it longer would start silencing real messages that happen to reuse an id (they cannot,
#: but the window is the thing that bounds the damage if anything else here is ever wrong).
_SENT_TTL_SECONDS = 600

#: How many wrap ids one call may report. A NIP-17 message is two wraps; a generous ceiling still
#: makes this useless as a way to fill the table.
_SENT_MAX_IDS = 50
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@router.post("/sent")
async def note_sent_wraps(request: Request, db: Session = Depends(get_db)):
    """These gift wraps are MINE — do not push me about them.

    Reported as "if I send a DM, i do not want a push notification saying that somebdy sent a DM —
    it was me!". NIP-17 publishes a SELF-COPY of every message so the sender's other devices see
    what they sent, and it is p-tagged to the sender, whose wrap author is an ephemeral throwaway
    key. The watcher's "don't notify the author" test cannot see that the recipient IS the sender.

    The publishing device already drops the push when it recognises the id (ClientNotified), which
    is exact and needs nothing from this node — but only on the device that sent it. Send from the
    desktop and the phone still buzzed, because the phone published nothing and knows nothing. And
    Web Push has no equivalent of that map at all, so every browser device was told regardless.

    So the account keeps the record and the push is never sent. Authenticated like /prefs: without
    it, knowing an npub would be enough to suppress somebody's real messages, and a silenced alert
    is invisible to the person it belonged to.
    """
    body = await request.json()
    pubkey = (body.get("pubkey") or "").strip().lower()
    if not nostr_event.verify_self_auth(body.get("auth") or "", pubkey, "push-sent"):
        return {"ok": False, "error": "auth required"}
    raw = body.get("ids")
    ids = [str(i).strip().lower() for i in raw][:_SENT_MAX_IDS] if isinstance(raw, list) else []
    ids = [i for i in ids if _HEX64.match(i)]
    if not ids:
        return {"ok": True, "noted": 0}
    # Housekeeping on the write path: a send is followed by its push within seconds, so anything
    # older than the window is bookkeeping rather than evidence. Keeps the table from ever growing.
    cutoff = datetime.utcnow() - timedelta(seconds=_SENT_TTL_SECONDS)
    db.query(PushSentWrap).filter(PushSentWrap.created_at < cutoff).delete(synchronize_session=False)
    have = {r.wrap_id for r in db.query(PushSentWrap.wrap_id)
            .filter(PushSentWrap.pubkey == pubkey, PushSentWrap.wrap_id.in_(ids)).all()}
    for wid in ids:
        if wid in have:
            continue
        db.add(PushSentWrap(pubkey=pubkey, wrap_id=wid))
    db.commit()
    return {"ok": True, "noted": len(set(ids) - have)}


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
    device_id = body.get("device_id")
    if device_id is not None and (not isinstance(device_id, str) or not _DEVICE_ID.fullmatch(device_id)):
        return {"ok": False, "error": "invalid device_id"}
    query = db.query(PushSubscription).filter(PushSubscription.pubkey == pubkey)
    if device_id:
        query = query.filter(PushSubscription.device_id == device_id,
                             PushSubscription.transport == direct_push_service.TRANSPORT)
    rows = query.all()
    if not rows:
        return {"ok": False, "devices": 0,
                "error": "This device is not registered on this server. Turn notifications off and on again."}
    payload = {"title": "🔔 Notifications are working",
               "body": "This is the test you just asked for.", "type": "test"}
    # Network/DB work must not block the socket loop that delivers Direct notifications.
    from starlette.concurrency import run_in_threadpool
    accepted = queued = failed = 0
    dead = []
    for row in rows:
        result = await run_in_threadpool(push_service.send_result,
                                        direct_push_service.subscription_dict(row), payload)
        if result == "accepted":
            accepted += 1
        elif result == "queued":
            queued += 1
        elif result == "expired":
            dead.append(row)
        else:
            failed += 1
    for row in dead:
        db.delete(row)
    if dead:
        db.commit()
    error = ""
    if not accepted and not queued:
        error = ("The server could not send the test. Your registration was kept; try again shortly."
                 if failed else "The registration expired. Turn notifications off and on again.")
    return {"ok": bool(accepted or queued), "devices": len(rows),
            "accepted": accepted, "queued": queued, "failed": failed, "expired": len(dead),
            "delivered": accepted + queued,  # compatibility for older installed clients
            "error": error}


@router.post("/unsubscribe")
async def unsubscribe(request: Request, db: Session = Depends(get_db)):
    """Drop a subscription (user turned notifications off / the browser revoked it)."""
    body = await request.json()
    endpoint = (body.get("endpoint") or "").strip()
    if endpoint:
        db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).delete()
        db.commit()
    return {"ok": True}
