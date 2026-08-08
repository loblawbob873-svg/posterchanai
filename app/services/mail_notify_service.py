"""New mail → a push notification, for a phone whose screen is off.

The Email client fetches on login and when you open it, which covers the case where the app is
already in front of you. This is the other case: the poller that notices mail arriving while nothing
is open, and pushes it the way reminders and DMs are pushed.

OFF BY DEFAULT, and that is not timidity. A background IMAP poll is the exact shape of the thing
that took this feature down in June 2026, so it is opt-in per node (`mail_poll_enabled`), it runs in
the WORKER process rather than the one serving requests, its interval is a setting with a sane floor,
and it does the same incremental sync the client does rather than a fresh full scan. It notifies —
it never re-downloads a mailbox to find out whether it should.

WHAT IT DOES NOT DO: read your mail to summarise it. The notification carries the sender and the
subject, which is what the message doc already holds, and nothing else leaves the node.
"""
import asyncio
import logging

from app.models import User

logger = logging.getLogger(__name__)

# A floor, not a default. Below this an IMAP poll across every account on the node stops being a
# notifier and becomes a load generator — the failure this feature has already had once.
_MIN_MINUTES = 2
_DEFAULT_MINUTES = 10

# Never announce more than this many individually; past it the notification is a count. A mailbox
# that has been offline for a week must not deliver two hundred separate buzzes.
_MAX_INDIVIDUAL = 3


def enabled() -> bool:
    from app.services import settings_store
    v = settings_store.get("mail_poll_enabled", None)
    if v is None or str(v).strip() == "":
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def interval_minutes() -> int:
    from app.services import settings_store
    try:
        n = int(settings_store.get_int("mail_poll_minutes", _DEFAULT_MINUTES) or _DEFAULT_MINUTES)
    except Exception:
        n = _DEFAULT_MINUTES
    return max(_MIN_MINUTES, n)


async def _push(db, user, title: str, body: str) -> None:
    """Web Push / UnifiedPush — the only path that reaches a phone with its screen off.

    Best-effort and never fatal: the mail is already stored by the time this runs, so a push service
    having a bad day must not turn into a failed sync.
    """
    npub = (getattr(user, "nostr_npub", "") or "").strip()
    if not npub:
        return
    try:
        from app.models import PushSubscription
        from app.services import push_service
        from app.services.nostr import nostr_service
        pk = nostr_service.to_pubkey_hex(npub)
        rows = db.query(PushSubscription).filter(PushSubscription.pubkey == pk).all() if pk else []
        payload = {"title": title, "body": body, "type": "mail"}
        for row in rows:
            sub = {"endpoint": row.endpoint, "keys": {"p256dh": row.p256dh, "auth": row.auth}}
            if not await asyncio.to_thread(push_service.send, sub, payload):
                db.delete(row)            # the endpoint is gone for good — prune it
        if rows:
            db.commit()
    except Exception as e:
        logger.warning("[mail-notify] push failed for user %s: %s", user.id, e)


def _describe(new_msgs: list) -> tuple:
    """(title, body) for what arrived."""
    n = len(new_msgs)
    if n == 1:
        m = new_msgs[0]
        who = (m.get("from") or m.get("from_email") or "").strip() or "someone"
        subj = (m.get("subject") or "(no subject)").strip()
        return "📧 New email", f"{who}\n{subj}"
    if n <= _MAX_INDIVIDUAL:
        lines = []
        for m in new_msgs:
            who = (m.get("from") or m.get("from_email") or "").split("<")[0].strip() or "someone"
            lines.append(f"{who}: {(m.get('subject') or '(no subject)').strip()[:60]}")
        return f"📧 {n} new emails", "\n".join(lines)
    return f"📧 {n} new emails", "Open Messages → Email to read them."


async def poll_user(db, user) -> int:
    """Sync this user's INBOXes and push anything new. Returns how many messages arrived."""
    from app.services import mail_service, mail_store, mail_sync, nostr_store
    try:
        if not mail_service.get_user_mail_accounts(user.id, db):
            return 0
    except Exception as e:
        logger.debug("[mail-notify] no accounts for %s: %s", getattr(user, "username", "?"), e)
        return 0

    sk = nostr_store.user_storage_seckey(db, user)
    # UIDs BEFORE the sync, read from d-tags without decrypting anything, so "what is new" is a set
    # difference rather than a second pass over the mailbox. INBOX only: nobody wants a buzz because
    # a copy of their own reply landed in Sent.
    before = {}
    accounts = mail_service.get_user_mail_accounts(user.id, db)
    for acc in accounts:
        try:
            before[acc.email] = await mail_store.have_uids(sk, acc.email, "INBOX")
        except Exception:
            before[acc.email] = None      # unreadable → treated as "cannot tell", never as "empty"

    try:
        res = await mail_sync.sync_all(db, user, folders=["INBOX"])
    except Exception as e:
        logger.info("[mail-notify] sync failed for %s: %s", getattr(user, "username", "?"), e)
        return 0

    total, arrived = 0, []
    for email, n in (res or {}).items():
        if not n:
            continue
        total += int(n)
        prev = before.get(email)
        if prev is None:
            continue                      # counted, but nothing specific can be said about it
        try:
            fresh = (await mail_store.have_uids(sk, email, "INBOX")) - prev
        except Exception:
            continue
        for uid in list(fresh)[:_MAX_INDIVIDUAL]:
            try:
                m = await mail_store.get_message(sk, email, "INBOX", uid)
                if m:
                    arrived.append(m)
            except Exception:
                pass

    if total:
        if arrived:
            title, body = _describe(arrived if len(arrived) >= total else arrived)
            if len(arrived) < total:
                title = f"📧 {total} new emails"
        else:
            title, body = f"📧 {total} new email" + ("s" if total > 1 else ""), \
                "Open Messages → Email to read them."
        await _push(db, user, title, body)
        logger.info("[mail-notify] %s new message(s) for %s", total, getattr(user, "username", "?"))
    return total


async def poll_once(db) -> int:
    if not enabled():
        return 0
    total = 0
    for user in db.query(User).all():
        try:
            total += await poll_user(db, user)
        except Exception as e:
            logger.warning("[mail-notify] %s: %s", getattr(user, "username", "?"), e)
    return total


_scheduler = None


def start_mail_notify_scheduler() -> None:
    """Idempotent. Runs in the WORKER (app/worker.py), never in the process serving requests: an
    IMAP round trip per account is exactly the kind of long await that should not share an event
    loop with the web UI."""
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    mins = interval_minutes()

    async def _job():
        from app.database import SessionLocal
        _db = SessionLocal()
        try:
            await poll_once(_db)
        except Exception as e:
            logger.warning("[mail-notify] poll error: %s", e)
        finally:
            _db.close()

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_job, "interval", minutes=mins, id="mail_notify",
                       max_instances=1, coalesce=True)
    _scheduler.start()
    # The JOB is always scheduled; `poll_once` is what respects the switch. Gating the scheduler on
    # the setting instead meant turning mail notifications on in Admin did nothing at all until
    # somebody restarted the worker — with the switch showing "on" the whole time. That is the
    # worker gotcha this repo already has a note about, and it is invisible from the UI.
    logger.info("[mail-notify] mail poller scheduled every %s min (currently %s)",
                mins, "ON" if enabled() else "off — flip mail_poll_enabled in Admin → Tools")


def stop_mail_notify_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
