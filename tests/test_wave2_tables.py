"""#161 wave 2: bots, per-user settings and chat conversations are relay DocTables -- relay-only, no SQL.

Everything here runs against the SHIPPED RelayServer over a real socket with the real PosterChanDB store behind
it (tests/wave2_harness.py); the `db` handed around is the app's NoSQL session. The rules:

  * a row written through the app is on the RELAY and a fresh process (a new table instance) reads it back;
  * "could not ask" is never "no rows": an unreachable relay raises Unavailable from every read and every write,
    and nothing is minted, defaulted or written on the strength of it (a new stream key, an empty mail account
    list, a forgotten follow seen-set, a storage key);
  * a point lookup during a restart's load reads ONE document.

The one-time copy of the old SQL rows is tests/test_migrate_tables_to_nostr.py.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.services import bot_table, conversation_table, user_settings_table
from app.services.relay_reader import Unavailable
from tests.wave2_harness import fresh_process_view, no_relay, relay_mode, shared_relay  # noqa: F401


def run(coro):
    return asyncio.run(coro)


# =========================================================================== user settings
def test_a_setting_lives_on_the_relay_and_a_fresh_process_reads_it(relay_mode):
    db = relay_mode.db()
    uid = 11
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


def test_an_unreachable_relay_is_unavailable_never_an_unset_setting(no_relay):
    db = no_relay.db()
    with pytest.raises(Unavailable):
        user_settings_table.get(db, 1, "stream_token")
    with pytest.raises(Unavailable):
        run(user_settings_table.aget(db, 1, "stream_token"))
    with pytest.raises(Unavailable):
        run(user_settings_table.aby_key(db, "ai_requested"))
    with pytest.raises(Unavailable):
        user_settings_table.set(db, 1, "x", "y")


def test_a_point_lookup_during_a_restarts_load_reads_one_document(relay_mode, monkeypatch):
    db = relay_mode.db()
    user_settings_table.set(db, 5, "stream_token", "abc123")
    t = fresh_process_view(user_settings_table.TABLE)
    # the table's own background load is held back: the answer must come without it
    monkeypatch.setattr(type(t), "start_background_load", lambda self: None)
    assert not t.loaded
    assert run(user_settings_table.aget(db, 5, "stream_token")) == "abc123"
    assert not t.loaded, "a point read must not wait for (or need) the whole table"


# =========================================================================== conversations
def test_conversations_on_the_relay_round_trip_with_owner_checks(relay_mode):
    db = relay_mode.db()
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


def test_conversations_unreachable_relay_is_unavailable(no_relay):
    db = no_relay.db()
    with pytest.raises(Unavailable):
        run(conversation_table.alist_for_user(db, 1))
    with pytest.raises(Unavailable):
        run(conversation_table.aget(db, 7, 1))
    with pytest.raises(Unavailable):
        conversation_table.create(db, 1, "x")


# =========================================================================== bots
def test_bots_on_the_relay_create_rename_toggle_delete(relay_mode):
    db = relay_mode.db()
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
        bot_table.all_bots(no_relay.db())


# =========================================================================== call sites
def _user(uid, **kw):
    return SimpleNamespace(id=uid, username="alice", nostr_npub=None, is_admin=False, can_ai=True, **kw)


def test_chat_routes_store_conversations_on_the_relay(relay_mode):
    from app.routers import chat
    from app.schemas import ConversationCreate
    db = relay_mode.db()
    uid = 11
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
    db = relay_mode.db()
    uid = 11
    auth.set_user_timezone({"offset_minutes": 420, "tz_name": "Asia/Bangkok"}, current_user=_user(uid), db=db)
    fresh_process_view(user_settings_table.TABLE)
    assert fresh_process_view(user_settings_table.TABLE).get("%d:tz_name" % uid)["value"] == "Asia/Bangkok"
    assert str(reminder_service.get_user_tzinfo(db, uid)) == "Asia/Bangkok"


def test_a_stream_key_that_cannot_be_read_is_never_replaced(no_relay):
    """Minting a new token over an unreadable one moves the user's stream path out from under their encoder."""
    from app.routers import streams
    with pytest.raises(Unavailable):
        streams._user_token(no_relay.db(), _user(1))


def test_stream_token_and_sentinel_round_trip_on_the_relay(relay_mode):
    from app.routers import streams
    from app.services import stream_end_service
    db = relay_mode.db()
    uid = 11
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
        run(sns.is_dupe_follow(no_relay.db(), _user(1), {"type": "follow", "platform": "nostr", "actor": "x"}))


def test_follow_dedup_remembers_on_the_relay(relay_mode):
    from app.services import social_notifications_service as sns
    db = relay_mode.db()
    n = {"type": "follow", "platform": "nostr", "actor": "bob"}
    assert run(sns.is_dupe_follow(db, _user(3), n)) is False
    fresh_process_view(user_settings_table.TABLE)
    assert run(sns.is_dupe_follow(db, _user(3), n)) is True


def test_mail_accounts_that_cannot_be_read_are_not_no_accounts(no_relay):
    from app.services import mail_service
    with pytest.raises(Unavailable):
        mail_service.get_user_mail_accounts(1, no_relay.db())


def test_bot_routes_and_the_manager_use_the_relay(relay_mode):
    from app.routers import bots
    from app.services import bot_manager_service
    db = relay_mode.db()
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
        nostr_store.user_storage_seckey(no_relay.db(), SimpleNamespace(id=1, nostr_npub=None))


