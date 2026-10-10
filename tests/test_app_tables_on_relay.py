"""Reminders, scheduled posts, pins and the Telegram reply map live on this node's relay now (#161, owner's
call: "Nostr documents"), and the move off Postgres happens once.

Driven against the SHIPPED RelayServer over the real PosterChanDB store (tests/app_tables_harness.py). Pinned:
  * a round trip: what is written is what a FRESH process reads back (not this process's cache);
  * a reminder fires EXACTLY once -- a second pass, a cancel, an unreadable table, an unconfirmed claim and a
    second process's view never make it fire twice, and "could not ask" never drops it either;
  * "could not ask" is Unavailable (a 503, a skipped tick, a "try again" sentence) -- never an empty list;
  * the one-time copy from SQL copies, VERIFIES by re-reading, writes a marker, is a no-op on re-run, refuses
    (no marker) when the relay holds something different, and never writes to SQL;
  * the converted code paths never touch the SQL models (their queries raise here);
  * deleting an account removes its rows (the old FK cascade).
"""
import asyncio
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, User, Reminder, ScheduledPost, SavedSearch, SocialReplyMap
from app.services import app_tables, reminder_service, table_migration
from app.services import saved_search_service as saved
from app.services import scheduled_posts_service as sched
from app.services import social_notifications_service as social
from app.services.doc_table import DocTable
from app.services.relay_reader import Unavailable
from tests.app_tables_harness import tables, shared_relay, no_relay, unmigrated, fresh_process_view  # noqa: F401

ME, THEM = SimpleNamespace(id=1), SimpleNamespace(id=2)
MOVED_MODELS = (Reminder, ScheduledPost, SavedSearch, SocialReplyMap)


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
    """Any query naming one of the moved models raises: the converted code must not ask Postgres for them."""
    real = Session.query

    def guarded(self, *entities, **kw):
        for e in entities:
            cls = getattr(e, "class_", e)
            if cls in MOVED_MODELS or getattr(getattr(e, "parent", None), "class_", None) in MOVED_MODELS:
                raise AssertionError("SQL model %s queried after the move to DocTable" % getattr(cls, "__name__", e))
        return real(self, *entities, **kw)
    monkeypatch.setattr(Session, "query", guarded)
    engine = create_engine("sqlite://")
    User.__table__.create(engine)
    db = sessionmaker(bind=engine)()
    db.add_all([User(id=1, username="me", password_hash="x"), User(id=2, username="them", password_hash="x")])
    db.commit()
    yield db
    db.close()


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


def test_before_the_marker_sql_answers_and_every_write_lands_in_both(unmigrated, delivered, monkeypatch):
    """#161 wave 1: until a table's marker exists SQL is its store of record. Readers and pollers do NOT wait
    (they used to answer "unavailable" for the whole copy): a list shows the reminder still only in SQL, the
    poller fires it, and a reminder created meanwhile goes to SQL AND the relay -- so the copy loses nothing."""
    from app.services import doc_table
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[User.__table__] + [m.__table__ for m in MOVED_MODELS])
    S = sessionmaker(bind=engine)
    db = S()
    db.add_all([User(id=1, username="me", password_hash="x")])
    db.add(Reminder(id=5, user_id=1, text="old, only in SQL", due_at=datetime.utcnow() - timedelta(seconds=1),
                    status="pending", created_at=datetime.utcnow()))
    db.commit()
    db.close()
    table_migration.bind(S)
    try:
        new = run(reminder_service.acreate_reminder(None, ME, "new", datetime.utcnow() + timedelta(days=1)))
        assert new.id < 1 << 31, "a new reminder took an id SQL cannot hold"
        assert sorted(r.text for r in run(reminder_service.alist_reminders(None, ME))) == ["new", "old, only in SQL"]
        db = S()
        assert sorted(r.text for r in db.query(Reminder)) == ["new", "old, only in SQL"], "the write skipped SQL"
        db.close()
        assert run(DocTable("reminders").aget_remote(str(new.id)))["text"] == "new", "the write skipped the relay"
        run(reminder_service.poll_once(None))
        assert [text for _i, _u, text in delivered] == ["old, only in SQL"]
        assert run(table_migration.migrate_all(S(), ["reminders"]))["reminders"]["verified"] == 2
        doc_table.reset_state()                       # a fresh process: the relay is now the store of record
        DocTable._registry.clear()
        assert {k: r["status"] for k, r in fresh_process_view("reminders").all().items()} == {
            "5": "done", str(new.id): "pending"}
    finally:
        doc_table.set_session_factory(None)


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
    assert run(social._deliver(sql_guard, TG(), sql_guard.get(User, 1), "777", norm))
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
    me = db.get(User, 1)
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


# ============================================================================ the one-time copy from SQL

@pytest.fixture
def sql(tables):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[User.__table__] + [m.__table__ for m in MOVED_MODELS])
    db = sessionmaker(bind=engine)()
    db.add_all([User(id=1, username="me", password_hash="x"), User(id=2, username="them", password_hash="x")])
    t0 = datetime(2026, 9, 1, 12, 0, 0, 123456)
    db.add_all([
        Reminder(id=5, user_id=1, text="call the bank", due_at=t0, status="pending", created_at=t0),
        Reminder(id=6, user_id=2, text="done one", due_at=t0, status="done", created_at=t0, delivered_at=t0),
        ScheduledPost(id=3, user_id=1, event_id="e" * 64, event_json='{"id":"' + "e" * 64 + '"}', scheduled_at=t0,
                      status="failed", content_preview="hi", attempts=240, created_at=t0, sent_at=t0),
        SavedSearch(id=9, user_id=1, query="xrp news", created_at=t0),
        SocialReplyMap(id=1, user_id=1, telegram_chat_id="777", telegram_message_id=10, platform="nostr",
                       target_id="old", created_at=t0),
        SocialReplyMap(id=2, user_id=1, telegram_chat_id="777", telegram_message_id=10, platform="nostr",
                       target_id="newer", created_at=t0),
    ])
    db.commit()
    yield db
    db.close()


def _sql_snapshot(db):
    return {m.__tablename__: sorted(tuple(sorted((c.name, str(getattr(r, c.name))) for c in m.__table__.columns))
                                    for r in db.query(m).all()) for m in MOVED_MODELS}


def test_the_copy_carries_every_field_verifies_and_marks(sql):
    before = _sql_snapshot(sql)
    app_tables._pass_done.clear()
    out = run(table_migration.migrate_all(sql))
    assert {n: (r["sql_rows"], r["copied"], r["verified"]) for n, r in out.items()} == {
        "reminders": (2, 2, 2), "scheduled_posts": (1, 1, 1), "saved_searches": (1, 1, 1),
        "social_reply_map": (1, 1, 1)}
    assert _sql_snapshot(sql) == before, "the migration wrote to SQL"
    for name in app_tables.TABLES:
        assert fresh_process_view("_migrated").get(name)["rows"] == out[name]["verified"]
        assert run(app_tables.ready(name))
    # the converted code reads the copied rows with the SAME ids and values
    me = SimpleNamespace(id=1)
    (r,) = run(reminder_service.alist_reminders(None, me))
    assert (r.id, r.text, r.due_at) == (5, "call the bank", datetime(2026, 9, 1, 12, 0, 0, 123456))
    assert run(reminder_service.adelivered_history(2, datetime(2026, 1, 1)))[0].id == 6
    assert [p["id"] for p in run(sched.list_for_user(None, me))] == [3]
    assert [(s.id, s.query) for s in run(saved.alist_saved_searches(None, me))] == [(9, "xrp news")]
    assert fresh_process_view("social_reply_map").get("777:10")["target_id"] == "newer", \
        "two SQL mappings for one message must collapse to the newest, as the old lookup answered"


def test_the_copy_is_idempotent(sql):
    run(table_migration.migrate_all(sql))
    sql.add(Reminder(id=77, user_id=1, text="after the marker", due_at=datetime(2026, 9, 2), status="pending"))
    sql.commit()
    again = run(table_migration.migrate_all(sql))
    assert all(r.get("skipped") for r in again.values()), again
    assert fresh_process_view("reminders").get("77") is None, "a marked table was copied a second time"


def test_a_crash_between_copy_and_marker_is_finished_on_rerun(sql):
    """Rows already copied (identical) count as `already`; the rest are copied; then the marker."""
    DocTable("reminders").put("5", table_migration.legacy("reminders").rows(sql)["5"])
    rep = run(table_migration.migrate_table("reminders", sql))
    assert (rep["copied"], rep["already"], rep["verified"]) == (1, 1, 2)


def test_a_relay_row_that_differs_is_overwritten_with_sql_s(sql):
    """#161 wave 1: until the marker exists SQL is the store of record -- the app reads it and writes it first --
    so a relay copy that differs is stale (a write whose relay half failed, an interrupted copy) and is replaced
    by SQL's row; then the marker."""
    DocTable("reminders").put("5", dict(table_migration.legacy("reminders").rows(sql)["5"], status="cancelled"))
    rep = run(table_migration.migrate_table("reminders", sql))
    assert rep["verified"] == 2 and rep["fixed"] + rep["copied"] >= 1
    assert fresh_process_view("reminders").get("5")["status"] == "pending"
    assert fresh_process_view("_migrated").get("reminders")["ok"] is True


def test_a_relay_that_keeps_refusing_is_no_marker_and_stays_on_sql(sql, monkeypatch):
    from app.services import doc_table_bulk
    real_put = doc_table_bulk.abulk_put

    async def lossy(table, rows, **kw):
        if table == "reminders" and "5" in rows:
            rows = dict(rows, **{"5": dict(rows["5"], status="cancelled")})
        return await real_put(table, rows, **kw)
    monkeypatch.setattr(doc_table_bulk, "abulk_put", lossy)
    app_tables._pass_done.clear()
    with pytest.raises(table_migration.Mismatch) as e:
        run(table_migration.migrate_table("reminders", sql))
    assert "call the bank" not in str(e.value), "a refusal names keys, never row content"
    assert fresh_process_view("_migrated").get("reminders") is None
    # migrate_all reports it and does not raise; the table is NOT marked (it stays on SQL)
    out = run(table_migration.migrate_all(sql, ["reminders"]))
    assert "did not verify" in out["reminders"] and "reminders" not in app_tables._pass_done


def test_a_relay_that_cannot_be_asked_is_no_verdict(no_relay):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[User.__table__] + [m.__table__ for m in MOVED_MODELS])
    db = sessionmaker(bind=engine)()
    with pytest.raises(Unavailable):
        run(table_migration.migrate_table("reminders", db))
    app_tables._pass_done.clear()
    out = run(table_migration.migrate_all(db))
    assert all(v.startswith("not migrated") for v in out.values())
    assert not app_tables._pass_done, "an unreachable relay was taken as a verdict: readers would serve a half-moved table"


def test_the_script(sql, capsys):
    import scripts.migrate_tables_to_nostr as script
    factory = lambda: sql  # noqa: E731
    sql.close = lambda: None
    assert script.main(["--table", "saved_searches"], session_factory=factory) == 0
    assert '"written": 1' in capsys.readouterr().out
    assert script.main(["--table", "saved_searches"], session_factory=factory) == 0
    assert '"skipped": true' in capsys.readouterr().out
    DocTable("reminders").put("5", dict(table_migration.legacy("reminders").rows(sql)["5"], text="edited since"))
    assert script.main(["--table", "reminders"], session_factory=factory) == 0
    out = capsys.readouterr().out
    assert "call the bank" not in out and "edited since" not in out, "the report must never print row content"
    assert fresh_process_view("reminders").get("5")["text"] == "call the bank", "SQL is the store of record"


def test_the_script_refuses_when_the_relay_cannot_be_asked(no_relay, capsys):
    import scripts.migrate_tables_to_nostr as script

    def never():
        raise AssertionError("opened the database although the relay could not be asked")
    assert script.main(["--table", "all"], session_factory=never) == 2
    assert "refusing" in capsys.readouterr().err


def test_startup_pass_retries(shared_relay, monkeypatch):
    import socket
    from tests import app_tables_harness as h
    s = socket.socket(); s.bind(("127.0.0.1", 0)); dead = s.getsockname()[1]; s.close()
    h._isolate(monkeypatch, dead, ready=False)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[User.__table__] + [m.__table__ for m in MOVED_MODELS])
    factory = sessionmaker(bind=engine)
    attempts = []
    real_sleep = asyncio.sleep

    async def sleep(_s):
        attempts.append(sorted(app_tables._pass_done))
        if len(attempts) > 3:
            raise AssertionError("the startup pass never reached a verdict once the relay was up")
        monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(shared_relay.port))   # the relay comes up
        DocTable._registry.clear()
        await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    out = run(table_migration.run_at_startup(factory, retry_s=0))
    assert attempts and attempts[0] == [], "a table was served before its migration could even be attempted"
    assert set(app_tables._pass_done) == set(app_tables.TABLES)
    assert all(r["sql_rows"] == 0 for r in out.values())
