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
from app.services.nostr import event as nostr_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/push", tags=["push"])


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
    # A UnifiedPush registration (the native APK) is an endpoint and nothing else — there are no
    # VAPID keys to send, and requiring them is what would keep the packaged app silent.
    if not (pubkey and endpoint):
        return {"ok": False, "error": "missing pubkey/endpoint"}
    if not endpoint.startswith(("http://", "https://")):
        # Name what was rejected. A distributor that hands out something other than an https URL
        # (a nostr: URI, a bare host) is otherwise indistinguishable from a broken registration, and
        # the scheme is the one detail that says which. The rest of the URL is a capability — it is
        # deliberately NOT echoed back or logged.
        from urllib.parse import urlparse
        return {"ok": False,
                "error": f"bad endpoint: expected an http(s) URL, got scheme "
                         f"{(urlparse(endpoint).scheme or '(none)')!r}"}
    # SSRF guard, but ONLY for UnifiedPush — the transport where the endpoint is an arbitrary URL this
    # server POSTs to, i.e. the one that could be aimed at http://192.168.x.y/admin/...
    #
    # A Web Push endpoint is NOT ours to second-guess: the BROWSER chose it, it is always the vendor's
    # own push service, and pywebpush is what talks to it. Running it through the same guard broke
    # subscribing outright on this deployment, because the LAN DNS here answers fcm.googleapis.com with
    # 0.0.0.0 — which the guard correctly reads as unroutable, and which is every Chrome and Android
    # Chrome user. A protection that rejects the single most common push endpoint is a bug, not safety.
    if not (p256dh and auth):
        from app.services import settings_store
        from app.services.rss_service import is_safe_host
        # A self-hosted distributor lives on the LAN by definition, so on a node that runs one the
        # guard rejects every registration with a flat "bad endpoint". Opt in per node rather than
        # weakening it for everyone.
        allow_private = str(settings_store.get("push_allow_private_endpoints", "false")).lower() in ("1", "true", "yes")
        if not (allow_private or is_safe_host(endpoint)):
            return {"ok": False,
                    "error": "bad endpoint — if your notification app is self-hosted on your LAN, "
                             "enable 'allow private push endpoints' in Admin settings"}
    row = db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).first()
    if row:
        row.pubkey, row.p256dh, row.auth = pubkey, p256dh, auth   # device re-subscribed / rotated keys
    else:
        db.add(PushSubscription(pubkey=pubkey, endpoint=endpoint, p256dh=p256dh, auth=auth))
    db.commit()
    return {"ok": True}


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
        sub = {"endpoint": r.endpoint, "keys": {"p256dh": r.p256dh, "auth": r.auth}}
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
    reachable = push_service.can_reach(rows[0].endpoint) if rows else True
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
