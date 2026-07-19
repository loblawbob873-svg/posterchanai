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
import time
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import User, FediBridgeDelivered, FediBridgeMap
from app.services import pleroma_service, settings_store
from app.services import fedi_bridge_identity as ident
from app.services.nostr import nip17
from app.services.fedi_timeline_service import _norm_pleroma
from app.services.fedi_nostr_bridge_service import _blocked_domains, _domain_blocked, _host_of, _is_public_audience

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


def _persist_cursor(user_id: int, attr: str, value) -> None:
    """Persist a poll cursor in a FRESH session (shared commit_in_fresh_session helper) so forward
    progress sticks even when the long poll transaction is killed by Postgres
    idle_in_transaction_session_timeout and its cursor commit rolls back (the stuck-cursor wedge)."""
    from app.database import commit_in_fresh_session

    def _mut(s):
        u = s.get(User, user_id)
        if u is not None:
            setattr(u, attr, value)
    commit_in_fresh_session(_mut)


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
            _persist_cursor(user.id, "fedi_bridge_dm_since", cursor)   # survive a killed poll txn
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
            if not _is_public_audience(status):
                # Any non-public-audience mention (direct/private DM, followers-only, Misskey specified/
                # followers, or unknown) must NOT become a public note — that leaks it. Same allowlist
                # guard as the mirror. Deliver it privately as a NIP-17 DM from the sender's puppet instead.
                body = (post.get("text") or "").strip()
                for m in (post.get("media") or []):
                    if m.get("url"):
                        body += ("\n" if body else "") + m["url"]
                return bool(await _wrap_dm(port, puppet, recipient, body or "​"))
            # Public/unlisted mention → properly threaded public note (e/p + ancestor backfill) + p-tag.
            from app.services.fedi_nostr_bridge_service import _deliver, _seen, _canonical_uri, _PublishFailed
            uri = _canonical_uri("pleroma", user.pleroma_instance_url, post)
            if status.get("id") and not _seen(db, user.pleroma_instance_url, status["id"], uri):
                try:
                    r = await _deliver(db, port, "pleroma", user.pleroma_instance_url, instance_host,
                                       status, post, token=user.pleroma_access_token, extra_ptags=[recipient])
                except _PublishFailed:
                    return False   # TRANSIENT relay failure (flap/disconnect) — retry next poll, never drop
                # r is None here only for a PERMANENT non-delivery (blocked author / oversized / already
                # mirrored): those never succeed, so advance past — do NOT feed them to the retry/skip
                # machinery (which is only for transient failures). r not None = delivered → also advance.
                return True
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
            ok, msg = await ident.publish(port, ident.build_event(puppet, 7, content, tags=tags, broadcast=broadcast))
            return ok or _is_permanent_reject(msg)   # delivered / permanently rejected → advance; transient → retry
        if ntype == "reblog" and status:
            target = await _ensure_status_event(db, port, user, instance_host, status, broadcast)
            tags = [["p", recipient]] + ([["e", target]] if target else [])
            ok, msg = await ident.publish(port, ident.build_event(puppet, 6, "", tags=tags, broadcast=broadcast))
            return ok or _is_permanent_reject(msg)   # delivered / permanently rejected → advance; transient → retry
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
_notif_poison: dict = {}        # user_id -> {"id": notif id, "fails": consecutive-failure count} for the
                                # item currently stuck at the head of the drain. A False from _deliver_one_notif
                                # is now ONLY a TRANSIENT failure (permanent non-deliveries advance in-place),
                                # so the default is to keep retrying — a brief relay flap must NEVER drop a
                                # notification (the old 2-strike drop silently ate mentions during a flap). Only
                                # after _POISON_MAX_FAILS consecutive cycles of the SAME item failing — i.e. it's
                                # genuinely wedging the drain, not a transient blip — do we skip past it so newer
                                # notifications aren't starved forever. Any success clears the streak.
_POISON_MAX_FAILS = 20          # ~20 min of continuous same-item failure before skipping (vs a flap of 1-3)


def _is_permanent_reject(msg: str) -> bool:
    """A relay OK-false whose reason is PERMANENT (the item will never be accepted) vs a transient/
    connection failure. Mirrors fedi_nostr_bridge_service._deliver's classifier so a permanently-rejected
    reaction/reblog advances immediately instead of wedging the drain for _POISON_MAX_FAILS cycles.
    Blocked = author/content not accepted (e.g. not-in-WoT); invalid = bad id/sig/expired; duplicate =
    already stored. Everything else (connection drop, 'not stored, retry') is transient → keep retrying."""
    return (msg or "").lower().startswith(("blocked", "invalid", "duplicate"))


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


_backfill_inflight: set = set()   # user_ids whose one-time follower backfill is running
_backfill_tasks: set = set()      # strong refs so a fire-and-forget task isn't GC'd mid-run
_BACKFILL_RETRY_SEC = 3600        # after a fetch failure, don't re-attempt for this long (no every-cycle re-scan)


def _user_flag(db: Session, user_id: int, key: str) -> str | None:
    from app.models import UserSetting
    row = db.query(UserSetting).filter(UserSetting.user_id == user_id, UserSetting.key == key).first()
    return row.value if row else None


def _set_user_flag(db: Session, user_id: int, key: str, value: str) -> None:
    from app.models import UserSetting
    row = db.query(UserSetting).filter(UserSetting.user_id == user_id, UserSetting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(UserSetting(user_id=user_id, key=key, value=value))
    db.commit()


def _backfill_due(db: Session, user_id: int) -> bool:
    """Whether the one-time follower backfill should run for this user: not yet done AND not inside a
    post-failure retry cooldown."""
    if _user_flag(db, user_id, "fedi_followers_backfilled") == "1":
        return False
    after = _user_flag(db, user_id, "fedi_followers_backfill_after")
    if after:
        try:
            if time.time() < float(after):
                return False
        except (TypeError, ValueError):
            pass
    return True


async def _backfill_followers(db: Session, port: int, user: User, instance_host: str) -> None:
    """ONE-TIME: mirror the user's EXISTING fediverse followers onto Nostr as puppet kind-3 follows,
    so their real follower count shows (the notification-driven path only catches NEW follows, and the
    first poll sets its cursor without replaying the backlog). Paced + idempotent (_bridge_follow
    unions). The done flag is set ONLY on a real success; a fetch failure schedules a retry cooldown
    instead so we neither re-scan every cycle nor burn the flag on a transient error."""
    recipient = _user_pubkey(user)
    if not recipient:
        return
    inst, token = user.pleroma_instance_url, user.pleroma_access_token
    try:
        me = await pleroma_service.verify_credentials(inst, token)
        acct_id = (me or {}).get("id")
        expected = int((me or {}).get("followers_count") or 0)
        followers = await pleroma_service.fetch_followers(inst, token, acct_id) if acct_id else []
    except Exception as e:
        logger.warning("[fedi-personal] follower-backfill fetch failed for %s: %s — retrying in %dm",
                       user.username, e, _BACKFILL_RETRY_SEC // 60)
        _set_user_flag(db, user.id, "fedi_followers_backfill_after", str(int(time.time()) + _BACKFILL_RETRY_SEC))
        return
    # The instance says we HAVE followers but we fetched none → transient failure (fetch_followers
    # swallows non-200s), so don't mark done — back off and retry.
    if expected > 0 and not followers:
        logger.warning("[fedi-personal] follower-backfill got 0/%d for %s (transient?) — retrying in %dm",
                       expected, user.username, _BACKFILL_RETRY_SEC // 60)
        _set_user_flag(db, user.id, "fedi_followers_backfill_after", str(int(time.time()) + _BACKFILL_RETRY_SEC))
        return
    broadcast = str(_get("fedi_bridge_broadcast", "false")).lower() in ("1", "true", "yes", "on")
    blocked = _blocked_domains()
    done = 0
    for i, account in enumerate(followers):
        acct = (account.get("acct") or "").lower()
        if _domain_blocked(_host_of(acct, instance_host), blocked):
            continue
        try:
            puppet = await ident.ensure_puppet(db, port, account, instance_host)
            if puppet and await _bridge_follow(db, port, puppet, recipient, broadcast):
                done += 1
        except Exception as e:
            logger.debug("[fedi-personal] follower-backfill entry failed: %s", e)
        if i % 10 == 9:
            await asyncio.sleep(1)   # pace: don't blast the relay with one big burst
    _set_user_flag(db, user.id, "fedi_followers_backfilled", "1")
    logger.info("[fedi-personal] backfilled %d/%d fediverse follower(s) → Nostr for %s",
                done, len(followers), user.username)


async def _run_follower_backfill(user_id: int) -> None:
    """Background one-shot (own session) so the potentially-slow backfill never blocks the poll. The
    in-flight guard is released in an OUTER finally so even a SessionLocal() failure can't leak it."""
    from app.database import SessionLocal
    from urllib.parse import urlparse
    try:
        db = SessionLocal()
        try:
            user = db.get(User, user_id)
            if user and user.pleroma_instance_url:
                host = urlparse(user.pleroma_instance_url).netloc.split(":")[0].lower()
                await _backfill_followers(db, _port(), user, host)
        except Exception as e:
            logger.warning("[fedi-personal] follower-backfill task failed for user %s: %s", user_id, e)
            db.rollback()
        finally:
            db.close()
    finally:
        _backfill_inflight.discard(user_id)


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
            nid = n.get("id") or ""
            if not await _deliver_one_notif(db, port, user, instance_host, n, recipient, broadcast, blocked):
                # TRANSIENT publish failure (relay flap/disconnect) — permanent non-deliveries already
                # advanced inside _deliver_one_notif and never reach here. Default: STOP and retry next
                # cycle with the cursor unmoved, so a relay that's briefly down (or flapping across a poll
                # or two) resumes delivery when it recovers and NEVER drops a notification. Only when the
                # SAME item keeps failing for _POISON_MAX_FAILS consecutive cycles — genuinely wedging the
                # drain, not a blip — do we skip past it so newer notifications aren't starved forever.
                pz = _notif_poison.get(user.id)
                fails = (pz.get("fails", 0) + 1) if (pz and pz.get("id") == nid) else 1
                if fails >= _POISON_MAX_FAILS:
                    logger.warning("[fedi-personal] skipping notification %s (%s from %s) for %s after %d "
                                   "consecutive failures (relay wedged?)", nid, n.get("type"),
                                   (n.get("account") or {}).get("acct"), user.username, fails)
                    _notif_poison.pop(user.id, None)
                    last = nid or last    # advance PAST the wedged item
                    continue
                _notif_poison[user.id] = {"id": nid, "fails": fails}
                stop = True               # transient failure → retry next cycle, don't advance past it
                break
            _notif_poison.pop(user.id, None)   # a success clears any in-progress failure streak
            last = nid or last
        if last and last != cursor:
            cursor = last
            user.fedi_bridge_notif_since = cursor
            _persist_cursor(user.id, "fedi_bridge_notif_since", cursor)   # survive a killed poll txn
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
        # One-time: backfill the user's EXISTING fediverse followers as puppet kind-3 follows. Runs in
        # the background (won't stall the poll), SERIALIZED to one user at a time (`not _backfill_inflight`)
        # so several opted-in users can't burst the relay at once, and gated by _backfill_due (done flag +
        # post-failure cooldown). A strong task ref is kept so it isn't GC'd mid-run.
        if not _backfill_inflight and _backfill_due(db, user.id):
            _backfill_inflight.add(user.id)
            t = asyncio.ensure_future(_run_follower_backfill(user.id))
            _backfill_tasks.add(t)
            t.add_done_callback(_backfill_tasks.discard)
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
            # Cursors persist via a FRESH session (_persist_cursor) so a poll whose transaction is killed
            # by Postgres idle_in_transaction_session_timeout (slow deliveries idle it) still records
            # forward progress — no leaky per-session GUC override needed.
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
