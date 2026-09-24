"""A bot answers a mention made as a REPLY.

This client sends every reply to a note as a NIP-22 comment (kind 1111). The bots fetched only
kind-1 mentions, so "@posterchan" in a reply was never seen -- "the posterchan bot never replies to
me" -- and a bot answering a comment with a NIP-10 kind-1 reply would have put its answer outside
the thread. Runs the shipped fetch / shape / reply code with the relay stubbed."""
import asyncio

from app.services.nostr import event as E
from app.services.nostr import nostr_service as ns

BOT = "c7" * 32
ME = "4b" * 32
ROOT_ID, ROOT_AUTHOR = "aa" * 32, "bb" * 32


def _comment():
    return {"id": "cc" * 32, "pubkey": ME, "kind": 1111, "created_at": 1_790_000_000, "content": "hey bot",
            "tags": [["E", ROOT_ID, "", ROOT_AUTHOR], ["K", "1"], ["P", ROOT_AUTHOR],
                     ["e", ROOT_ID, "", ROOT_AUTHOR], ["k", "1"], ["p", ROOT_AUTHOR], ["p", BOT]]}


def test_mentions_include_comments(monkeypatch):
    seen = {}

    async def query(relays, filters, **kw):
        seen["kinds"] = filters[0]["kinds"]
        return [_comment()]
    monkeypatch.setattr(ns.relay, "query", query)
    got = asyncio.run(ns.fetch_mentions(BOT, ["ws://127.0.0.1:3052"]))
    assert 1111 in seen["kinds"] and got[0]["kind"] == 1111


def test_the_bot_listener_accepts_a_comment():
    import importlib, sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "botframework"))
    bot = importlib.import_module("nostr")
    note = bot._shape_note(_comment())
    assert note and note["_event"]["kind"] == 1111 and note["text"] == "hey bot"


def test_a_reply_to_a_comment_is_a_comment_in_the_same_thread(monkeypatch):
    sent = {}

    async def publish(relays, ev):
        sent["ev"] = ev
        return True
    monkeypatch.setattr(ns.relay, "publish", publish)
    sk = bytes([7]) * 32
    ev = asyncio.run(ns.post_note(sk, ["ws://127.0.0.1:3052"], "hello", reply_to=_comment()))
    assert ev["kind"] == 1111
    tags = ev["tags"]
    assert ["E", ROOT_ID, "", ROOT_AUTHOR] in tags and ["K", "1"] in tags, "the thread's root was lost"
    assert ["e", "cc" * 32, "", ME] in tags and ["k", "1111"] in tags and ["p", ME] in tags
    # A reply to an ordinary note is still an ordinary NIP-10 reply.
    note = {"id": "dd" * 32, "pubkey": ME, "kind": 1, "tags": [], "content": "x"}
    ev1 = asyncio.run(ns.post_note(sk, ["ws://127.0.0.1:3052"], "hi", reply_to=note))
    assert ev1["kind"] == 1 and ["e", "dd" * 32, "", "root"] in ev1["tags"]
