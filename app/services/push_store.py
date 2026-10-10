"""The push subsystem's four tables, kept as Nostr documents on this node's relay (#161).

They used to be Postgres tables (push_subscriptions, push_sent_wraps, push_follow_seen,
direct_push_messages). Each is now a `DocTable` (app/services/doc_table.py): one operator-signed,
NIP-44-encrypted kind-30078 document per row, the whole table held in memory per process and kept
current by a live subscription, so the worker (which sends) sees what the app (which registers
devices) wrote within moments.

KEY SCHEMES (stable; a device or client never sees most of them):
  * push_subs         `<id>`            -- the integer id the SQL row had (kept by the migration, because
                                           a live Direct socket, the delivery queue and `subscription_dict`
                                           are all keyed on it); a new device gets a random 52-bit id, which
                                           cannot collide with an autoincrement and needs no shared counter
                                           between the app and the worker.
  * push_sent_wraps   `<pubkey>:<wrap id>`
  * push_follow_seen  `<recipient>`              -> {"seeded": true}  (the old follower="" row)
                      `<recipient>:<xx>`         -> {"f": [follower, ...]}, sharded on the follower's first two
                                                   hex digits. One document PER PAIR would turn the silent
                                                   first-sighting seed (up to 5000 followers) into 5000 signed
                                                   writes; one document per recipient does not fit a NIP-44
                                                   payload (64 KB) for a popular account. 256 shards hold ~240k.
  * direct_push_msgs  `<message id>`    -- the integer the phone ACKs (Java `long`). Old ids are kept; a new one
                                           is microseconds*16 + 4 random bits, so FIFO order (ascending id)
                                           still holds and two processes never mint the same one.

WHAT "COULD NOT ASK" MEANS HERE (relay_reader.Unavailable is never "no rows"):
  * a subscription list that cannot be read -> the sender skips this pass and retries; nothing is deleted;
  * the own-sent-wrap dedup that cannot be read -> FAILS OPEN, the push goes out (never suppress the only copy);
  * a device whose queue cannot be written -> "failed", never "expired" (only "expired" deletes a device).

MOVING OFF POSTGRES (#161 wave 1): until a table's `_migrated` marker exists SQL is still its store of record --
DocTable reads it through the Legacies at the bottom of this module and writes SQL first, then the relay -- so
nothing waits for the copy (table_migration runs it at startup) and a phone that re-registers mid-move updates
its one row. A new device or queued message takes SQL's id until the marker exists.
"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone

from app.services.doc_table import DocTable
from app.services.relay_reader import Unavailable

logger = logging.getLogger(__name__)

SUBS = "push_subs"
SENT = "push_sent_wraps"
FOLLOWS = "push_follow_seen"
DIRECT = "direct_push_msgs"
MIGRATED = "_migrated"

#: SQL table name -> DocTable name. The migration markers are keyed on the SQL name.
TABLES = {"push_subscriptions": SUBS, "push_sent_wraps": SENT,
          "push_follow_seen": FOLLOWS, "direct_push_messages": DIRECT}

SENT_TTL_SECONDS = 600
_ready = False


def subs() -> DocTable: return DocTable(SUBS)
def sent() -> DocTable: return DocTable(SENT)
def follows() -> DocTable: return DocTable(FOLLOWS)
def direct() -> DocTable: return DocTable(DIRECT)


def new_sub_id() -> int:
    return (1 << 40) + secrets.randbelow((1 << 52) - (1 << 40))


def new_message_id() -> int:
    return time.time_ns() // 1000 * 16 + secrets.randbelow(16)


def _ts(value) -> int | None:
    """A SQL DateTime (naive UTC) as unix seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):                  # SQLite hands a raw query's DateTime back as text
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


# ---------------------------------------------------------------- the migration "gate"
# There is none any more (#161 wave 1): until a table's `_migrated` marker exists DocTable answers it from SQL and
# writes SQL then the relay (the Legacies at the bottom of this module), so no push path waits for the move and
# none can register a device beside a not-yet-copied row. Kept for callers that still ask.
async def aready() -> None:
    return None


def ready() -> None:
    return None


# ---------------------------------------------------------------- push_subscriptions
def _with_id(k, row) -> dict:
    out = dict(row)
    out["id"] = int(k)
    return out


def _sub_doc(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "id"}


async def all_subs() -> list[dict]:
    await aready()
    return [_with_id(k, r) for k, r in (await subs().aall()).items()]


def all_subs_sync() -> list[dict]:
    ready()
    return [_with_id(k, r) for k, r in subs().all().items()]


async def subs_where(pred) -> list[dict]:
    return [r for r in await all_subs() if pred(r)]


async def subs_for(pks) -> dict:
    """{pubkey: [rows]} for `pks`."""
    want = set(pks)
    out: dict = {}
    for r in await all_subs():
        if r.get("pubkey") in want:
            out.setdefault(r["pubkey"], []).append(r)
    return out


def subs_for_sync(pks) -> dict:
    want = set(pks)
    out: dict = {}
    for r in all_subs_sync():
        if r.get("pubkey") in want:
            out.setdefault(r["pubkey"], []).append(r)
    return out


def get_sub_sync(sub_id) -> dict | None:
    ready()
    row = subs().get(str(int(sub_id)))
    return _with_id(sub_id, row) if row is not None else None


async def put_sub(row: dict) -> dict:
    if row.get("id") is None:
        row = dict(row)
        row.setdefault("created_at", int(time.time()))
        # A new device: SQL's own id until the table's marker exists (the relay copy carries it), a random
        # 52-bit id after.
        doc = _sub_doc(row)
        sid = await subs().ainsert(doc, new_sub_id)
        return dict(row, id=int(sid))
    await subs().aput(str(int(row["id"])), _sub_doc(row))
    return row


async def delete_sub(sub_id) -> None:
    """Delete a device and (as the SQL foreign key's ON DELETE CASCADE did) whatever was queued for it."""
    await aready()
    sid = int(sub_id)
    await subs().adelete(str(sid))
    try:
        for k, r in await direct().awhere(lambda r: r.get("sub") == sid):
            await direct().adelete(k)
    except Unavailable as e:
        # The queue is bounded by its own expiry and `pending` checks the device still exists.
        logger.info("[push-store] queue cleanup for a removed device deferred: %s", e)


# ---------------------------------------------------------------- push_sent_wraps
async def note_sent(pubkey: str, ids: list[str]) -> int:
    """Record wraps `pubkey` published; prune what is past the window. Returns how many were new."""
    await aready()
    t = sent()
    now = int(time.time())
    for k, r in await t.awhere(lambda r: int(r.get("at") or 0) < now - SENT_TTL_SECONDS):
        await t.adelete(k)
    new = 0
    for wid in dict.fromkeys(ids):
        k = "%s:%s" % (pubkey, wid)
        if await t.aget(k) is not None:
            continue
        await t.aput(k, {"pubkey": pubkey, "wrap": wid, "at": now})
        new += 1
    return new


def sent_by_own_device_sync(pks, wrap_id: str) -> set:
    """Which of `pks` published `wrap_id`. Raises Unavailable -- the caller FAILS OPEN on it."""
    ready()
    t = sent()
    cutoff = int(time.time()) - SENT_TTL_SECONDS
    out = set()
    for pk in set(pks):
        r = t.get("%s:%s" % (pk, wrap_id))
        if r and int(r.get("at") or 0) >= cutoff:
            out.add(pk)
    return out


# ---------------------------------------------------------------- push_follow_seen
def _shard(follower: str) -> str:
    return (follower[:2] or "__").lower()


async def follow_seeded(recipient: str) -> bool:
    await aready()
    r = await follows().aget(recipient)
    return bool(r and r.get("seeded"))


async def follow_known(recipient: str, follower: str) -> bool:
    await aready()
    r = await follows().aget("%s:%s" % (recipient, _shard(follower)))
    return bool(r) and follower in (r.get("f") or [])


async def follow_record(recipient: str, followers, *, seeded: bool = False) -> None:
    """Add `followers` to `recipient`'s seen set, shard by shard; the seeded mark is written LAST, so a seed
    that stops part-way is re-run rather than announcing the followers it never recorded."""
    await aready()
    t = follows()
    by: dict = {}
    for f in followers:
        if f:
            by.setdefault(_shard(f), set()).add(f)
    for sh, fs in sorted(by.items()):
        k = "%s:%s" % (recipient, sh)
        cur = await t.aget(k) or {}
        have = set(cur.get("f") or [])
        if fs <= have:
            continue
        await t.aput(k, {"r": recipient, "f": sorted(have | fs)})
    if seeded:
        await t.aput(recipient, {"seeded": True, "at": int(time.time())})


# ---------------------------------------------------------------- direct_push_messages (sync: worker threads)
def queue_for(sub_id: int) -> list[tuple[int, dict]]:
    ready()
    sid = int(sub_id)
    return sorted(((int(k), r) for k, r in direct().where(lambda r: r.get("sub") == sid)), key=lambda x: x[0])


def drop_expired(now: int | None = None) -> None:
    ready()
    now = int(time.time()) if now is None else now
    for k, _r in direct().where(lambda r: int(r.get("exp") or 0) <= now):
        direct().delete(k)


def enqueue_message(sub_id: int, wire: str, ttl: int, keep: int) -> int:
    """Append to a device's queue, keeping its newest `keep - 1` plus this one."""
    ready()
    sid = int(sub_id)
    for mid, _r in queue_for(sid)[::-1][keep - 1:]:
        direct().delete(str(mid))
    now = int(time.time())
    return int(direct().insert({"sub": sid, "payload": wire, "at": now, "exp": now + int(ttl)}, new_message_id))


def ack_message(sub_id: int, message_id: int) -> None:
    ready()
    r = direct().get(str(int(message_id)))
    if r is not None and r.get("sub") == int(sub_id):
        direct().delete(str(int(message_id)))


# ================================================================ the SQL side (#161 wave 1)
# Until a table's `_migrated` marker exists SQL is its store of record: DocTable reads it through these Legacies
# and writes SQL first, then the relay; table_migration copies it across (verified) and writes the marker.
from app.services.doc_table import Legacy as _Legacy  # noqa: E402


from app.services.doc_table_bulk import MigrationMismatch  # noqa: E402,F401 -- the engine's; callers catch it


def _sub_from_sql(r) -> dict:
    return {"pubkey": r.pubkey, "endpoint": r.endpoint, "transport": r.transport or "webpush",
            "device_id": r.device_id, "token_hash": r.token_hash, "last_seen": _ts(r.last_seen),
            "p256dh": r.p256dh, "auth": r.auth,
            "prefs": r.prefs,                       # NULL stays None: "never configured" means send everything
            "created_at": _ts(r.created_at)}


def _direct_from_sql(r) -> dict:
    return {"sub": int(r.subscription_id), "payload": r.payload, "at": _ts(r.created_at) or int(time.time()),
            "exp": _ts(r.expires_at)}


def _direct_expired(r) -> bool:
    """Already-expired queue rows are not part of the table: every reader deletes them on sight anyway."""
    exp = _ts(r.expires_at)
    return exp is None or exp <= int(time.time())


def _direct_cols(row: dict) -> dict:
    from app.services.legacy_sql import to_datetime
    return {"subscription_id": int(row.get("sub")), "payload": row.get("payload"),
            "created_at": to_datetime(row.get("at")), "expires_at": to_datetime(row.get("exp"))}


class FollowSeenLegacy(_Legacy):
    """push_follow_seen: SQL holds one (recipient, follower) PAIR per row (follower "" = seeded); the documents
    are per-recipient shards. A shard document is the pairs whose follower starts with its two hex digits."""
    name = FOLLOWS

    def rows(self, db) -> dict:
        from app.models import PushFollowSeen
        shards: dict = {}
        seeded: set = set()
        for r in db.query(PushFollowSeen).all():
            if not r.recipient:
                continue
            if not r.follower:
                seeded.add(r.recipient)
                continue
            shards.setdefault("%s:%s" % (r.recipient, _shard(r.follower)), set()).add(r.follower)
        want = {k: {"r": k.rsplit(":", 1)[0], "f": sorted(v)} for k, v in shards.items()}
        for rcp in seeded:
            want[rcp] = {"seeded": True, "at": 0}
        return want

    def get(self, db, k):
        from app.models import PushFollowSeen
        if ":" not in k:
            r = db.get(PushFollowSeen, (k, ""))
            return {"seeded": True, "at": 0} if r is not None else None
        rcp, sh = k.rsplit(":", 1)
        fs = sorted(f for (f,) in db.query(PushFollowSeen.follower).filter(PushFollowSeen.recipient == rcp)
                    if f and _shard(f) == sh)
        return {"r": rcp, "f": fs} if fs else None

    def put(self, db, k, row: dict) -> None:
        from app.models import PushFollowSeen
        if ":" not in k:
            if row.get("seeded") and db.get(PushFollowSeen, (k, "")) is None:
                db.add(PushFollowSeen(recipient=k, follower=""))
            db.flush()
            return
        rcp = k.rsplit(":", 1)[0]
        for f in row.get("f") or []:
            if f and db.get(PushFollowSeen, (rcp, f)) is None:
                db.add(PushFollowSeen(recipient=rcp, follower=f))
        db.flush()

    def delete(self, db, k) -> None:
        from app.models import PushFollowSeen
        if ":" not in k:
            db.query(PushFollowSeen).filter(PushFollowSeen.recipient == k,
                                            PushFollowSeen.follower == "").delete(synchronize_session=False)
        else:
            rcp, sh = k.rsplit(":", 1)
            for r in db.query(PushFollowSeen).filter(PushFollowSeen.recipient == rcp).all():
                if r.follower and _shard(r.follower) == sh:
                    db.delete(r)
        db.flush()


class SentWrapsLegacy(_Legacy):
    """push_sent_wraps is NOT COPIED, on purpose. A row lives 10 minutes and only stops a device being pushed
    about a DM its owner sent. Losing the ledger at the cutover can at worst produce, inside those ten minutes,
    the 'somebody sent you a message' push for one's own message that this table exists to prevent -- and the
    publishing device drops even that one itself (ClientNotified). The table lives on the relay from the start;
    its marker is written for the record."""
    name = SENT
    copy = False
    why_not_copied = "transient 10-minute dedup ledger"

    def rows(self, db) -> dict:
        return {}


def _register_legacy():
    from app.services import table_migration
    from app.services.legacy_sql import ModelLegacy
    table_migration.register(ModelLegacy(SUBS, "PushSubscription", _sub_from_sql))
    table_migration.register(ModelLegacy(DIRECT, "DirectPushMessage", _direct_from_sql, to_cols=_direct_cols,
                                         skip_row=_direct_expired))
    table_migration.register(FollowSeenLegacy())
    table_migration.register(SentWrapsLegacy())


_register_legacy()


def _migrate_one(name: str, session_factory) -> dict:
    from app.services import doc_table, table_migration
    if session_factory is not None:
        doc_table.set_session_factory(session_factory)
    return table_migration.migrate(name)


def migrate_push_subscriptions(session_factory=None) -> dict:
    return _migrate_one(SUBS, session_factory)


def migrate_direct_push_messages(session_factory=None) -> dict:
    return _migrate_one(DIRECT, session_factory)


def migrate_push_follow_seen(session_factory=None) -> dict:
    return _migrate_one(FOLLOWS, session_factory)


def migrate_push_sent_wraps(session_factory=None) -> dict:
    return _migrate_one(SENT, session_factory)


MIGRATIONS = {
    "push_subscriptions": migrate_push_subscriptions,
    "direct_push_messages": migrate_direct_push_messages,
    "push_follow_seen": migrate_push_follow_seen,
    "push_sent_wraps": migrate_push_sent_wraps,
}


def migrate_all(session_factory=None) -> dict:
    """Every push table, in order (subscriptions before the queue that refers to them), through the one engine
    (table_migration). Raises on the first failure; what already succeeded stays marked and is skipped next
    time."""
    return {t: fn(session_factory) for t, fn in MIGRATIONS.items()}
