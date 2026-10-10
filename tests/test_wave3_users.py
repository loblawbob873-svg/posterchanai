"""#161 wave 3: the ACCOUNTS (and the Exodus wallet backups, the instance-name applications) are relay DocTables,
relay-only from the first request -- no SQL anywhere.

Against the SHIPPED RelayServer over a real socket with the real PosterChanDB store (tests/wave2_harness.py); the
`db` handed around is the app's NoSQL session. The rules pinned, each on the code path a user goes through:

  * a session survives a FRESH PROCESS: sign in with a Nostr key, then a new process (empty memory) still knows who
    the bearer is, and signing in again finds the SAME account;
  * "could not ask" is never "unknown user": an unreachable relay answers Unavailable (503) to a session, an API
    key's owner lookup and a Nostr sign-in -- never 401, never a fresh account minted on the strength of it;
  * a point lookup during a restart's load reads ONE document (by id, or one index document + the account),
    without loading the table, and an index entry left behind by a rename is never an answer;
  * username / npub / Telegram chat / email are unique under the writer, and a rename frees the old value;
  * two saves of the same account that change different fields keep both.

The one-time copy of the old SQL rows (ids kept) is tests/test_migrate_tables_to_nostr.py.
"""
import asyncio

import pytest

from app.services import doc_table
from app.services.relay_reader import Unavailable
from tests.wave2_harness import fresh_process_view, no_relay, relay_mode, shared_relay  # noqa: F401

NPUB_A = "npub1" + "a" * 58


def run(coro):
    return asyncio.run(coro)


def _request():
    from starlette.requests import Request
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b"",
                    "scheme": "http", "server": ("test", 80), "client": ("127.0.0.1", 1)})


def _nostr_login(monkeypatch, db, pubkey_hex):
    from fastapi import Response
    from app.routers import auth as A
    from app.services import registration_service
    monkeypatch.setattr(A, "_verify_nostr_auth", lambda *_a, **_k: True)
    monkeypatch.setattr(registration_service, "enabled", lambda: True)
    monkeypatch.setenv("POSTERCHANAI_AUTO_ADMIN", "0")
    return run(A.nostr_login(A.NostrLogin(pubkey=pubkey_hex, auth="x"), Response(), _request(), db))


def _session_user(db, token):
    from fastapi.security import HTTPAuthorizationCredentials
    from app.auth import get_current_user
    return get_current_user(_request(), HTTPAuthorizationCredentials(scheme="Bearer", credentials=token), db)


# =========================================================================== the auth path
def test_a_nostr_sign_in_is_still_a_session_in_a_fresh_process(relay_mode, monkeypatch):
    pk = "ab" * 32
    out = _nostr_login(monkeypatch, relay_mode.db(), pk)
    assert out["user"]["new"] is True
    for name in ("users", "user_index"):            # a NEW process: nothing in memory
        fresh_process_view(name)
    user = _session_user(relay_mode.db(), out["access_token"])
    assert user.id == out["user"]["id"] and user.username == out["user"]["username"]
    again = _nostr_login(monkeypatch, relay_mode.db(), pk)
    assert again["user"]["id"] == out["user"]["id"] and again["user"]["new"] is False


def test_an_unreachable_relay_is_503_never_an_unknown_user(no_relay):
    from fastapi import HTTPException
    from app.auth import create_access_token
    db = no_relay.db()
    token = create_access_token({"sub": "4242"})
    try:
        _session_user(db, token)
    except HTTPException as e:              # 401 "User not found" would log the session out
        pytest.fail("a relay outage logged the session out: %s %s" % (e.status_code, e.detail))
    except Unavailable:
        pass
    else:
        pytest.fail("a session was answered without its account")
    from app.utils.auth_utils import get_user_from_api_key
    with pytest.raises(Unavailable):        # None would read as "Invalid API key"
        get_user_from_api_key(db, 4242)


def test_a_sign_in_during_an_outage_never_mints_an_account(no_relay, monkeypatch):
    with pytest.raises(Unavailable):
        _nostr_login(monkeypatch, no_relay.db(), "cd" * 32)


def test_a_lookup_during_the_load_reads_one_document_and_ignores_stale_index_entries(relay_mode, monkeypatch):
    from app.services import user_table
    db = relay_mode.db()
    u = user_table.create(db, username="dora", password_hash="h", nostr_npub=NPUB_A,
                          telegram_chat_id="777", telegram_enabled=True, email="d@x.y")
    u.username = "dora2"
    user_table.save(db, u)
    for name in ("users", "user_index"):
        fresh_process_view(name)
    # the tables' own background loads are held back: every answer must come from one-document reads
    monkeypatch.setattr(doc_table.DocTable, "start_background_load", lambda self: None)
    monkeypatch.setattr(user_table, "_load_in_background", lambda: None)
    assert run(user_table.aget(db, u.id)).username == "dora2"
    assert run(user_table.aby_username(db, "dora2")).id == u.id
    assert run(user_table.aby_username(db, "dora")) is None           # the old name is free, not "dora2"
    assert run(user_table.aby_telegram_chat(db, 777)).id == u.id
    assert run(user_table.aby_npub(db, [NPUB_A, "ab" * 32])).id == u.id
    assert user_table.by_email(db, "d@x.y").id == u.id                 # sync, off the loop: one document too
    assert not doc_table.DocTable("users").loaded


def test_unique_fields_are_refused_and_a_rename_frees_the_old_value(relay_mode):
    from app.services import user_table
    db = relay_mode.db()
    a = user_table.create(db, username="ann", password_hash="h", nostr_npub=NPUB_A, telegram_chat_id="5",
                          email="a@x.y")
    for field, value in (("username", "ann"), ("nostr_npub", NPUB_A), ("telegram_chat_id", "5"), ("email", "a@x.y")):
        with pytest.raises(user_table.UserConflict) as e:
            user_table.create(db, password_hash="h", **dict({"username": "other-" + field}, **{field: value}))
        assert e.value.field == field
    b = user_table.create(db, username="bob", password_hash="h")
    b.telegram_chat_id = "5"
    with pytest.raises(user_table.UserConflict):
        user_table.save(db, b)
    a.username = "ann2"
    user_table.save(db, a)
    assert user_table.create(db, username="ann", password_hash="h").username == "ann"
    assert len(user_table.filter_users(db, lambda u: u.nostr_npub == NPUB_A)) == 1


def test_new_ids_are_never_reused(relay_mode):
    from app.services import user_table
    db = relay_mode.db()
    a = user_table.create(db, username="x1", password_hash="h")
    user_table.delete(db, a.id)
    b = user_table.create(db, username="x2", password_hash="h")
    assert b.id != a.id and b.id > 2 ** 31


def test_two_saves_of_one_account_keep_both_fields(relay_mode):
    from app.services import user_table
    db = relay_mode.db()
    u = user_table.create(db, username="eve", password_hash="h")
    first, second = user_table.get(db, u.id), user_table.get(db, u.id)
    first.theme = "dark"
    second.can_ai = True
    user_table.save(db, first)
    user_table.save(db, second)
    fresh_process_view("users")
    got = user_table.get(db, u.id)
    assert got.theme == "dark" and got.can_ai is True


def test_an_unreachable_relay_never_lists_nobody_or_creates(no_relay):
    from app.services import user_table
    db = no_relay.db()
    with pytest.raises(Unavailable):
        user_table.all_users(db)
    with pytest.raises(Unavailable):
        user_table.create(db, username="z", password_hash="h")


# =========================================================================== exodus wallet backups, applications
def test_a_wallet_backup_is_inserted_once_and_read_back_strictly(relay_mode):
    from app.services import exodus_tables
    db = relay_mode.db()
    run(exodus_tables.ainsert_record(db, 7, "default", b"\x01\x02"))
    with pytest.raises(exodus_tables.Exists):
        run(exodus_tables.ainsert_record(db, 7, "default", b"\x09"))     # never replaces a seed
    fresh_process_view(exodus_tables.RECORDS)
    assert run(exodus_tables.arecord(db, 7, "default")).document_enc == b"\x01\x02"
    run(exodus_tables.aput_wallet(db, 7, seed_enc=b"\xaa", label="L", address_index=2, backed_up_at=None))
    fresh_process_view(exodus_tables.WALLETS)
    w = run(exodus_tables.awallet(db, 7))
    assert w.seed_enc == b"\xaa" and w.address_index == 2


def test_an_unreadable_wallet_backup_is_never_no_backup(no_relay):
    from app.services import exodus_tables
    with pytest.raises(Unavailable):
        run(exodus_tables.awallet(no_relay.db(), 7))


def test_an_instance_application_lives_on_the_relay_once(relay_mode):
    from app.services import instance_welcome as W
    db = relay_mode.db()
    row, created = run(W.apply(db, "ef" * 32))
    assert created
    fresh_process_view(W.TABLE)
    row2, created2 = run(W.apply(db, "ef" * 32))
    assert not created2 and row2.created_at == row.created_at
    assert run(W._claim(db, "ef" * 32, "admin_retry_at", 1000)) is True
    assert run(W._claim(db, "ef" * 32, "admin_retry_at", 1000)) is False     # claimed: no second DM
