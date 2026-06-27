"""Personal plane of the Nostr ↔ Fediverse bridge: each user's own DMs + notifications → Nostr.

Per-user poller (worker process, port 3051). For each user who linked a Pleroma account, linked a
Nostr identity, and opted in (User.fedi_bridge_enabled) — gated additionally on the global
fedi_bridge_enabled — it delivers:

  - **Direct messages** → a NIP-17 gift-wrapped Nostr DM to the user's npub, from the SENDER's puppet
    key, so it lands in their normal encrypted-DM inbox as if the fedi user were on Nostr. A
    FediBridgeMap row lets the user's NIP-17 reply route back (handled by fedi_nostr_writeback).
  - **Mentions** → a public kind-1 from the actor's puppet, p-tagging the user (a real Nostr mention).
    Recorded in FediBridgeDelivered so a Nostr reply federates back like any bridged-note reply.
  - **Favourites / boosts / follows** → a private NIP-17 notice from the actor's puppet ("❤ … liked
    your post"), so they don't pollute the puppet's public feed.

Cursors are per-user (User.fedi_bridge_dm_since / fedi_bridge_notif_since), advanced PER delivered
item so a mid-batch failure can't reflood. Reuses fedi_bridge_identity for puppet provisioning.
"""
import asyncio
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import User, FediBridgeDelivered, FediBridgeMap
from app.services import pleroma_service, settings_store
from app.services import fedi_bridge_identity as ident
from app.services.nostr import nip17
from app.services.fedi_timeline_service import _norm_pleroma

logger = logging.getLogger(__name__)

_POLL_TIMEOUT = 90
_MAX = 20   # items per user per poll


def _get(key: str, default: str = "") -> str:
    v = settings_store.get(key, default)
    return v if v not in (None, "") else default


def _enabled() -> bool:
    return str(_get("fedi_bridge_enabled", "false")).lower() in ("1", "true", "yes", "on")


def _port() -> int:
    try:
        return int(_get("nostr_relay_port", "3052") or "3052")
    except ValueError:
        return 3052


def _user_pubkey(user: User) -> str | None:
    from app.services.nostr import nostr_service
    npub = getattr(user, "nostr_npub", None)
    return nostr_service.to_pubkey_hex(npub) if npub else None


async def _wrap_dm(port: int, puppet: dict, recipient_hex: str, text: str) -> str | None:
    """NIP-17 gift-wrap `text` from the puppet to the user; publish to the local relay. Returns id."""
    try:
        wrap = nip17.wrap(puppet["seckey"], recipient_hex, text)
    except Exception as e:
        logger.debug("[fedi-personal] wrap failed: %s", e)
        return None
    ok, _ = await ident.publish(port, wrap)
    return wrap["id"] if ok else None


async def _deliver_dms(db: Session, port: int, user: User, instance_host: str) -> None:
    since = getattr(user, "fedi_bridge_dm_since", None)
    try:
        raw = await pleroma_service.fetch_direct(user.pleroma_instance_url, user.pleroma_access_token,
                                                 since_id=since, limit=_MAX)
    except Exception as e:
        logger.debug("[fedi-personal] DM fetch failed for %s: %s", user.username, e)
        return
    recipient = _user_pubkey(user)
    if not recipient:
        return
    me = (await _self_acct(user)) or ""
    for st in sorted(raw, key=lambda s: s.get("id") or ""):   # oldest-first
        account = st.get("account") or {}
        post = _norm_pleroma(st)
        # Skip our own outgoing direct statuses.
        if me and (account.get("acct") or "").lower() == me.lower():
            user.fedi_bridge_dm_since = st.get("id") or user.fedi_bridge_dm_since
            db.commit()
            continue
        puppet = await ident.ensure_puppet(db, port, account, instance_host)
        if not puppet:
            continue
        body = (post.get("text") or "").strip()
        for m in (post.get("media") or []):
            if m.get("url"):
                body += ("\n" if body else "") + m["url"]
        wrap_id = await _wrap_dm(port, puppet, recipient, body or "​")
        if wrap_id:
            db.add(FediBridgeMap(user_id=user.id, nostr_event_id=wrap_id, kind="dm",
                                 platform="pleroma", instance_url=user.pleroma_instance_url,
                                 peer_pubkey=puppet["pubkey_hex"], target_id=st.get("id"),
                                 visibility="direct"))
        user.fedi_bridge_dm_since = st.get("id") or user.fedi_bridge_dm_since
        try:
            db.commit()
        except Exception:
            db.rollback()


async def _self_acct(user: User) -> str | None:
    try:
        me = await pleroma_service.verify_credentials(user.pleroma_instance_url, user.pleroma_access_token)
        return (me or {}).get("acct") or (me or {}).get("username")
    except Exception:
        return None


def _delivered_event_for(db: Session, instance_url: str, status: dict) -> str | None:
    """The Nostr event id we already published for a fediverse status, or None."""
    uri = status.get("uri") or status.get("url")
    sid = status.get("id")
    row = None
    if uri:
        row = db.query(FediBridgeDelivered).filter(FediBridgeDelivered.note_uri == uri).first()
    if not row and sid:
        row = (db.query(FediBridgeDelivered)
               .filter(FediBridgeDelivered.instance_url == instance_url,
                       FediBridgeDelivered.note_id == sid).first())
    return row.nostr_event_id if row else None


async def _ensure_status_event(db: Session, port: int, user: User, instance_host: str,
                               status: dict, broadcast: bool) -> str | None:
    """Resolve (or mirror) the Nostr event for a fediverse status so a reaction/repost can reference
    a REAL event. The reacted/boosted status is the user's own post — mirror it under its author's
    puppet if we haven't already, so the notification threads to a concrete note."""
    eid = _delivered_event_for(db, user.pleroma_instance_url, status)
    if eid or not status.get("id"):
        return eid
    try:
        from app.services.fedi_nostr_bridge_service import _deliver
        post = _norm_pleroma(status)
        return await _deliver(db, port, "pleroma", user.pleroma_instance_url, instance_host, status, post)
    except Exception as e:
        logger.debug("[fedi-personal] mirror-for-reaction failed: %s", e)
        return None


async def _deliver_notifications(db: Session, port: int, user: User, instance_host: str) -> None:
    since = getattr(user, "fedi_bridge_notif_since", None)
    try:
        raw = await pleroma_service.fetch_notifications(user.pleroma_instance_url,
                                                        user.pleroma_access_token, since_id=since, limit=_MAX)
    except Exception as e:
        logger.debug("[fedi-personal] notif fetch failed for %s: %s", user.username, e)
        return
    recipient = _user_pubkey(user)
    if not recipient:
        return
    broadcast = str(_get("fedi_bridge_broadcast", "false")).lower() in ("1", "true", "yes", "on")
    for n in sorted(raw, key=lambda x: x.get("id") or ""):   # oldest-first
        account = n.get("account") or {}
        puppet = await ident.ensure_puppet(db, port, account, instance_host)
        if puppet:
            ntype = (n.get("type") or "").lower()
            status = n.get("status") or {}
            # Each fediverse notification → the matching NATIVE Nostr notification event (NOT a DM):
            # mention → kind-1 reply, favourite/reaction → kind-7, boost → kind-6, follow → a brief
            # mention note. All p-tag the user so they surface in the client's notifications tab.
            if ntype == "mention" and status:
                post = _norm_pleroma(status)
                content = (post.get("text") or "").strip()
                for m in (post.get("media") or []):
                    if m.get("url"):
                        content += ("\n" if content else "") + m["url"]
                uri = status.get("uri") or status.get("url")
                ev = ident.build_event(puppet, 1, content or "​", tags=[["p", recipient]],
                                       object_uri=uri, broadcast=broadcast)
                ok, _ = await ident.publish(port, ev)
                if ok and status.get("id"):
                    db.add(FediBridgeDelivered(platform="pleroma", instance_url=user.pleroma_instance_url,
                                               note_id=status["id"], note_uri=uri,
                                               author_acct=puppet["acct"], nostr_event_id=ev["id"],
                                               nostr_pubkey=puppet["pubkey_hex"]))
            elif ntype in ("favourite", "reaction", "emoji_reaction", "pleroma:emoji_reaction") and status:
                target = await _ensure_status_event(db, port, user, instance_host, status, broadcast)
                emoji = n.get("emoji") or "+"
                tags = [["p", recipient]] + ([["e", target]] if target else [])
                await ident.publish(port, ident.build_event(puppet, 7, ("+" if ntype == "favourite" else emoji),
                                                            tags=tags, broadcast=broadcast))
            elif ntype == "reblog" and status:
                target = await _ensure_status_event(db, port, user, instance_host, status, broadcast)
                tags = [["p", recipient]] + ([["e", target]] if target else [])
                await ident.publish(port, ident.build_event(puppet, 6, "", tags=tags, broadcast=broadcast))
            elif ntype in ("follow", "follow_request"):
                # A follow has no note to react to, and a kind-1 "followed you" would pollute the
                # global feed (it isn't a real post). Deliver it as a private NIP-17 notice instead —
                # follows are low-volume, so this won't flood DMs the way reactions did.
                msg = ("➕ @%s followed you" if ntype == "follow" else "➕ @%s requested to follow you") % puppet["acct"]
                await _wrap_dm(port, puppet, recipient, msg)
            # other notification types (poll, update, …) are intentionally not bridged
        user.fedi_bridge_notif_since = n.get("id") or user.fedi_bridge_notif_since
        try:
            db.commit()
        except Exception:
            db.rollback()


async def poll_once(db: Session) -> None:
    if not _enabled():
        return
    port = _port()
    users = db.query(User).filter(User.fedi_bridge_enabled == True,   # noqa: E712
                                  User.pleroma_enabled == True).all()  # noqa: E712
    for user in users:
        if not (user.pleroma_instance_url and user.pleroma_access_token and _user_pubkey(user)):
            continue
        from urllib.parse import urlparse
        instance_host = urlparse(user.pleroma_instance_url).netloc.split(":")[0].lower()
        try:
            await _deliver_dms(db, port, user, instance_host)
            await _deliver_notifications(db, port, user, instance_host)
        except Exception as e:
            logger.warning("[fedi-personal] poll failed for %s: %s", user.username, e)
            db.rollback()


# --- scheduler --------------------------------------------------------------

_scheduler = None


def start_fedi_personal_scheduler() -> None:
    """Start the per-user DMs + notifications poller (idempotent). Call from a running loop."""
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    try:
        secs = max(60, int(_get("fedi_bridge_poll_seconds", "90") or "90"))
    except ValueError:
        secs = 90

    async def _job():
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            await asyncio.wait_for(poll_once(db), timeout=_POLL_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("[fedi-personal] poll exceeded %ss; retrying next cycle", _POLL_TIMEOUT)
            db.rollback()
        except Exception as e:
            logger.warning("[fedi-personal] poll job error: %s", e)
            db.rollback()
        finally:
            db.close()

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_job, "interval", seconds=secs, id="fedi_personal_poll", max_instances=1, coalesce=True)
    _scheduler.start()
    logger.info("[fedi-personal] per-user DM + notification poller started (every %ss)", secs)


def stop_fedi_personal_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception as e:
            logger.warning("[fedi-personal] scheduler shutdown error: %s", e)
        _scheduler = None
