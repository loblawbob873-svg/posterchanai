"""The mailbox walk is remembered and only CHANGES are read again ("mail is super slow").

Opening a conversation and every search walked the whole mailbox: measured on a real one, 18,736
documents in ~11 s, 10 s of it the relay read, and the next walk read everything again. Now the walk is
kept and later calls ask only for documents written since (measured 12.7 s -> 0.13 s, identical result).
These drive the shipped mail_store against a fake relay that honours `since`/`until`/`limit` the way
the real one does -- including the one thing `since` cannot see, a deletion.
"""
import asyncio
import json
import time

import pytest

from app.services import mail_store, nostr_store
from app.services.nostr import nip44

SK = bytes(range(1, 33))


class FakeRelay:
    def __init__(self):
        self.docs = {}        # d -> (created_at, value)
        self.calls = []

    def put(self, d, value, ts):
        self.docs[d] = (ts, value)

    async def list_docs(self, port, prefix, *, seckey=None, encrypt=True, limit=5000, until=None,
                        since=None, with_meta=False, strict=False, **kw):
        self.calls.append({"since": since, "until": until})
        rows = [(d, ts, v) for d, (ts, v) in self.docs.items() if d.startswith(prefix)
                and (since is None or ts >= since) and (until is None or ts <= until)]
        rows.sort(key=lambda r: -r[1])
        rows = rows[:limit]
        return {d: (v, ts) for d, ts, v in rows}


@pytest.fixture
def relay(monkeypatch):
    r = FakeRelay()
    monkeypatch.setattr(nostr_store, "list_docs", r.list_docs)
    async def delete_doc(port, seckey, d):
        r.docs.pop(d, None)
        return True
    monkeypatch.setattr(nostr_store, "delete_doc", delete_doc)
    monkeypatch.setattr(mail_store, "_port", lambda: 1)
    mail_store._SNAPS.clear()
    yield r
    mail_store._SNAPS.clear()


NOW = int(time.time())


def at(n):
    """A write time: old mail sits well in the past, anything written 'later' is after the walk began."""
    return NOW - 100000 + n if n < 10000 else NOW + (n - 10000)


def msg(uid, ts, folder="INBOX", subject="s"):
    return {"uid": str(uid), "account": "me@x", "folder": folder, "ts": ts, "subject": subject}


def walk():
    return asyncio.run(mail_store.list_all_messages(SK, None, None))


def uids(rows):
    return sorted((m["folder"], m["uid"], m["subject"]) for m in rows)


def test_a_second_walk_reads_only_what_changed_and_sees_new_and_edited_mail(relay):
    for i in range(50):
        relay.put(mail_store._d("me@x", "INBOX", str(i)), msg(i, 1000 + i), at(1000 + i))
    assert len(walk()) == 50
    relay.calls.clear()
    relay.put(mail_store._d("me@x", "INBOX", "new"), msg("new", 5000), at(10005))                     # new mail
    relay.put(mail_store._d("me@x", "INBOX", "3"), msg(3, 1003, subject="edited"), at(10006))        # edited
    rows = walk()
    assert relay.calls and all(c["since"] is not None for c in relay.calls), \
        ("the second walk read the whole mailbox again", relay.calls)
    assert len(rows) == 51 and ("INBOX", "3", "edited") in uids(rows) and ("INBOX", "new", "s") in uids(rows)


def test_a_deletion_is_never_served_from_memory(relay):
    for i in range(5):
        relay.put(mail_store._d("me@x", "INBOX", str(i)), msg(i, 100 + i), at(100 + i))
    walk()
    asyncio.run(mail_store.delete_message(SK, "me@x", "INBOX", "2"))
    assert "2" not in {m["uid"] for m in walk()}, "a deleted message came back from the remembered walk"


def test_a_message_moved_to_another_folder_is_in_one_place(relay):
    for i in range(3):
        relay.put(mail_store._d("me@x", "INBOX", str(i)), msg(i, 100 + i), at(100 + i))
    walk()
    asyncio.run(mail_store.delete_message(SK, "me@x", "INBOX", "1"))                               # what /move does
    relay.put(mail_store._d("me@x", "Archive", "1"), msg(1, 101, folder="Archive"), at(10002))
    assert uids(walk()) == [("Archive", "1", "s"), ("INBOX", "0", "s"), ("INBOX", "2", "s")]


def test_a_burst_bigger_than_a_page_and_old_memory_both_walk_everything(relay, monkeypatch):
    monkeypatch.setattr(mail_store, "_SCAN_LIMIT", 10)
    for i in range(25):
        relay.put(mail_store._d("me@x", "INBOX", str(i)), msg(i, 100 + i), at(100 + i))
    assert len(walk()) == 25, "paging dropped messages"
    for i in range(25, 40):                                         # more new than one page holds
        relay.put(mail_store._d("me@x", "INBOX", str(i)), msg(i, 500 + i), at(10010 + i))
    assert len(walk()) == 40
    mail_store._SNAPS[next(iter(mail_store._SNAPS))]["full_at"] -= mail_store._FULL_EVERY + 1
    relay.calls.clear()
    walk()
    assert relay.calls[0]["since"] is None, "an old memory was not refreshed by a full walk"


def test_callers_get_their_own_copies(relay):
    relay.put(mail_store._d("me@x", "INBOX", "1"), msg(1, 100), at(100))
    walk()[0]["body_text"] = "rehydrated by the thread view"
    assert "body_text" not in walk()[0], "one caller's edit leaked into the remembered walk"


def test_the_decrypt_cache_never_serves_an_edited_document_stale():
    a = nip44.encrypt_self(SK, json.dumps({"subject": "one"})) if hasattr(nip44, "encrypt_self") else None
    if a is None:
        pytest.skip("no encrypt_self in this nip44 module")
    b = nip44.encrypt_self(SK, json.dumps({"subject": "two"}))
    first = nostr_store._decode(a, SK, True)
    first["subject"] = "mutated by a caller"
    assert nostr_store._decode(a, SK, True) == {"subject": "one"}, "a cached object was shared"
    assert nostr_store._decode(b, SK, True) == {"subject": "two"}, "an edited document was served stale"
