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
from app.services.fedi_nostr_bridge_service import _blocked_domains, _domain_blocked, _host_of

logger = logging.getLogger(__name__)

_POLL_TIMEOUT = 90
_MAX = 20             # items per page
_MAX_PAGES = 5        # bound the forward-drain per user per poll (≈100 items; rest drains next cycle)
_self_acct_cache: dict = {}   # user_id -> own acct (stable; cached so a transient verify failure
                              # can't disable the own-DM skip and echo your sent DMs back to you)


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
    recipient = _user_pubkey(user)
    if not recipient:
        return
    inst, token = user.pleroma_instance_url, user.pleroma_access_token
    since = getattr(user, "fedi_bridge_dm_since", None)
    # First poll: set the cursor to newest WITHOUT delivering, so opting in doesn't flood the inbox
    # with a backlog (mirrors the global/social pollers' no-backfill-on-first-poll invariant).
    if not since:
        try:
            raw = await pleroma_service.fetch_direct(inst, token, limit=_MAX)
        except Exception as e:
            logger.debug("[fedi-personal] DM first-poll failed for %s: %s", user.username, e)
            return
        newest = max((s.get("id") for s in raw if s.get("id")), default=None)
        if newest:
            user.fedi_bridge_dm_since = newest
            db.commit()
        return
    me = await _self_acct(user)
    if me is None:      # can't determine our own handle (transient) → skip rather than echo our own DMs
        return
    blocked = _blocked_domains()
    cursor = since
    for _page in range(_MAX_PAGES):
        try:
            raw = await pleroma_service.fetch_direct(inst, token, min_id=cursor, limit=_MAX)
        except Exception as e:
            logger.debug("[fedi-personal] DM drain failed for %s: %s", user.username, e)
            break
        if not raw:
            break
        last, stop = None, False
        for st in sorted(raw, key=lambda s: s.get("id") or ""):   # oldest-first (forward order)
            account = st.get("account") or {}
            acct = (account.get("acct") or "").lower()
            host = _host_of(acct, instance_host)
            if acct == me.lower() or _domain_blocked(host, blocked):
                last = st.get("id") or last        # our own / a blocked-domain sender → skip (advance)
                continue
            puppet = await ident.ensure_puppet(db, port, account, instance_host)
            if not puppet:
                last = st.get("id") or last
                continue
            post = _norm_pleroma(st)
            body = (post.get("text") or "").strip()
            for m in (post.get("media") or []):
                if m.get("url"):
                    body += ("\n" if body else "") + m["url"]
            wrap_id = await _wrap_dm(port, puppet, recipient, body or "​")
            if not wrap_id:
                stop = True            # publish failed → STOP; don't advance past it, retry next cycle
                break
            db.add(FediBridgeMap(user_id=user.id, nostr_event_id=wrap_id, kind="dm",
                                 platform="pleroma", instance_url=inst,
                                 peer_pubkey=puppet["pubkey_hex"], target_id=st.get("id"),
                                 visibility="direct"))
            last = st.get("id") or last
        if last and last != cursor:
            cursor = last
            user.fedi_bridge_dm_since = cursor
            try:
                db.commit()
            except Exception:
                db.rollback()
        if stop or len(raw) < _MAX:
            break


async def _self_acct(user: User) -> str | None:
    """The user's own fediverse handle, cached per process (it's stable). Cached so a later transient
    verify_credentials failure can't return None and disable the own-DM skip (echoing sent DMs back)."""
    if user.id in _self_acct_cache:
        return _self_acct_cache[user.id]
    try:
        me = await pleroma_service.verify_credentials(user.pleroma_instance_url, user.pleroma_access_token)
        acct = (me or {}).get("acct") or (me or {}).get("username")
    except Exception:
        return None
    if acct:
        _self_acct_cache[user.id] = acct
    return acct


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
        return await _deliver(db, port, "pleroma", user.pleroma_instance_url, instance_host, status,
                              post, token=user.pleroma_access_token)
    except Exception as e:
        logger.debug("[fedi-personal] mirror-for-reaction failed: %s", e)
        return None


async def _deliver_one_notif(db: Session, port: int, user: User, instance_host: str, n: dict,
                             recipient: str, broadcast: bool, blocked: set) -> bool:
    """Deliver ONE notification as the matching native Nostr event. Returns True when delivered OR
    intentionally skipped (cursor may advance), False when a publish failed (cursor must NOT advance
    so it retries next cycle). Honors the admin domain blocklist (the personal plane used to bypass it)."""
    account = n.get("account") or {}
    acct = (account.get("acct") or "").lower()
    if _domain_blocked(_host_of(acct, instance_host), blocked):
        return True                                  # blocked-domain actor → never mirror (advance)
    puppet = await ident.ensure_puppet(db, port, account, instance_host)
    if not puppet:
        return True
    ntype = (n.get("type") or "").lower()
    status = n.get("status") or {}
    try:
        if ntype == "mention" and status:
            post = _norm_pleroma(status)
            vis = (status.get("visibility") or "public").lower()
            if vis in ("direct", "private"):
                # A private/DM mention must NOT become a public note (that leaks the DM). Deliver it
                # privately as a NIP-17 DM from the sender's puppet instead.
                body = (post.get("text") or "").strip()
                for m in (post.get("media") or []):
                    if m.get("url"):
                        body += ("\n" if body else "") + m["url"]
                return bool(await _wrap_dm(port, puppet, recipient, body or "​"))
            # Public/unlisted mention → properly threaded public note (e/p + ancestor backfill) + p-tag.
            from app.services.fedi_nostr_bridge_service import _deliver, _seen, _canonical_uri
            uri = _canonical_uri("pleroma", user.pleroma_instance_url, post)
            if status.get("id") and not _seen(db, user.pleroma_instance_url, status["id"], uri):
                r = await _deliver(db, port, "pleroma", user.pleroma_instance_url, instance_host,
                                   status, post, token=user.pleroma_access_token, extra_ptags=[recipient])
                return r is not None
            return True
        if ntype in ("favourite", "reaction", "emoji_reaction", "pleroma:emoji_reaction") and status:
            target = await _ensure_status_event(db, port, user, instance_host, status, broadcast)
            tags = [["p", recipient]] + ([["e", target]] if target else [])
            if ntype == "favourite":
                content = "+"
            else:
                content = n.get("emoji") or "+"
                # Custom (non-unicode) emoji reaction → NIP-30: content is :shortcode:, tag carries url.
                emoji_url = n.get("emoji_url") or n.get("url")
                if emoji_url:
                    sc = content.strip(":")
                    content = f":{sc}:"
                    tags.append(["emoji", sc, emoji_url])
            ok, _ = await ident.publish(port, ident.build_event(puppet, 7, content, tags=tags, broadcast=broadcast))
            return ok
        if ntype == "reblog" and status:
            target = await _ensure_status_event(db, port, user, instance_host, status, broadcast)
            tags = [["p", recipient]] + ([["e", target]] if target else [])
            ok, _ = await ident.publish(port, ident.build_event(puppet, 6, "", tags=tags, broadcast=broadcast))
            return ok
        if ntype in ("follow", "follow_request"):
            # A fediverse user followed this bridge user → reflect it on Nostr by adding the bridge
            # user to the FOLLOWER puppet's kind-3 contact list, so they appear in the user's Nostr
            # follower list. Maintained INCREMENTALLY (read current list, append) so it never wipes
            # the puppet's existing follows. BEST-EFFORT: a follow failure must NOT block the rest of
            # the drain (always advance the cursor) — losing a single follow on a transient relay
            # hiccup is far better than head-of-line-stalling every later notification.
            await _bridge_follow(db, port, puppet, recipient, broadcast)
            return True
        return True                                  # follow-accepted + untracked types (poll/update/…) → skip
    except Exception as e:
        logger.debug("[fedi-personal] notif deliver failed (%s): %s", ntype, e)
        return True                                  # poison item → skip so the drain can't wedge


_puppet_follows: dict = {}      # follower puppet pubkey -> set of followed pubkeys we've published
                                # (so a momentary empty relay read can never SHRINK the list)


async def _bridge_follow(db: Session, port: int, follower_puppet: dict, followed_pk: str,
                         broadcast: bool) -> bool:
    """Add `followed_pk` to the follower puppet's kind-3 contact list (incrementally — read, union,
    republish) so a fediverse follow shows up in the followed Nostr user's follower list. The union
    of (relay read ∪ what we've published this process) guarantees a SUCCESSFUL-but-empty read can't
    wipe an existing list (the replaceable-list-wipe class). Returns False only on read/publish
    failure — the caller treats follows as best-effort and advances regardless."""
    fpk = follower_puppet["pubkey_hex"]
    ok, cur = await ident.query_one(port, {"authors": [fpk], "kinds": [3], "limit": 1})
    if not ok:
        return False        # couldn't read current list → don't risk wiping it; retry next cycle
    existing = set(_puppet_follows.get(fpk, set()))
    content = ""
    if cur:
        content = cur.get("content", "") or ""
        for t in cur.get("tags", []):
            if t and len(t) >= 2 and t[0] == "p" and t[1]:
                existing.add(t[1])
    if followed_pk in existing:
        _puppet_follows[fpk] = existing
        return True          # already following → nothing to publish
    existing.add(followed_pk)
    ev = ident.build_event(follower_puppet, 3, content, tags=[["p", x] for x in sorted(existing)],
                           broadcast=broadcast)
    pubok, _ = await ident.publish(port, ev)
    if pubok:
        _puppet_follows[fpk] = existing
    return pubok


async def _deliver_notifications(db: Session, port: int, user: User, instance_host: str) -> None:
    recipient = _user_pubkey(user)
    if not recipient:
        return
    inst, token = user.pleroma_instance_url, user.pleroma_access_token
    since = getattr(user, "fedi_bridge_notif_since", None)
    # First poll: set the cursor to newest WITHOUT delivering (no backlog flood on opt-in).
    if not since:
        try:
            raw = await pleroma_service.fetch_notifications(inst, token, limit=_MAX)
        except Exception as e:
            logger.debug("[fedi-personal] notif first-poll failed for %s: %s", user.username, e)
            return
        newest = max((x.get("id") for x in raw if x.get("id")), default=None)
        if newest:
            user.fedi_bridge_notif_since = newest
            db.commit()
        return
    broadcast = str(_get("fedi_bridge_broadcast", "false")).lower() in ("1", "true", "yes", "on")
    blocked = _blocked_domains()
    cursor = since
    for _page in range(_MAX_PAGES):       # min_id forward-drain (no dropped items on bursts >20)
        try:
            raw = await pleroma_service.fetch_notifications(inst, token, min_id=cursor, limit=_MAX)
        except Exception as e:
            logger.debug("[fedi-personal] notif drain failed for %s: %s", user.username, e)
            break
        if not raw:
            break
        last, stop = None, False
        for n in sorted(raw, key=lambda x: x.get("id") or ""):   # oldest-first
            if not await _deliver_one_notif(db, port, user, instance_host, n, recipient, broadcast, blocked):
                stop = True               # publish failed → retry next cycle, don't advance past it
                break
            last = n.get("id") or last
        if last and last != cursor:
            cursor = last
            user.fedi_bridge_notif_since = cursor
            try:
                db.commit()
            except Exception:
                db.rollback()
        if stop or len(raw) < _MAX:
            break


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
