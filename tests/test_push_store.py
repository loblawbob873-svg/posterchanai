"""The push tables as Nostr documents (#161): push_subscriptions, push_sent_wraps, push_follow_seen and
direct_push_messages leave Postgres for push_store's DocTables on this node's relay.

Everything here runs against the SHIPPED RelayServer over the real PosterChanDB store
(tests/push_relay_harness.py). Rules pinned, each one a way the conversion could lose a notification:
  * rows round-trip, a fresh process sees them, and a device's prefs keep NULL ("never configured" =
    send everything) distinct from a set value;
  * a write made by another process (the worker queueing for a phone the app serves) arrives;
  * COULD NOT ASK is never "no devices": the poll skips and retries the same window, the queue answers
    "failed" (never "expired", the only answer that deletes a device), the Direct socket is not told its
    token is unknown, and the own-sent-DM dedup FAILS OPEN;
  * the one-time copy's readers give the rows these paths serve (ids the phone ACKs kept, expired cards dropped,
    the follow ledger sharded);
  * none of these paths touches SQL (relay-only).
"""
import asyncio
import json
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.routers import push as push_router
from app.services import direct_push_service as direct
from app.services import doc_table, nostr_push_service as nps, push_store
from app.services.relay_reader import Unavailable
from tests import push_relay_harness as H
push_relay = H.push_relay          # the fixture

ME = "a" * 64
AUTHOR = "b" * 64


class _Req:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


def _forbid_sql(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("a converted push path opened a SQL session")
    monkeypatch.setattr("app.database.SessionLocal", forbidden)


# ------------------------------------------------------------------ CRUD and other processes
def test_rows_round_trip_and_a_fresh_process_reads_them_with_null_prefs_kept(push_relay):
    a = H.add_sub(pubkey=ME, endpoint="https://push.example/a", prefs=None)
    b = H.add_sub(pubkey=ME, endpoint="https://push.example/b", prefs='{"likes": false}')
    assert a["id"] != b["id"] and a["id"] > 2 ** 40, "a new device id must not collide with a SQL id"
    rows = {r["endpoint"]: r for r in H.fresh_subs()}
    assert rows["https://push.example/a"]["prefs"] is None, "NULL prefs must stay NULL (send everything)"
    assert rows["https://push.example/b"]["prefs"] == '{"likes": false}'
    assert rows["https://push.example/a"]["id"] == a["id"]
    asyncio.run(push_store.delete_sub(a["id"]))
    assert [r["endpoint"] for r in H.fresh_subs()] == ["https://push.example/b"]


def test_a_notification_queued_by_the_worker_reaches_the_apps_socket_loop(push_relay):
    sid = H.add_sub(pubkey=ME, endpoint="direct:%s:dev" % ME, transport=direct.TRANSPORT,
                    device_id="dev-0000000000000001", token_hash="d" * 64)["id"]
    assert direct._pending(sid) == []                       # the app's view is loaded and live
    app_view = doc_table.DocTable(push_store.DIRECT)
    doc_table.DocTable._registry.pop(push_store.DIRECT)     # "the worker": its own table instance
    doc_table.DocTable._registry.pop(push_store.SUBS)
    assert direct.enqueue_result(sid, {"type": "dm", "title": "x"}) == "queued"
    doc_table.DocTable._registry[push_store.DIRECT] = app_view
    end = time.monotonic() + 8
    while time.monotonic() < end and not direct._pending(sid):
        time.sleep(0.1)
    frames = direct._pending(sid)
    assert len(frames) == 1 and frames[0]["payload"]["type"] == "dm", "a cross-process write never arrived"
    direct._ack(sid, frames[0]["id"])
    assert direct._pending(sid) == []


def test_the_queue_keeps_order_and_its_bound(push_relay, monkeypatch):
    monkeypatch.setattr(direct, "_MAX_PENDING", 3)
    sid = H.add_sub(transport=direct.TRANSPORT, endpoint="direct:x", device_id="dev-0000000000000002")["id"]
    for i in range(5):
        assert direct.enqueue_result(sid, {"n": i}) == "queued"
    assert [f["payload"]["n"] for f in direct._pending(sid)] == [2, 3, 4], "oldest dropped, FIFO kept"
    H.reset()
    assert [f["payload"]["n"] for f in direct._pending(sid)] == [2, 3, 4], "a fresh process disagrees"


def test_deleting_a_device_drops_its_queue_like_the_foreign_key_did(push_relay):
    sid = H.add_sub(transport=direct.TRANSPORT, endpoint="direct:y", device_id="dev-0000000000000003")["id"]
    direct.enqueue_result(sid, {"n": 1})
    asyncio.run(push_store.delete_sub(sid))
    H.reset()
    assert push_store.direct().all() == {}


# ------------------------------------------------------------------ could not ask
def test_an_unreadable_device_table_skips_the_poll_and_the_next_pass_retries_the_same_window(
        tmp_path, monkeypatch):
    """Not "nobody is subscribed": the cursor must not move, and nothing is deleted."""
    H.dead_relay(monkeypatch)
    ev = {"id": "e" * 64, "kind": 7, "pubkey": AUTHOR, "created_at": 100, "tags": [["p", ME]],
          "content": "+", "sig": ""}

    async def query(*_a, **_k):
        return [ev]
    monkeypatch.setattr(nps.relay, "query", query)

    async def name(_pk):
        return "Alice"
    monkeypatch.setattr(nps, "_name_for", name)
    sent = []
    monkeypatch.setattr(nps.push_service, "send", lambda sub, payload: sent.append(payload) or True)
    monkeypatch.setattr(nps, "_seen", set())
    monkeypatch.setattr(nps, "_cursor", 50)
    asyncio.run(nps._poll())
    assert sent == [] and nps._cursor == 50, "the window was spent while the devices could not be read"

    r = H.start(tmp_path, monkeypatch)                   # the relay is back
    try:
        H.add_sub(pubkey=ME, endpoint="https://push.example/phone")
        H.reset()
        asyncio.run(nps._poll())
        assert [p["type"] for p in sent] == ["likes"], "the retry did not deliver what the skipped pass held"
    finally:
        H.reset()
        r.close()


def test_an_unreadable_queue_is_failed_never_expired(monkeypatch):
    """Only "expired" makes a caller delete the device."""
    H.dead_relay(monkeypatch)
    assert direct.enqueue_result(123, {"type": "dm"}) == "failed"
    assert direct.enqueue(123, {"type": "dm"}) is True
    assert direct._pending(123) == []                       # this pass only; nothing raised, nothing deleted


def test_an_unreadable_device_table_does_not_tell_the_phone_its_token_is_unknown(monkeypatch):
    """4401 makes the APK FORGET its token and report notifications off. Could-not-ask must not."""
    H.dead_relay(monkeypatch)
    closed = []

    class Socket:
        async def accept(self):
            pass

        async def receive_json(self):
            return {"type": "auth", "token": "t" * 43}

        async def close(self, code=1000):
            closed.append(code)
    asyncio.run(push_router.direct_socket(Socket()))
    assert closed and closed[0] != 4401, closed


def test_the_dm_dedup_fails_open_when_its_ledger_cannot_be_read(push_relay, monkeypatch):
    H.add_sub(pubkey=ME, endpoint="https://push.example/phone")
    H.reset()

    def down(*_a, **_k):
        raise Unavailable("ledger unreadable")
    monkeypatch.setattr(push_store, "sent_by_own_device_sync", down)
    monkeypatch.setattr(nps, "_dm_recent", {})
    monkeypatch.setattr(nps, "_sub_pks", set())
    sent = []
    monkeypatch.setattr(nps.push_service, "send", lambda sub, payload: sent.append(payload) or True)
    asyncio.run(nps._dm_handler({"id": "c" * 64, "kind": 1059, "pubkey": "f" * 64, "created_at": 1,
                                 "tags": [["p", ME]], "content": "", "sig": ""}))
    assert [p["type"] for p in sent] == ["dm"], "an unreadable dedup ledger suppressed the only copy"


def test_a_wrap_this_account_sent_is_not_pushed_to_any_of_its_devices(push_relay, monkeypatch):
    from app.services.nostr import event as nostr_event
    monkeypatch.setattr(nostr_event, "verify_self_auth", lambda *a: True)
    H.add_sub(pubkey=ME, endpoint="https://push.example/phone")
    wrap = "c" * 64
    assert asyncio.run(push_router.note_sent_wraps(_Req({"pubkey": ME, "auth": "x", "ids": [wrap]})))["noted"] == 1
    H.reset()                                               # the worker reads what the app wrote
    monkeypatch.setattr(nps, "_dm_recent", {})
    monkeypatch.setattr(nps, "_sub_pks", set())
    sent = []
    monkeypatch.setattr(nps.push_service, "send", lambda sub, payload: sent.append(payload) or True)
    asyncio.run(nps._dm_handler({"id": wrap, "kind": 1059, "pubkey": "f" * 64, "created_at": 1,
                                 "tags": [["p", ME]], "content": "", "sig": ""}))
    assert sent == []


def test_an_expired_endpoint_is_pruned_by_the_poll(push_relay, monkeypatch):
    dead = H.add_sub(pubkey=ME, endpoint="https://push.example/gone")
    H.add_sub(pubkey=ME, endpoint="https://push.example/fine")
    H.reset()
    ev = {"id": "9" * 64, "kind": 7, "pubkey": AUTHOR, "created_at": 100, "tags": [["p", ME]],
          "content": "+", "sig": ""}

    async def query(*_a, **_k):
        return [ev]
    monkeypatch.setattr(nps.relay, "query", query)

    async def name(_pk):
        return ""
    monkeypatch.setattr(nps, "_name_for", name)
    monkeypatch.setattr(nps.push_service, "send", lambda sub, payload: sub["endpoint"] != "https://push.example/gone")
    monkeypatch.setattr(nps, "_seen", set())
    monkeypatch.setattr(nps, "_cursor", 50)
    asyncio.run(nps._poll())
    assert [r["endpoint"] for r in H.fresh_subs()] == ["https://push.example/fine"]
    assert dead["id"] not in {r["id"] for r in H.fresh_subs()}


# ------------------------------------------------------------------ the one-time copy's readers
@pytest.fixture
def unmigrated(tmp_path, monkeypatch):
    """A real, empty relay (the state a node is in before its old rows are copied)."""
    r = H.start(tmp_path, monkeypatch)
    try:
        yield r
    finally:
        H.reset()
        r.close()


def _sql_with_rows():
    from app.models import DirectPushMessage, PushFollowSeen, PushSentWrap, PushSubscription
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for t in (PushSubscription, DirectPushMessage, PushFollowSeen, PushSentWrap):
        t.__table__.create(engine)
    S = sessionmaker(bind=engine)
    db = S()
    now = datetime.utcnow()
    db.add_all([
        PushSubscription(id=7, pubkey=ME, endpoint="https://push.example/w", transport="webpush",
                         p256dh="p", auth="a", prefs=None, created_at=now),
        PushSubscription(id=9, pubkey=ME, endpoint="direct:%s:phone" % ME, transport=direct.TRANSPORT,
                         device_id="phone-000000000001", token_hash="h" * 64, last_seen=now,
                         prefs='{"likes": false}', created_at=now),
    ])
    db.flush()
    db.add_all([
        DirectPushMessage(id=3, subscription_id=9, payload='{"type":"dm"}', created_at=now,
                          expires_at=now + timedelta(hours=1)),
        DirectPushMessage(id=4, subscription_id=9, payload='{"type":"call"}', created_at=now,
                          expires_at=now - timedelta(seconds=5)),
        PushFollowSeen(recipient=ME, follower=""),
        PushFollowSeen(recipient=ME, follower="0" * 64),
        PushFollowSeen(recipient=ME, follower="0f" + "1" * 62),
        PushFollowSeen(recipient=ME, follower="c" * 64),
        PushSentWrap(pubkey=ME, wrap_id="d" * 64, created_at=now),
    ])
    db.commit()
    db.close()
    return S


def _copy(S):
    """What scripts/migrate_tables_to_nostr.py writes for the push tables: the SQL readers' rows, as documents."""
    import pathlib
    import sys
    from app.services import doc_table_bulk
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from scripts.nostr_migration import wave1_sources as W
    db = S()
    got = {n: W.SOURCES[n](db) for n in (push_store.SUBS, push_store.DIRECT, push_store.FOLLOWS)}
    db.close()
    for name, rows in got.items():
        asyncio.run(doc_table_bulk.abulk_put(name, rows))
    return got


def test_the_copied_rows_are_what_the_converted_paths_serve(unmigrated, monkeypatch):
    S = _sql_with_rows()
    got = _copy(S)
    assert len(got[push_store.SUBS]) == 2
    assert len(got[push_store.DIRECT]) == 1, "an already-expired card was copied"
    assert push_store.SENT not in got, "the 10-minute sent-wrap ledger is not copied"
    # 3 followers in 3 shards ("00", "0f", "cc") + the seeded mark
    assert len(got[push_store.FOLLOWS]) == 4
    H.reset()
    _forbid_sql(monkeypatch)
    rows = {r["id"]: r for r in push_store.all_subs_sync()}
    assert rows[7]["prefs"] is None and rows[9]["prefs"] == '{"likes": false}'
    assert rows[9]["token_hash"] == "h" * 64 and rows[9]["device_id"] == "phone-000000000001"
    # the phone's queued card kept its id (the phone ACKs it by that number)
    assert [(f["id"], f["payload"]) for f in direct._pending(9)] == [(3, {"type": "dm"})]
    # the follow ledger: seeded, and every recorded follower still known
    assert asyncio.run(push_store.follow_seeded(ME))
    for f in ("0" * 64, "0f" + "1" * 62, "c" * 64):
        assert asyncio.run(push_store.follow_known(ME, f)), f
    assert not asyncio.run(push_store.follow_known(ME, "e" * 64))


# ------------------------------------------------------------------ no SQL on the converted paths
def test_the_device_lifecycle_never_opens_a_sql_session(push_relay, monkeypatch):
    _forbid_sql(monkeypatch)
    monkeypatch.setattr(push_router, "_direct_auth", lambda *a: True)
    from app.services.nostr import event as nostr_event
    monkeypatch.setattr(nostr_event, "verify_self_auth", lambda *a: True)
    monkeypatch.setattr(direct, "disconnect", lambda *a: None)
    dev = "dev-0000000000000009"
    reg = asyncio.run(push_router.register_direct(_Req({"pubkey": ME, "device_id": dev, "auth": "x"})))
    assert reg["ok"]
    assert asyncio.run(push_router.set_prefs(_Req({"pubkey": ME, "auth": "x", "device_id": dev,
                                                   "prefs": {"likes": False}})))["devices"] == 1
    [row] = H.fresh_subs()
    assert json.loads(row["prefs"]) == {"likes": False}
    assert direct.enqueue_result(row["id"], {"type": "test"}) == "queued"
    [frame] = direct._pending(row["id"])
    direct._ack(row["id"], frame["id"])
    assert direct._pending(row["id"]) == []
    assert asyncio.run(push_router.subscribe(_Req({"pubkey": ME, "auth": "x", "subscription": {
        "endpoint": "https://push.example/p", "keys": {"p256dh": "p", "auth": "a"}}}))) == {"ok": True}
    assert asyncio.run(push_router.unsubscribe(_Req({"endpoint": "https://push.example/p"}))) == {"ok": True}
    assert asyncio.run(push_router.unregister_direct(_Req({"pubkey": ME, "device_id": dev, "auth": "x"}))) == {"ok": True}
    assert H.fresh_subs() == []
