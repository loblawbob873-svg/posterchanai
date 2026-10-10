"""#161 wave 2: bots, per-user settings and chat conversations leave Postgres for relay DocTables.

Everything here runs against the SHIPPED RelayServer over a real socket with the real PosterChanDB store behind
it (tests/wave2_harness.py), and an in-memory SQLite database standing in for the legacy tables. The rules:

  * a row written through the app is on the RELAY and a fresh process (a new table instance) reads it back;
  * "could not ask" is never "no rows": an unreachable relay raises Unavailable from every read and every write,
    and nothing is minted, defaulted or written on the strength of it (a new stream key, an empty mail account
    list, a forgotten follow seen-set, a storage key);
  * until a table's `_migrated` marker exists SQL answers and every write lands in BOTH stores, relay first;
  * the migration copies, re-reads strictly, compares, and only then marks; it is idempotent, resumes an
    interrupted copy (stale documents removed), and refuses the marker when SQL changed underneath it;
  * legacy chat MESSAGE rows are counted, never copied (a copy would bring back history deleted on the relay).

The call-site tests drive the converted routes/services themselves, so they fail against the SQL-only code.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services import (bot_table, conversation_table, doc_table_bulk, table_gate, user_settings_table,
                          wave2_migration)
from app.services.relay_reader import Unavailable
from tests.wave2_harness import (add_user, forget_markers, fresh_process_view, no_relay, relay_mode,  # noqa: F401
                                 shared_relay, sql_mode)


def run(coro):
    return asyncio.run(coro)


# =========================================================================== user settings
def test_a_setting_lives_on_the_relay_and_a_fresh_process_reads_it(relay_mode):
    db = relay_mode.Session()
    uid = add_user(relay_mode.Session)
    user_settings_table.set(db, uid, "tz_name", "Asia/Bangkok")
    run(user_settings_table.aset(db, uid, "mail_accounts", json.dumps([{"email": "a@b.c", "password": "s3cret"}])))
    user_settings_table.set(db, uid + 1, "tz_name", "Europe/Oslo")
    fresh_process_view(user_settings_table.TABLE)
    assert user_settings_table.get(db, uid, "tz_name") == "Asia/Bangkok"
    assert json.loads(run(user_settings_table.aget(db, uid, "mail_accounts")))[0]["password"] == "s3cret"
    assert user_settings_table.for_user(db, uid) == {
        "tz_name": "Asia/Bangkok", "mail_accounts": json.dumps([{"email": "a@b.c", "password": "s3cret"}])}
    assert user_settings_table.by_key(db, "tz_name") == {uid: "Asia/Bangkok", uid + 1: "Europe/Oslo"}
    assert user_settings_table.find_user(db, "tz_name", "Europe/Oslo") == uid + 1
    user_settings_table.delete(db, uid, "tz_name")
    fresh_process_view(user_settings_table.TABLE)
    assert user_settings_table.get(db, uid, "tz_name", "gone") == "gone"
    # nothing was written to SQL: the relay is authoritative once the marker exists
    from app.models import UserSetting
    assert db.query(UserSetting).count() == 0


def test_an_unreachable_relay_is_unavailable_never_an_unset_setting(no_relay):
    db = no_relay.Session()
    with pytest.raises(Unavailable):
        user_settings_table.get(db, 1, "stream_token")
    with pytest.raises(Unavailable):
        run(user_settings_table.aget(db, 1, "stream_token"))
    with pytest.raises(Unavailable):
        run(user_settings_table.aby_key(db, "ai_requested"))
    with pytest.raises(Unavailable):
        user_settings_table.set(db, 1, "x", "y")


def test_before_the_marker_sql_answers_and_a_write_lands_in_both(sql_mode):
    from app.models import UserSetting
    db = sql_mode.Session()
    uid = add_user(sql_mode.Session)
    db.add(UserSetting(user_id=uid, key="legacy", value="from-sql"))
    db.commit()
    assert user_settings_table.get(db, uid, "legacy") == "from-sql"       # SQL, not the (empty) relay
    user_settings_table.set(db, uid, "tz_name", "Asia/Tokyo")
    assert db.query(UserSetting).filter_by(user_id=uid, key="tz_name").one().value == "Asia/Tokyo"
    assert fresh_process_view(user_settings_table.TABLE).get("%d:tz_name" % uid)["value"] == "Asia/Tokyo"
    user_settings_table.delete(db, uid, "tz_name")
    assert db.query(UserSetting).filter_by(user_id=uid, key="tz_name").count() == 0
    assert fresh_process_view(user_settings_table.TABLE).get("%d:tz_name" % uid) is None


def test_a_point_lookup_during_a_restarts_load_reads_one_document(relay_mode):
    db = relay_mode.Session()
    user_settings_table.set(db, 5, "stream_token", "abc123")
    t = fresh_process_view(user_settings_table.TABLE)
    assert not t.loaded
    assert run(user_settings_table.aget(db, 5, "stream_token")) == "abc123"
    assert not t.loaded, "a point read must not wait for (or need) the whole table"


# =========================================================================== conversations
def test_conversations_on_the_relay_round_trip_with_owner_checks(relay_mode):
    db = relay_mode.Session()
    a = conversation_table.create(db, 1, "first")
    b = run(conversation_table.acreate(db, 1, "second"))
    other = conversation_table.create(db, 2, "not yours")
    assert a.id != b.id and a.id > 2 ** 31, "a relay-mode id is time-based, far above every SQL id"
    run(conversation_table.atouch(db, a, title="renamed"))
    fresh_process_view(conversation_table.TABLE)
    assert [c.title for c in conversation_table.list_for_user(db, 1)] == ["renamed", "second"]
    assert conversation_table.get(db, other.id, 1) is None
    assert run(conversation_table.aget(db, other.id, 2)).title == "not yours"
    assert run(conversation_table.afind_by_title(db, 1, "second")).id == b.id
    run(conversation_table.adelete(db, a.id))
    fresh_process_view(conversation_table.TABLE)
    assert [c.id for c in conversation_table.list_for_user(db, 1)] == [b.id]


def test_a_conversation_made_before_the_marker_takes_the_sql_id_and_reaches_the_relay(sql_mode):
    from app.models import Conversation
    db = sql_mode.Session()
    uid = add_user(sql_mode.Session)
    c = conversation_table.create(db, uid, "hello")
    assert db.query(Conversation).filter_by(id=c.id).one().title == "hello"
    assert fresh_process_view(conversation_table.TABLE).get(str(c.id))["title"] == "hello"


def test_a_conversation_the_relay_refused_is_not_left_in_sql(sql_mode, monkeypatch):
    from app.models import Conversation
    db = sql_mode.Session()
    uid = add_user(sql_mode.Session)
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", "1")      # the relay stops answering mid-flight
    with pytest.raises(Exception):
        conversation_table.create(db, uid, "lost")
    assert db.query(Conversation).count() == 0, "an id was handed out for a conversation nobody stored"


def test_conversations_unreachable_relay_is_unavailable(no_relay):
    db = no_relay.Session()
    with pytest.raises(Unavailable):
        run(conversation_table.alist_for_user(db, 1))
    with pytest.raises(Unavailable):
        run(conversation_table.aget(db, 7, 1))
    with pytest.raises(Unavailable):
        conversation_table.create(db, 1, "x")


# =========================================================================== bots
def test_bots_on_the_relay_create_rename_toggle_delete(relay_mode):
    db = relay_mode.Session()
    b = bot_table.create(db, name="alice", platform="nostr", modes="--nostr",
                         config=json.dumps({"nostr_nsec": "nsec1secret"}))
    with pytest.raises(bot_table.NameTaken):
        bot_table.create(db, name="alice")
    b.name, b.enabled = "alice2", False
    bot_table.save(db, b, old_name="alice")
    fresh_process_view(bot_table.TABLE)
    got = bot_table.get(db, b.id)
    assert (got.name, got.enabled, json.loads(got.config)["nostr_nsec"]) == ("alice2", False, "nsec1secret")
    assert bot_table.get_by_name(db, "alice") is None and run(bot_table.aget_by_name(db, "alice2")).id == b.id
    assert "nsec1secret" not in repr(got), "a bot's repr must never carry its config (keys)"
    bot_table.delete(db, got)
    fresh_process_view(bot_table.TABLE)
    assert bot_table.all_bots(db) == []


def test_bots_unreachable_relay_is_unavailable_never_no_bots(no_relay):
    with pytest.raises(Unavailable):
        bot_table.all_bots(no_relay.Session())


# =========================================================================== migration
def _seed_sql(factory):
    from app.models import Bot, Conversation, UserSetting
    db = factory()
    uid = add_user(factory)
    db.add_all([UserSetting(user_id=uid, key="tz_name", value="Asia/Bangkok"),
                UserSetting(user_id=uid, key="stream_token", value="tok1"),
                Conversation(user_id=uid, title="old chat"),
                Bot(name="b1", platform="nostr", modes="--nostr", config=json.dumps({"nostr_nsec": "nsec1x"}))])
    db.commit()
    db.close()
    return uid


def test_the_migration_copies_verifies_marks_and_then_the_relay_answers(sql_mode):
    from app.models import UserSetting
    uid = _seed_sql(sql_mode.Session)
    out = run(wave2_migration.migrate_all(sql_mode.Session, list(wave2_migration.TABLES)))
    for name in wave2_migration.TABLES:
        assert out[name].get("verified"), out[name]
    forget_markers()                                         # a fresh process reads the markers from the relay
    assert run(table_gate.arelay_mode("user_settings"))
    # SQL is not read any more: a change made there after the marker is invisible
    db = sql_mode.Session()
    db.query(UserSetting).filter_by(key="tz_name").one().value = "changed-in-sql"
    db.commit()
    fresh_process_view(user_settings_table.TABLE)
    assert user_settings_table.get(db, uid, "tz_name") == "Asia/Bangkok"
    assert [c.title for c in conversation_table.list_for_user(db, uid)] == ["old chat"]
    assert json.loads(bot_table.get_by_name(db, "b1").config)["nostr_nsec"] == "nsec1x"
    # idempotent: a second pass is skipped and writes nothing
    again = run(wave2_migration.migrate_all(sql_mode.Session, list(wave2_migration.TABLES)))
    assert all(again[n].get("skipped") for n in wave2_migration.TABLES)


def test_the_migration_resumes_an_interrupted_copy_and_removes_its_stale_documents(sql_mode):
    uid = _seed_sql(sql_mode.Session)
    t = fresh_process_view(user_settings_table.TABLE)
    t.put("%d:tz_name" % uid, {"user_id": uid, "key": "tz_name", "value": "half-written"})   # differs from SQL
    t.put("999:ghost", {"user_id": 999, "key": "ghost", "value": "no SQL row"})              # stale
    rep = run(wave2_migration.migrate_table("user_settings", sql_mode.Session))
    assert rep["verified"] and rep["rows"] == 2 and rep["removed_stale"] == 1
    t = fresh_process_view(user_settings_table.TABLE)
    assert t.get("%d:tz_name" % uid)["value"] == "Asia/Bangkok" and t.get("999:ghost") is None


def test_the_migration_refuses_the_marker_when_sql_changed_during_the_copy(sql_mode, monkeypatch):
    from app.models import UserSetting
    uid = _seed_sql(sql_mode.Session)
    real = doc_table_bulk.abulk_put

    async def write_lands_mid_copy(table, rows, **kw):
        n = await real(table, rows, **kw)
        db = sql_mode.Session()                     # a live write, relay first then SQL, during the copy
        await user_settings_table.aset(db, uid, "tz_name", "Europe/Paris")
        db.close()
        return n
    monkeypatch.setattr(doc_table_bulk, "abulk_put", write_lands_mid_copy)
    with pytest.raises(doc_table_bulk.MigrationMismatch):
        run(wave2_migration.migrate_table("user_settings", sql_mode.Session))
    forget_markers()
    assert not run(table_gate.arelay_mode("user_settings")), "a copy that raced a write must not be marked"
    monkeypatch.setattr(doc_table_bulk, "abulk_put", real)
    assert run(wave2_migration.migrate_table("user_settings", sql_mode.Session))["verified"]
    db = sql_mode.Session()
    assert db.query(UserSetting).filter_by(key="tz_name").one().value == "Europe/Paris"
    assert user_settings_table.get(db, uid, "tz_name") == "Europe/Paris"


def test_the_migration_refuses_when_the_relay_cannot_be_asked(no_relay):
    doc_table_bulk._seen.clear()
    _seed_sql(no_relay.Session)
    out = run(wave2_migration.migrate_all(no_relay.Session, list(wave2_migration.TABLES)))
    assert all(str(v).startswith("not migrated") for v in out.values()), out


def test_legacy_messages_are_counted_never_copied(sql_mode, monkeypatch):
    """A Telegram `new` deleted the relay transcript and never touched the legacy SQL rows: copying "what the
    relay lacks" would bring that history back."""
    from app.models import Conversation, Message
    from app.services import chat_store, keystore, nostr_store
    sk = bytes.fromhex("42" * 32)
    monkeypatch.setattr(keystore, "get_storage_seckey", lambda npub: sk)
    monkeypatch.setattr(chat_store, "_port", lambda db=None: sql_mode.port)
    uid = add_user(sql_mode.Session, npub="npub1alice")
    db = sql_mode.Session()
    kept, cleared = Conversation(user_id=uid, title="kept"), Conversation(user_id=uid, title="📱 Telegram")
    db.add_all([kept, cleared])
    db.flush()
    db.add_all([Message(conversation_id=kept.id, role="user", content="hello"),
                Message(conversation_id=kept.id, role="assistant", content="hi there"),
                Message(conversation_id=cleared.id, role="user", content="zebra private words")])
    db.commit()
    user = SimpleNamespace(id=uid, nostr_npub="npub1alice")
    assert run(chat_store.add_message(db, user, kept.id, "user", "hello"))
    assert run(chat_store.add_message(db, user, kept.id, "assistant", "hi there"))
    rep = run(wave2_migration.amessage_census(sql_mode.Session))
    assert (rep["sql_rows"], rep["on_relay"], rep["not_on_relay"]) == (3, 2, 1)
    assert rep["conversations_with_gaps"] == {str(cleared.id): 1}
    docs = run(nostr_store.list_all_docs(sql_mode.port, "%s%d:" % (nostr_store.NS_MSG, cleared.id), seckey=sk))
    assert docs == {}, "a legacy row was copied back onto the relay"
    assert "zebra private words" not in json.dumps(rep), "the census carries counts, never content"


# =========================================================================== call sites (fail on the SQL code)
def _user(uid, **kw):
    return SimpleNamespace(id=uid, username="alice", nostr_npub=None, is_admin=False, can_ai=True, **kw)


def test_chat_routes_store_conversations_on_the_relay(relay_mode):
    from app.routers import chat
    from app.schemas import ConversationCreate
    db = relay_mode.Session()
    uid = add_user(relay_mode.Session)
    me = _user(uid)
    made = chat.create_conversation(ConversationCreate(title="plans"), db=db, current_user=me)
    chat.create_conversation(ConversationCreate(title="📱 Telegram"), db=db, current_user=me)
    fresh_process_view(conversation_table.TABLE)
    assert fresh_process_view(conversation_table.TABLE).get(str(made.id))["title"] == "plans"
    assert [c.title for c in chat.list_conversations(db=db, current_user=me)] == ["plans"]
    chat.delete_all_conversations(db=db, current_user=SimpleNamespace(id=uid, username="nobody-%d" % uid))
    assert conversation_table.list_for_user(db, uid) == []


def test_timezone_route_stores_on_the_relay_and_reminders_read_it(relay_mode):
    from app.routers import auth
    from app.services import reminder_service
    db = relay_mode.Session()
    uid = add_user(relay_mode.Session)
    auth.set_user_timezone({"offset_minutes": 420, "tz_name": "Asia/Bangkok"}, current_user=_user(uid), db=db)
    fresh_process_view(user_settings_table.TABLE)
    assert fresh_process_view(user_settings_table.TABLE).get("%d:tz_name" % uid)["value"] == "Asia/Bangkok"
    assert str(reminder_service.get_user_tzinfo(db, uid)) == "Asia/Bangkok"


def test_a_stream_key_that_cannot_be_read_is_never_replaced(no_relay):
    """Minting a new token over an unreadable one moves the user's stream path out from under their encoder."""
    from app.routers import streams
    with pytest.raises(Unavailable):
        streams._user_token(no_relay.Session(), _user(1))


def test_stream_token_and_sentinel_round_trip_on_the_relay(relay_mode):
    from app.routers import streams
    from app.services import stream_end_service
    db = relay_mode.Session()
    uid = add_user(relay_mode.Session)
    tok = streams._user_token(db, _user(uid))
    assert streams._user_token(db, _user(uid)) == tok
    assert run(stream_end_service.user_by_token(db, tok)) == uid
    ev = {"kind": 30311, "tags": [["d", tok + "-1760000000"], ["starts", "1760000000"]], "id": "e", "sig": "s"}
    run(stream_end_service.save_sentinel(db, uid, ev))
    run(stream_end_service.mark_publishing(db, uid))
    fresh_process_view(user_settings_table.TABLE)
    assert json.loads(user_settings_table.get(db, uid, stream_end_service.SENTINEL_KEY))["seen_live"] is True
    run(stream_end_service.clear_sentinel(db, uid))
    assert user_settings_table.get(db, uid, stream_end_service.SENTINEL_KEY) is None


def test_follow_dedup_that_cannot_read_its_seen_set_does_not_reannounce(no_relay):
    from app.services import social_notifications_service as sns
    with pytest.raises(Unavailable):
        run(sns.is_dupe_follow(no_relay.Session(), _user(1), {"type": "follow", "platform": "nostr", "actor": "x"}))


def test_follow_dedup_remembers_on_the_relay(relay_mode):
    from app.services import social_notifications_service as sns
    db = relay_mode.Session()
    n = {"type": "follow", "platform": "nostr", "actor": "bob"}
    assert run(sns.is_dupe_follow(db, _user(3), n)) is False
    fresh_process_view(user_settings_table.TABLE)
    assert run(sns.is_dupe_follow(db, _user(3), n)) is True


def test_mail_accounts_that_cannot_be_read_are_not_no_accounts(no_relay):
    from app.services import mail_service
    with pytest.raises(Unavailable):
        mail_service.get_user_mail_accounts(1, no_relay.Session())


def test_bot_routes_and_the_manager_use_the_relay(relay_mode):
    from app.routers import bots
    from app.services import bot_manager_service
    db = relay_mode.Session()
    payload = bots.BotPayload(name="chessy", platform="pleroma", modes="--chess", bot_type="text",
                              config={"prompt": "hi"})
    made = bots.create_bot(payload, request=SimpleNamespace(url=SimpleNamespace(hostname="")), db=db, admin=None)
    assert fresh_process_view(bot_table.TABLE).get(str(made["id"]))["name"] == "chessy"
    bots.stop_bot(made["id"], db=db, admin=None)
    fresh_process_view(bot_table.TABLE)
    assert [b["enabled"] for b in bots.list_bots(db=db, admin=None)] == [False]
    bots.start_bot(made["id"], db=db, admin=None)
    assert [d["name"] for d in bot_manager_service._enabled_bots_for_host()["text"]] == ["chessy"]
    bots.delete_bot(made["id"], db=db, admin=None)
    fresh_process_view(bot_table.TABLE)
    assert bot_table.all_bots(db) == []


def test_a_legacy_storage_key_that_cannot_be_read_is_never_reminted(no_relay):
    """Minting a new key over an unreadable one makes everything encrypted to the old key unreadable."""
    from app.services import nostr_store
    with pytest.raises(Unavailable):
        nostr_store.user_storage_seckey(no_relay.Session(), SimpleNamespace(id=1, nostr_npub=None))


def test_the_migration_script_reports_counts_never_content(sql_mode, capsys):
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location(
        "migrate_wave2_tables", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "migrate_wave2_tables.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _seed_sql(sql_mode.Session)
    assert mod.main(["--table", "all"], session_factory=sql_mode.Session) == 0
    out = capsys.readouterr().out
    assert "nsec1x" not in out and "Asia/Bangkok" not in out, "a report must never carry a row's content"
    assert mod.main(["--table", "bots"], session_factory=sql_mode.Session) == 0      # re-run: skipped
    assert '"skipped": true' in capsys.readouterr().out


def test_the_migration_script_refuses_without_a_relay(no_relay):
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location(
        "migrate_wave2_tables", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "migrate_wave2_tables.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    doc_table_bulk._seen.clear()
    assert mod.main(["--table", "user_settings"], session_factory=no_relay.Session) == 2
