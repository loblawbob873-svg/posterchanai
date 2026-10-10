"""Reminders, scheduled posts, pins and the Telegram reply map live on this node's relay now (#161, owner's
call: "Nostr documents"), and the move off Postgres happens once.

Driven against the SHIPPED RelayServer over the real PosterChanDB store (tests/app_tables_harness.py). Pinned:
  * a round trip: what is written is what a FRESH process reads back (not this process's cache);
  * a reminder fires EXACTLY once -- a second pass, a cancel, an unreadable table, an unconfirmed claim and a
    second process's view never make it fire twice, and "could not ask" never drops it either;
  * "could not ask" is Unavailable (a 503, a skipped tick, a "try again" sentence) -- never an empty list;
  * there is no SQL: the `db` these paths get is app.database's NoSQL session, which raises on any query;
  * deleting an account removes its rows (the old FK cascade).
"""
import asyncio
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.database import NoSQL
from app.services import app_tables, reminder_service, user_table
from app.services import saved_search_service as saved
from app.services import scheduled_posts_service as sched
from app.services import social_notifications_service as social
from app.services.doc_table import DocTable
from app.services.relay_reader import Unavailable
from tests.app_tables_harness import tables, shared_relay, no_relay, fresh_process_view  # noqa: F401

ME, THEM = SimpleNamespace(id=1), SimpleNamespace(id=2)


def run(c):
    return asyncio.run(c)


@pytest.fixture
def delivered(monkeypatch):
    got = []

    async def deliver(db, r):
        got.append((r.id, r.user_id, r.text))
    monkeypatch.setattr(reminder_service, "deliver", deliver)
    return got


@pytest.fixture
def sql_guard(monkeypatch):
    """The `db` the app hands around (NoSQL: any query raises), with accounts 1 and 2 on the relay (written as the
    copy script writes migrated accounts: under their old ids)."""
    for uid, name in ((1, "me"), (2, "them")):
        try:
            DocTable(user_table.TABLE).put(str(uid), user_table.UserRow(uid, {"username": name,
                                                                              "password_hash": "x"}).row())
        except Unavailable:
            break               # the `no_relay` tests: nothing can be written, which is what they test
    return NoSQL()


# ============================================================================ reminders

def test_a_reminder_round_trips_and_a_fresh_process_reads_it(tables):
    due = datetime(2030, 5, 6, 7, 8, 9)
    r = run(reminder_service.acreate_reminder(None, ME, "water the plants", due))
    assert isinstance(r.id, int) and r.status == "pending" and r.due_at == due
    row = fresh_process_view("reminders").get(str(r.id))
    assert row["user_id"] == 1 and row["text"] == "water the plants" and row["status"] == "pending"
    assert reminder_service.as_reminder(r.id, row).due_at == due
    assert [x.id for x in run(reminder_service.alist_reminders(None, ME))] == [r.id]
    assert run(reminder_service.alist_reminders(None, THEM)) == []


def test_list_is_pending_only_and_soonest_first(tables):
    now = datetime.utcnow()
    late = run(reminder_service.acreate_reminder(None, ME, "late", now + timedelta(hours=5)))
    soon = run(reminder_service.acreate_reminder(None, ME, "soon", now + timedelta(hours=1)))
    gone = run(reminder_service.acreate_reminder(None, ME, "gone", now + timedelta(hours=2)))
    assert run(reminder_service.acancel_reminder(None, ME, gone.id))
    assert [x.id for x in run(reminder_service.alist_reminders(None, ME))] == [soon.id, late.id]


def test_cancel_is_the_owner_s_and_only_while_pending(tables):
    r = run(reminder_service.acreate_reminder(None, ME, "x", datetime.utcnow() + timedelta(hours=1)))
    assert not run(reminder_service.acancel_reminder(None, THEM, r.id)), "another account cancelled my reminder"
    assert run(reminder_service.acancel_reminder(None, ME, r.id))
    assert not run(reminder_service.acancel_reminder(None, ME, r.id)), "cancelling twice reports success"
    assert run(reminder_service.aget_reminder(None, THEM, r.id)) is None
    assert fresh_process_view("reminders").get(str(r.id))["status"] == "cancelled"


def test_the_sync_create_is_for_threads_without_a_loop(tables):
    """The torrent alert thread files reminders synchronously; on an event-loop thread a sync call is refused
    (it would block the single uvicorn worker on its own relay socket) rather than deadlocking."""
    box = {}
    th = threading.Thread(target=lambda: box.update(r=reminder_service.create_reminder(
        None, ME, "🎉 Torrent finished downloading: x", datetime.utcnow())))
    th.start()
    th.join(20)
    assert box["r"].id and fresh_process_view("reminders").get(str(box["r"].id))["status"] == "pending"

    async def on_loop():
        with pytest.raises(RuntimeError, match="event-loop"):
            reminder_service.create_reminder(None, ME, "x", datetime.utcnow())
    run(on_loop())


def test_a_due_reminder_fires_exactly_once(tables, delivered):
    r = run(reminder_service.acreate_reminder(None, ME, "now", datetime.utcnow() - timedelta(seconds=5)))
    later = run(reminder_service.acreate_reminder(None, ME, "later", datetime.utcnow() + timedelta(hours=1)))
    run(reminder_service.poll_once(None))
    assert delivered == [(r.id, 1, "now")]
    row = fresh_process_view("reminders").get(str(r.id))
    assert row["status"] == "done" and row["delivered_at"], "the claim was not written to the relay"
    run(reminder_service.poll_once(None))
    assert len(delivered) == 1, "a second pass delivered the same reminder again"
    # a different process (a restarted app) reads the claim from the relay and does not fire it either
    fresh_process_view("reminders")
    run(reminder_service.poll_once(None))
    assert len(delivered) == 1, "a restarted process fired an already-delivered reminder"
    assert fresh_process_view("reminders").get(str(later.id))["status"] == "pending"


def test_a_cancelled_reminder_never_fires(tables, delivered):
    r = run(reminder_service.acreate_reminder(None, ME, "x", datetime.utcnow() - timedelta(seconds=1)))
    assert run(reminder_service.acancel_reminder(None, ME, r.id))
    run(reminder_service.poll_once(None))
    assert delivered == []


def test_an_unreadable_table_skips_the_tick_never_fires_never_drops(no_relay, delivered):
    with pytest.raises(Unavailable):
        run(reminder_service.poll_once(None))
    assert delivered == []


def test_an_unconfirmed_claim_is_not_delivered_and_is_not_delivered_twice_later(tables, delivered, monkeypatch):
    """The claim is written BEFORE delivery. If the relay does not confirm it, nobody knows whether it landed:
    deliver nothing now, and re-read the table strictly before the next claim -- if the write DID land (a lost
    ack), the re-read shows it done and it is not fired a second time; if it did not, it fires once."""
    from app.services import doc_table
    real = doc_table.nostr_store.put_doc
    r = run(reminder_service.acreate_reminder(None, ME, "x", datetime.utcnow() - timedelta(seconds=1)))

    async def lost_ack(port, sk, d, data, **kw):          # the relay STORES it, the answer never arrives
        await real(port, sk, d, data, **kw)
        return False
    monkeypatch.setattr(doc_table.nostr_store, "put_doc", lost_ack)
    run(reminder_service.poll_once(None))
    assert delivered == [], "delivered on the strength of a claim the relay never confirmed"
    monkeypatch.setattr(doc_table.nostr_store, "put_doc", real)
    run(reminder_service.poll_once(None))
    assert delivered == [], "a claim that DID land was fired again after the lost ack"

    r2 = run(reminder_service.acreate_reminder(None, ME, "y", datetime.utcnow() - timedelta(seconds=1)))

    async def refused(*a, **k):                            # the relay did NOT store it
        return False
    monkeypatch.setattr(doc_table.nostr_store, "put_doc", refused)
    run(reminder_service.poll_once(None))
    monkeypatch.setattr(doc_table.nostr_store, "put_doc", real)
    run(reminder_service.poll_once(None))
    assert delivered == [(r2.id, 1, "y")], "a reminder whose claim was refused was dropped instead of retried"
    assert r.id != r2.id


def test_delivered_history_is_owner_scoped_bounded_and_newest_first(tables):
    t = DocTable("reminders")
    now = datetime.utcnow()
    rows = {"1": reminder_service.reminder_row(1, "old", now - timedelta(days=9), "done", None, now - timedelta(days=9)),
            "2": reminder_service.reminder_row(1, "new", now - timedelta(hours=1), "done", None, now - timedelta(hours=1)),
            "3": reminder_service.reminder_row(1, "mid", now - timedelta(days=2), "done", None, None),
            "4": reminder_service.reminder_row(2, "theirs", now, "done", None, now),
            "5": reminder_service.reminder_row(1, "pending", now, "pending", None, None)}
    for k, v in rows.items():
        t.put(k, v)
    got = run(reminder_service.adelivered_history(1, now - timedelta(days=7)))
    assert [r.text for r in got] == ["new", "mid"]


def test_the_history_endpoint_is_a_503_when_the_relay_cannot_be_asked(no_relay):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.auth import get_current_user
    from app.database import get_db
    from app.routers import auth
    app = FastAPI()
    app.include_router(auth.router)
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    r = TestClient(app).get("/api/auth/reminder-notifications")
    assert r.status_code == 503 and "unavailable" in r.json()["detail"]


def test_commands_say_unavailable_rather_than_you_have_none(no_relay):
    from app.services.command_service.productivity import _ProductivityMixin
    cmd = _ProductivityMixin()
    cmd.user, cmd.db = ME, None
    out = run(cmd._reminders_command())
    assert out["content"] == reminder_service.UNAVAILABLE_TEXT
    assert run(cmd._remind_command("cancel 5"))["content"] == reminder_service.UNAVAILABLE_TEXT
    assert run(cmd._pins_command())["content"] == saved.UNAVAILABLE_TEXT
    assert run(cmd._pin_command("delete 5"))["content"] == saved.UNAVAILABLE_TEXT


def test_the_calendar_filer_dedups_against_the_relay_and_waits_when_it_cannot_read(tables, monkeypatch, sql_guard):
    from app.services import calendar_notify_service as cal, caldav_store
    when = datetime(2030, 1, 1, 9, 0)
    monkeypatch.setattr(caldav_store, "enabled", lambda: True)

    async def alarms(db, user):
        return [(when, "📅 Dentist — in 15 minutes")] if user.id == 1 else []
    monkeypatch.setattr(cal, "due_alarms", alarms)
    assert run(cal.poll_once(sql_guard)) == 1
    assert run(cal.poll_once(sql_guard)) == 0, "the same alarm was filed twice"
    rows = fresh_process_view("reminders").all()
    assert [(r["user_id"], r["text"], r["status"]) for r in rows.values()] == [(1, "📅 Dentist — in 15 minutes", "pending")]


def test_the_calendar_filer_writes_nothing_when_the_table_is_unreadable(no_relay, monkeypatch, sql_guard):
    from app.services import calendar_notify_service as cal, caldav_store
    monkeypatch.setattr(caldav_store, "enabled", lambda: True)

    async def alarms(db, user):
        return [(datetime(2030, 1, 1, 9, 0), "📅 Dentist — in 15 minutes")]
    monkeypatch.setattr(cal, "due_alarms", alarms)
    with pytest.raises(Unavailable):
        run(cal.poll_once(sql_guard))


# ============================================================================ the reply map

def test_a_forwarded_notification_maps_back_to_its_target(tables, monkeypatch, sql_guard):
    sent = []

    class TG:
        async def send_message(self, chat_id, text, parse_mode=None):
            return {"result": {"message_id": 4242}}
    norm = {"type": "mention", "platform": "nostr", "reply_target": "e" * 64, "text": "hi", "actor": "x"}
    assert run(social._deliver(sql_guard, TG(), user_table.get(None, 1), "777", norm))
    row = fresh_process_view("social_reply_map").get(social.reply_key("777", 4242))
    assert row["user_id"] == 1 and row["target_id"] == "e" * 64 and row["platform"] == "nostr"

    monkeypatch.setattr(social, "_nostr_cfg", lambda user: (b"\x01" * 32, ["wss://x"], {}))

    async def fetch_event(relays, eid):
        return {"id": eid, "pubkey": "p", "tags": []}

    async def post_note(sk, relays, text, reply_to=None, media_cfg=None):
        sent.append((reply_to["id"], len(text)))
    monkeypatch.setattr(social.nostr_service, "fetch_event", fetch_event)
    monkeypatch.setattr(social.nostr_service, "post_note", post_note)
    assert run(social.handle_reply(sql_guard, 777, 4242, "thanks")) == "✅ Reply posted to Nostr."
    assert sent == [("e" * 64, 6)]
    assert run(social.handle_reply(sql_guard, 777, 1, "not a notification")) is None


def test_reply_lookup_that_cannot_ask_says_so_instead_of_falling_through_to_chat(no_relay, sql_guard):
    out = run(social.handle_reply(sql_guard, 777, 4242, "thanks"))
    assert out and "try again" in out


def test_old_mappings_are_pruned(tables):
    t = DocTable("social_reply_map")
    t.put("1:1", {"user_id": 1, "created_at": (datetime.utcnow() - timedelta(days=8)).isoformat()})
    t.put("1:2", {"user_id": 1, "created_at": datetime.utcnow().isoformat()})
    run(social._prune(None))
    assert set(fresh_process_view("social_reply_map").all()) == {"1:2"}


# ============================================================================ the cascade

def test_deleting_an_account_removes_its_rows_everywhere(tables):
    run(reminder_service.acreate_reminder(None, ME, "mine", datetime.utcnow()))
    keep = run(reminder_service.acreate_reminder(None, THEM, "theirs", datetime.utcnow()))
    run(saved.acreate_saved_search(None, ME, "mine"))
    run(sched.create(None, ME, {"id": "a" * 64, "content": "x"}, datetime.utcnow()))
    DocTable("social_reply_map").put("9:9", {"user_id": 1})
    assert app_tables.purge_user(1) == 4
    for name in app_tables.TABLES:
        assert all(r.get("user_id") != 1 for r in fresh_process_view(name).all().values()), name
    assert fresh_process_view("reminders").get(str(keep.id))["text"] == "theirs"


# ============================================================================ no SQL

def test_the_converted_paths_never_query_the_sql_models(tables, delivered, monkeypatch, sql_guard):
    db = sql_guard
    me = user_table.get(None, 1)
    r = run(reminder_service.acreate_reminder(db, me, "x", datetime.utcnow() - timedelta(seconds=1)))
    run(reminder_service.alist_reminders(db, me))
    run(reminder_service.aget_reminder(db, me, r.id))
    run(reminder_service.poll_once(db))
    run(reminder_service.acancel_reminder(db, me, r.id))
    run(reminder_service.adelivered_history(1, datetime.utcnow() - timedelta(days=1)))
    s = run(saved.acreate_saved_search(db, me, "q"))
    run(saved.alist_saved_searches(db, me))
    run(saved.adelete_saved_search(db, me, s.id))
    monkeypatch.setattr(sched.store, "publish_event", lambda *a: asyncio.sleep(0, (True, "")))
    p = run(sched.create(db, me, {"id": "a" * 64, "content": "x"}, datetime.utcnow() - timedelta(seconds=1)))
    run(sched.count_open(db, me))
    run(sched.list_for_user(db, me))
    run(sched._publish_due_once())
    run(sched.cancel(db, me, p.id))
    run(social._prune(db))
    run(social.handle_reply(db, 1, 1, "x"))
    assert delivered and fresh_process_view("scheduled_posts").get(str(p.id))["status"] == "sent"
