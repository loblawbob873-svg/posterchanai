"""Scheduled posts — publish a Nostr note at a chosen future time.

A note is signed by the USER's key, which the server never holds, so the flow is:
  1. The client composes the note and picks a future time, then SIGNS a normal event with
     `created_at` = that time (works for nip07 / Amber / local nsec — signing stays client-side).
  2. It POSTs the pre-signed event here (`/client/scheduled`), which stores a `ScheduledPost` row
     (status=pending).
  3. A background AsyncIOScheduler (`start_scheduled_posts_scheduler`, port-3051 only) polls every
     ~30s for due pending rows, atomically claims each (pending → sending) so a concurrent user
     cancel can't double-fire, and BROADCASTS the already-signed event to the relay (which then
     federates via the outbox). On success → sent; while the relay is unreachable it stays pending
     and retries; only after a long retry window past its due time is it marked failed.

The `ScheduledPost` row (in the app's Postgres, the same DB the relay uses) is the store of record —
it persists across restarts. Cancel/edit: cancel flips pending → cancelled (atomic, loses the race to
a mid-send claim); editing the time/content re-signs client-side (a new event), i.e. cancel-old +
create-new. A row left 'sending' by a crash/restart mid-publish is recovered to 'pending' on startup.
"""

import json
import logging
from datetime import datetime, timedelta

from app.database import SessionLocal
from app.models import ScheduledPost, User
from app.services import nostr_store as store
from app.services import settings_store as _ss

logger = logging.getLogger("scheduled_posts")

_MAX_FUTURE_DAYS = 300              # cap how far out a schedule can be. Kept BELOW Blossom's 365-day
                                   # age-TTL so an image/background post (whose blob is uploaded at
                                   # schedule time) can't have its media pruned before the note publishes.
# Give up after this many ACTUAL publish attempts (~2h at the 30s poll). Counting attempts — not wall time
# since the due date — means a post whose due time fell during a long outage/deploy still gets its full
# retry budget once the node is back (attempts only accrue when we actually try), which is exactly when
# the local relay may still be doing its WoT-load startup race and answering OK-false transiently.
_MAX_ATTEMPTS = 240
_PRUNE_AGE = timedelta(days=7)      # delete terminal (sent/cancelled/failed) rows this long AFTER they resolve


def _preview(event: dict) -> str:
    """First line of the note, trimmed — for the Drafts list UI (no key needed to show it)."""
    body = (event.get("content") or "").strip().replace("\n", " ")
    return body[:277] + "…" if len(body) > 278 else body


# ---- create / list / cancel (used by the /client/scheduled router) ----
def create(db, user: User, event: dict, scheduled_at: datetime) -> ScheduledPost:
    """Store a pre-signed event to publish at `scheduled_at`. Caller has already verified the event
    signature and that its author is this user."""
    row = ScheduledPost(
        user_id=user.id,
        event_id=str(event.get("id") or ""),
        event_json=json.dumps(event, separators=(",", ":")),
        scheduled_at=scheduled_at,
        status="pending",
        content_preview=_preview(event),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_for_user(db, user: User) -> list:
    """The user's schedules worth showing: still-pending, mid-send, or failed (so a failure is visible
    and dismissable rather than silently vanishing). Soonest first; 'sent'/'cancelled' are dropped."""
    from datetime import timezone
    rows = (db.query(ScheduledPost)
            .filter(ScheduledPost.user_id == user.id,
                    ScheduledPost.status.in_(("pending", "sending", "failed")))
            .order_by(ScheduledPost.scheduled_at.asc())
            .all())
    # scheduled_at is a NAIVE UTC datetime — tag it UTC before .timestamp() (a naive .timestamp() would
    # assume local time and shift the unix value by the server's tz offset).
    return [{"id": r.id, "scheduled_at": int(r.scheduled_at.replace(tzinfo=timezone.utc).timestamp()),
             "preview": r.content_preview or "", "status": r.status,
             "event_id": r.event_id} for r in rows]


def cancel(db, user: User, row_id: int) -> bool:
    """Cancel a still-pending schedule, or dismiss a failed one. Atomic: loses the race to a scheduler
    tick that already claimed a pending row (pending → sending), in which case this returns False."""
    claimed = (db.query(ScheduledPost)
               .filter(ScheduledPost.id == row_id, ScheduledPost.user_id == user.id,
                       ScheduledPost.status.in_(("pending", "failed")))
               .update({"status": "cancelled", "sent_at": datetime.utcnow()}, synchronize_session=False))
    db.commit()
    return bool(claimed)


# ---- the scheduler: publish due posts ----
def _recover_stale(db) -> int:
    """Rows left 'sending' by a crash/restart mid-publish are orphaned (single instance → no other
    process owns them). Reset them to 'pending' so the next poll re-attempts. Returns the count."""
    n = (db.query(ScheduledPost)
         .filter(ScheduledPost.status == "sending")
         .update({"status": "pending"}, synchronize_session=False))
    db.commit()
    return n


def _prune_terminal(db) -> int:
    """Delete terminal (sent/cancelled/failed) rows a while AFTER they resolve so the table doesn't grow
    without bound. Keyed off `sent_at` (the resolve time — set for sent/failed/cancelled alike), NOT
    created_at: a post scheduled far out (up to 400 days) must keep its ⚠ failed notice for the full
    window after it actually fails, not be pruned the instant it fails just because it was made long ago."""
    cutoff = datetime.utcnow() - _PRUNE_AGE
    n = (db.query(ScheduledPost)
         .filter(ScheduledPost.status.in_(("sent", "cancelled", "failed")),
                 ScheduledPost.sent_at.isnot(None),
                 ScheduledPost.sent_at < cutoff)
         .delete(synchronize_session=False))
    db.commit()
    return n


async def _publish_due_once() -> None:
    """One poll pass: claim + broadcast every due pending post. Runs on the app's event loop."""
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        # Reclaim any 'sending' row orphaned by a prior pass that died mid-publish (e.g. an idle-connection
        # death on the follow-up query/commit). Poll passes never overlap (max_instances=1, coalesce), so
        # any 'sending' seen at the START of a pass is stale — reset it to 'pending' to re-attempt. Without
        # this, only a process restart would recover it and the note would wedge as a permanent 'sending…'.
        _recover_stale(db)
        _prune_terminal(db)   # cheap indexed delete of old sent/cancelled/failed rows (usually 0)
        due = (db.query(ScheduledPost)
               .filter(ScheduledPost.status == "pending", ScheduledPost.scheduled_at <= now)
               .order_by(ScheduledPost.scheduled_at.asc())
               .limit(50)
               .all())
        if not due:
            return
        # Snapshot the fields we need for EVERY due row up front. Each per-row commit below expires all
        # loaded ORM instances (expire_on_commit), so reading a sibling row's attribute mid-loop would
        # lazily reload it — and raise ObjectDeletedError if its owner's account was CASCADE-deleted
        # meanwhile, aborting the whole pass. Reading everything now avoids any post-commit ORM access.
        jobs = [(r.id, r.event_json) for r in due]
        port = _ss._port()
        for rid, ejson in jobs:
            # ATOMIC CLAIM: pending → sending. If a user cancel flipped it first, rowcount is 0 → skip.
            claimed = (db.query(ScheduledPost)
                       .filter(ScheduledPost.id == rid, ScheduledPost.status == "pending")
                       .update({"status": "sending"}, synchronize_session=False))
            db.commit()
            if not claimed:
                continue
            ok = False
            try:
                event = json.loads(ejson)
                ok, msg = await store.publish_event(port, event)
                if not ok:
                    logger.warning("[scheduled] post %s not published: %s", rid, msg)
            except Exception as e:
                logger.warning("[scheduled] publish %s failed: %s", rid, e)
            fresh = db.query(ScheduledPost).filter(ScheduledPost.id == rid).first()
            if fresh is None or fresh.status != "sending":
                continue   # cancelled/changed underneath us — leave it
            if ok:
                fresh.status = "sent"
                fresh.sent_at = datetime.utcnow()
            else:
                # Retry EVERY failure (keep it 'pending') up to _MAX_ATTEMPTS, then give up. We deliberately
                # do NOT classify permanent-vs-transient: the local relay legitimately returns OK-false for
                # RECOVERABLE reasons — a transient "not stored, retry", or "not in web of trust" during its
                # startup/WoT-load race right after a deploy — so any fast-fail on a single rejection would
                # wrongly kill a note that would publish moments later. sent_at doubles as the resolve time.
                fresh.attempts = (fresh.attempts or 0) + 1
                if fresh.attempts >= _MAX_ATTEMPTS:
                    fresh.status = "failed"
                    fresh.sent_at = datetime.utcnow()
                else:
                    fresh.status = "pending"
            db.commit()
    except Exception as e:
        logger.error("[scheduled] poll pass failed: %s", e, exc_info=True)
    finally:
        db.close()


_scheduler = None


def start_scheduled_posts_scheduler() -> None:
    """Start the due-post poller (idempotent; port-3051 only, wired in main.py)."""
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    # Recover any 'sending' rows orphaned by the last shutdown/crash BEFORE polling resumes.
    db = SessionLocal()
    try:
        n = _recover_stale(db)
        if n:
            logger.info("[scheduled] recovered %d stale 'sending' post(s) → pending", n)
    except Exception as e:
        logger.warning("[scheduled] stale-recovery failed: %s", e)
    finally:
        db.close()

    async def _job():
        try:
            await _publish_due_once()
        except Exception as e:   # a scheduler job must never raise out
            logger.error("[scheduled] job error: %s", e, exc_info=True)

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_job, "interval", seconds=30, id="scheduled_posts_poll",
                       max_instances=1, coalesce=True)
    _scheduler.start()
    logger.info("[scheduled] scheduler started (30s poll)")


def stop_scheduled_posts_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
