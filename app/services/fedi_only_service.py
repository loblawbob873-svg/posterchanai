"""Authenticated Fediverse posting without publishing the signed action to a Nostr relay."""
import asyncio
import json
import weakref

from app.models import FediBridgeAction, FediBridgeDelivered, FediOnlyEvent, User
from urllib.parse import urlparse
from app.services.nostr.event import verify_event
from app.services.nostr.nostr_service import to_pubkey_hex
from app.services import fedi_nostr_writeback_service as writeback

MARKER = ["client-mode", "fedi-only"]
SOCIAL_KINDS = frozenset((1, 5, 6, 7, 16, 1068, 1018, 1111, 1311, 30023, 30311))
SUPPORTED_KINDS = frozenset((1, 5, 6, 7, 16))
_locks = weakref.WeakValueDictionary()


def is_private(ev):
    return MARKER in (ev.get("tags") or [])


def suppress_mirror(db, account, instance_host):
    """Our public bridge must not echo an opted-out account back under a puppet key."""
    from app.services.fedi_bridge_identity import acct_of
    acct = acct_of(account, instance_host).lower()
    if not acct:
        return False
    # Verified handles are normalized on write, so this uses the existing account index.
    return db.query(User.id).filter(User.fedi_only.is_(True),
                                    User.pleroma_acct == acct).first() is not None


async def verify_link(user):
    """Record the verified handle before enabling/sending, so mirror suppression is ready first."""
    from app.services.fedi_bridge_identity import acct_of
    account = await writeback.pleroma_service.verify_credentials(user.pleroma_instance_url,
                                                                 user.pleroma_access_token)
    acct = acct_of(account, urlparse(user.pleroma_instance_url).netloc)
    if not acct:
        raise ValueError("Could not identify the connected Fediverse account")
    user.pleroma_acct = acct.lower()


async def route(db, user, ev, *, broadcast_only=False):
    """Return a Nostr route decision or a confirmed Fediverse result. Never publish to Nostr.

    A signed marker survives caches, retries and mode changes. It must never become a
    normal Nostr post when the user later turns Fediverse-only mode off.
    """
    private = is_private(ev)
    if not getattr(user, "fedi_only", False) and not private:
        return {"route": "nostr"}
    if not verify_event(ev) or ev.get("pubkey") != to_pubkey_hex(user.nostr_npub or ""):
        return {"route": "fediverse", "ok": False, "msg": "Account does not own this action"}
    # Explicit cleanup of old public activity still needs a public NIP-09 request.
    # Marked private deletions must remain on the authenticated bridge route.
    if ev.get("kind") == 5 and not private and not ev.get("content"):
        return {"route": "nostr"}
    if broadcast_only:
        return {"route": "fediverse", "ok": False, "msg": "Fediverse-only activity cannot be rebroadcast to Nostr"}
    if not private:
        return {"route": "fediverse", "ok": False, "msg": "Fediverse-only mode is enabled. Reload before posting."}
    if ev.get("kind") not in SUPPORTED_KINDS:
        return {"route": "fediverse", "ok": False, "msg": "This post type is not supported in Fediverse-only mode"}
    if not user.pleroma_instance_url or not user.pleroma_access_token:
        return {"route": "fediverse", "ok": False, "msg": "Connect your Fediverse account in User Settings first"}
    if not writeback._bridge_on() or ev["pubkey"] in writeback._blocked_pubkeys():
        return {"route": "fediverse", "ok": False, "msg": "Fediverse bridge access is unavailable"}
    # Serialize one user's actions in this process, including duplicate HTTP retries.
    # Status creation also carries the event id as Mastodon's Idempotency-Key.
    key = user.id
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    async with lock:
        if not user.pleroma_acct:
            try:
                await verify_link(user)
                db.commit()
            except Exception:
                db.rollback()
                return {"route": "fediverse", "ok": False, "msg": "Could not verify your Fediverse account. Reconnect in User Settings."}
        if ev["kind"] == 5:
            ok = await writeback._delete_federated(db, user, ev)
        else:
            target = writeback._target_row(db, ev)
            if (ev["kind"] != 1 or writeback._is_reply(ev)) and not target:
                return {"route": "fediverse", "ok": False, "msg": "This post has no Fediverse bridge target"}
            await writeback._handle(db, ev, private_user=user)
            model = FediBridgeDelivered if ev["kind"] == 1 else FediBridgeAction
            row = db.query(model).filter(model.nostr_event_id == ev["id"],
                                         model.nostr_pubkey == ev["pubkey"]).first()
            ok = bool(row)
        if ok:
            if ev["kind"] == 5:
                ids = [t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "e"]
                db.query(FediOnlyEvent).filter(FediOnlyEvent.user_id == user.id,
                    FediOnlyEvent.id.in_(ids)).update({"deleted": True}, synchronize_session=False)
            if not db.get(FediOnlyEvent, ev["id"]):
                db.add(FediOnlyEvent(id=ev["id"], user_id=user.id, created_at=ev["created_at"],
                                    raw=json.dumps(ev), deleted=False))
            db.commit()
        return {"route": "fediverse", "ok": ok,
                "msg": "" if ok else "Fediverse delivery failed. Nothing was published to Nostr."}
