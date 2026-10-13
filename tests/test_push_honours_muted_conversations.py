"""A MUTED CONVERSATION STOPS BUZZING THE PHONE -- the server half of #196 ("mute conversation not working
for fedi conversations").

Mastodon and Pleroma mention EVERY participant in every reply, so in a fediverse thread each new reply is a
mention of you, and the push watcher sent one per reply whatever your mute list said: it never read the
list at all (muted PEOPLE leaked through it too). Drives the real `_poll()` with the relay answers stubbed:
the mute list is the recipient's public NIP-51 kind-10000, exactly as the client's toggleMuteThread writes it.
"""
import asyncio

import pytest

from app.services import nostr_push_service as nps
from tests import push_relay_harness as H

ME = "a" * 64
FEDI = "b" * 64        # a fediverse puppet replying in the thread
MUTED = "c" * 64       # a person ME muted
ROOT = "1" * 64        # the conversation ME muted
OTHER_ROOT = "2" * 64


def _reply(eid, author, root, parent=None):
    tags = [["e", root, "", "root"]] + ([["e", parent, "", "reply"]] if parent else []) + [["p", ME]]
    return {"id": eid, "kind": 1, "pubkey": author, "created_at": 100, "content": "hi", "tags": tags, "sig": ""}


@pytest.fixture
def world(tmp_path, monkeypatch):
    r = H.start(tmp_path, monkeypatch)
    monkeypatch.setattr(nps, "_seen", set())
    H.add_sub(pubkey=ME, endpoint="https://push.example/phone", transport="webpush", p256dh="p", auth="a")
    lists, poll_events, sent = [], [], []

    async def query(relays, filters, timeout=8):
        if filters[0].get("kinds") == [10000]:
            return [e for e in lists if e["pubkey"] in filters[0]["authors"]]
        return list(poll_events)
    monkeypatch.setattr(nps.relay, "query", query)

    async def name(pk):
        return "someone"
    monkeypatch.setattr(nps, "_name_for", name)
    monkeypatch.setattr(nps.push_service, "send", lambda sub, payload: sent.append(payload) or True)
    monkeypatch.setattr(nps, "subscription_dict", lambda s: {"endpoint": s["endpoint"]})

    def poll(*events):
        poll_events[:] = events
        nps._cursor = 1
        asyncio.run(nps._poll())
        return [p["eid"] for p in sent]
    yield {"poll": poll, "lists": lists}
    H.reset()
    r.close()


def _mutes(*tags):
    return {"id": "f" * 64, "kind": 10000, "pubkey": ME, "created_at": 50, "content": "", "tags": list(tags), "sig": ""}


def test_a_reply_in_a_muted_conversation_is_not_pushed(world):
    world["lists"].append(_mutes(["e", ROOT]))
    sent = world["poll"](_reply("3" * 64, FEDI, ROOT), _reply("4" * 64, FEDI, OTHER_ROOT))
    assert sent == ["4" * 64], "a reply in the muted conversation still buzzed the phone"


def test_a_deep_reply_that_only_names_its_parent_is_still_in_the_thread(world):
    """The client's rule (_mutedThread): the event or ANY e tag it carries."""
    world["lists"].append(_mutes(["e", "5" * 64]))
    assert world["poll"](_reply("6" * 64, FEDI, ROOT, parent="5" * 64)) == []


def test_a_muted_person_is_not_pushed(world):
    world["lists"].append(_mutes(["p", MUTED]))
    assert world["poll"](_reply("7" * 64, MUTED, OTHER_ROOT), _reply("8" * 64, FEDI, OTHER_ROOT)) == ["8" * 64]


def test_an_unreadable_mute_list_mutes_nothing(world, monkeypatch):
    """Fails OPEN, like every push filter: "could not ask" never swallows a notification."""
    real = nps.relay.query

    async def flaky(relays, filters, timeout=8):
        if filters[0].get("kinds") == [10000]:
            raise TimeoutError
        return await real(relays, filters, timeout)
    monkeypatch.setattr(nps.relay, "query", flaky)
    world["lists"].append(_mutes(["e", ROOT]))
    assert world["poll"](_reply("9" * 64, FEDI, ROOT)) == ["9" * 64]
