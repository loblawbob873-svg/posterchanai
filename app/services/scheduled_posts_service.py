"""Scheduled posts — publish a Nostr note at a chosen future time.

A note is signed by the USER's key, which the server never holds, so the flow is:
  1. The client composes the note and picks a future time, then SIGNS a normal event with
     `created_at` = that time (works for nip07 / Amber / local nsec — signing stays client-side).
  2. It POSTs the pre-signed event here (`/client/scheduled`), which stores a `scheduled_posts` row
     (status=pending).
  3. A background AsyncIOScheduler (`start_scheduled_posts_scheduler`, port-3051 only) polls every
     ~30s for due pending rows, atomically claims each (pending → sending) so a concurrent user
     cancel can't double-fire, and BROADCASTS the already-signed event to the relay (which then
     federates via the outbox). On success → sent; while the relay is unreachable it stays pending
     and retries; only after a long retry window past its due time is it marked failed.

The store of record is the `scheduled_posts` DocTable (#161; it used to be a Postgres row): one operator
document per schedule keyed by its integer id, so it persists across restarts. A table that cannot be read
raises relay_reader.Unavailable — the router answers 503 and the poller skips the pass; neither ever reads
"could not ask" as "nothing scheduled". Cancel/edit: cancel flips pending → cancelled (atomic, loses the race to
a mid-send claim); editing the time/content re-signs client-side (a new event), i.e. cancel-old +
create-new. A row left 'sending' by a crash/restart mid-publish is recovered to 'pending' on startup.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.models import User
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


def post_row(user_id, event_id, event_json, scheduled_at, status="pending", content_preview="", attempts=0,
             created_at=None, sent_at=None) -> dict:
    from app.services.app_tables import to_iso
    return {"user_id": user_id, "event_id": event_id or "", "event_json": event_json,
            "scheduled_at": to_iso(scheduled_at), "status": status or "pending",
            "content_preview": content_preview or "", "attempts": int(attempts or 0),
            "created_at": to_iso(created_at), "sent_at": to_iso(sent_at)}


def as_post(key, row: dict) -> SimpleNamespace:
    from app.services.app_tables import from_iso
    return SimpleNamespace(id=int(key), user_id=row.get("user_id"), event_id=row.get("event_id") or "",
                           event_json=row.get("event_json") or "", scheduled_at=from_iso(row.get("scheduled_at")),
                           status=row.get("status") or "pending", content_preview=row.get("content_preview") or "",
                           attempts=int(row.get("attempts") or 0), created_at=from_iso(row.get("created_at")),
                           sent_at=from_iso(row.get("sent_at")))


async def _table():
    from app.services.app_tables import table, SCHEDULED_POSTS
    return await table(SCHEDULED_POSTS)


def _lock():
    from app.services.app_tables import row_lock, SCHEDULED_POSTS
    return row_lock(SCHEDULED_POSTS)


# ---- create / list / cancel (used by the /client/scheduled router) ----
async def count_open(db, user: User) -> int:
    """How many of the user's schedules are still pending or mid-send (the per-user cap)."""
    t = await _table()
    return len(await t.awhere(lambda r: r.get("user_id") == user.id and r.get("status") in ("pending", "sending")))


async def create(db, user: User, event: dict, scheduled_at: datetime) -> SimpleNamespace:
    """Store a pre-signed event to publish at `scheduled_at`. Caller has already verified the event
    signature and that its author is this user."""
    from app.services.app_tables import new_id
    t = await _table()
    row = post_row(user.id, str(event.get("id") or ""), json.dumps(event, separators=(",", ":")),
                   scheduled_at, "pending", _preview(event), 0, datetime.utcnow(), None)
    rid = await t.ainsert(row, new_id)       # SQL's id until the marker exists, a new_id() after
    return as_post(rid, row)


async def list_for_user(db, user: User) -> list:
    """The user's schedules worth showing: still-pending, mid-send, or failed (so a failure is visible
    and dismissable rather than silently vanishing). Soonest first; 'sent'/'cancelled' are dropped."""
    t = await _table()
    rows = [as_post(k, r) for k, r in await t.awhere(
        lambda r: r.get("user_id") == user.id and r.get("status") in ("pending", "sending", "failed"))]
    rows.sort(key=lambda r: (r.scheduled_at or datetime.max, r.id))
    # scheduled_at is a NAIVE UTC datetime — tag it UTC before .timestamp() (a naive .timestamp() would
    # assume local time and shift the unix value by the server's tz offset).
    return [{"id": r.id, "scheduled_at": int(r.scheduled_at.replace(tzinfo=timezone.utc).timestamp()),
             "preview": r.content_preview or "", "status": r.status,
             "event_id": r.event_id} for r in rows]


async def cancel(db, user: User, row_id: int) -> bool:
    """Cancel a still-pending schedule, or dismiss a failed one. Loses the race to a scheduler tick that
    already claimed a pending row (pending → sending) — both take the same row lock — and returns False."""
    t = await _table()
    async with _lock():
        row = await t.aget(str(int(row_id)))
        if not row or row.get("user_id") != user.id or row.get("status") not in ("pending", "failed"):
            return False
        row["status"] = "cancelled"
        row["sent_at"] = datetime.utcnow().isoformat()
        await t.aput(str(int(row_id)), row)
    return True


# ---- the scheduler: publish due posts ----
async def _recover_stale(t) -> int:
    """Rows left 'sending' by a crash/restart mid-publish are orphaned (single instance → no other
    process owns them). Reset them to 'pending' so the next poll re-attempts. Returns the count."""
    n = 0
    async with _lock():
        for k, row in await t.awhere(lambda r: r.get("status") == "sending"):
            row["status"] = "pending"
            await t.aput(k, row)
            n += 1
    return n


async def _prune_terminal(t) -> int:
    """Delete terminal (sent/cancelled/failed) rows a while AFTER they resolve so the table doesn't grow
    without bound. Keyed off `sent_at` (the resolve time — set for sent/failed/cancelled alike), NOT
    created_at: a post scheduled far out (up to 400 days) must keep its ⚠ failed notice for the full
    window after it actually fails, not be pruned the instant it fails just because it was made long ago."""
    from app.services.app_tables import from_iso
    cutoff = datetime.utcnow() - _PRUNE_AGE

    def old(r):
        at = from_iso(r.get("sent_at"))
        return r.get("status") in ("sent", "cancelled", "failed") and at is not None and at < cutoff
    n = 0
    for k, _row in await t.awhere(old):
        await t.adelete(k)
        n += 1
    return n


async def _publish_due_once() -> None:
    """One poll pass: claim + broadcast every due pending post. Runs on the app's event loop.

    A table that cannot be read ends the pass with nothing touched (the next pass tries again); a claim the
    relay did not confirm is not published, so an unconfirmed write can never become a second broadcast."""
    from app.services.app_tables import from_iso
    now = datetime.utcnow()
    try:
        t = await _table()
        # Reclaim any 'sending' row orphaned by a prior pass that died mid-publish. Poll passes never overlap
        # (max_instances=1, coalesce), so any 'sending' seen at the START of a pass is stale — reset it to
        # 'pending' to re-attempt. Without this, the note would wedge as a permanent 'sending…'.
        await _recover_stale(t)
        await _prune_terminal(t)   # old sent/cancelled/failed rows (usually 0)

        def _due(r):
            at = from_iso(r.get("scheduled_at"))
            return r.get("status") == "pending" and at is not None and at <= now
        due = sorted(await t.awhere(_due), key=lambda kr: (from_iso(kr[1].get("scheduled_at")), int(kr[0])))[:50]
        if not due:
            return
        port = _ss._port()
        for rid, _ in due:
            # CLAIM: pending → sending, under the lock a user cancel takes. A cancel that got there first wins.
            async with _lock():
                cur = await t.aget(rid)
                if not cur or cur.get("status") != "pending":
                    continue
                cur["status"] = "sending"
                await t.aput(rid, cur)       # raises if the relay did not confirm: nothing is published
            ok = False
            try:
                event = json.loads(cur.get("event_json") or "")
                ok, msg = await store.publish_event(port, event)
                if not ok:
                    logger.warning("[scheduled] post %s not published: %s", rid, msg)
            except Exception as e:
                logger.warning("[scheduled] publish %s failed: %s", rid, e)
            async with _lock():
                fresh = await t.aget(rid)
                if fresh is None or fresh.get("status") != "sending":
                    continue   # cancelled/changed underneath us — leave it
                if ok:
                    fresh["status"] = "sent"
                    fresh["sent_at"] = datetime.utcnow().isoformat()
                else:
                    # Retry EVERY failure (keep it 'pending') up to _MAX_ATTEMPTS, then give up. We deliberately
                    # do NOT classify permanent-vs-transient: the local relay legitimately returns OK-false for
                    # RECOVERABLE reasons — a transient "not stored, retry", or "not in web of trust" during its
                    # startup/WoT-load race right after a deploy — so any fast-fail on a single rejection would
                    # wrongly kill a note that would publish moments later. sent_at doubles as the resolve time.
                    fresh["attempts"] = int(fresh.get("attempts") or 0) + 1
                    if fresh["attempts"] >= _MAX_ATTEMPTS:
                        fresh["status"] = "failed"
                        fresh["sent_at"] = datetime.utcnow().isoformat()
                    else:
                        fresh["status"] = "pending"
                # If this write is not confirmed the row stays 'sending' and the next pass's recovery re-arms
                # it: a published note is re-broadcast (the same signed event — relays dedupe it by id),
                # never lost.
                await t.aput(rid, fresh)
    except Exception as e:
        logger.error("[scheduled] poll pass failed: %s", e, exc_info=True)


_scheduler = None


def start_scheduled_posts_scheduler() -> None:
    """Start the due-post poller (idempotent; port-3051 only, wired in main.py). A 'sending' row orphaned by
    the last shutdown/crash is recovered at the start of every pass (the first one included) — at startup
    the relay this table lives on is usually not up yet, so recovering here could only fail."""
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

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


# ---- the SQL side (#161 wave 1): SQL is the store of record until the `scheduled_posts` marker exists ----
def _post_from_sql(r) -> dict:
    return post_row(r.user_id, r.event_id, r.event_json, r.scheduled_at, r.status, r.content_preview,
                    r.attempts, r.created_at, r.sent_at)


def _register_legacy():
    from app.services import table_migration
    from app.services.app_tables import SCHEDULED_POSTS
    from app.services.legacy_sql import ModelLegacy
    table_migration.register(ModelLegacy(SCHEDULED_POSTS, "ScheduledPost", _post_from_sql))


_register_legacy()
