"""A post a SUBSCRIBED ActivityPub relay re-shares is stored as an ordinary post -- the global timeline gets busy
like a fediverse instance's federated timeline, not only with what members follow.

2026-10-18: "our global timeline should be busy regardless of what I follow, just like on activitypub". Relays send
an Announce signed by the RELAY actor; nobody follows that actor, so the inbox dropped every one as "a boost of
something nobody here asked for". `relays.relay_actors()` (the relays the admin listed AND that accepted us) existed
and nothing called it.
"""
import pytest

from app.services.activitypub import config, inbox, relays
from tests.test_activitypub import run, world  # noqa: F401

RELAY = "https://relay.example/actor"
STRANGER = "https://far.example/users/zed"


def _setup(world, monkeypatch, subscribed=True):
    world["actors"][RELAY] = {"id": RELAY, "type": "Application", "preferredUsername": "relay", "inbox": RELAY + "/inbox"}
    world["actors"][STRANGER] = {"id": STRANGER, "type": "Person", "preferredUsername": "zed",
                                 "inbox": STRANGER + "/inbox"}
    note = {"id": "https://far.example/notes/7", "type": "Note", "attributedTo": STRANGER,
            "content": "<p>hello from a server nobody here follows</p>", "to": [config.PUBLIC], "cc": []}
    world["objects"][note["id"]] = note

    async def actors(max_age=60.0):
        return frozenset({RELAY}) if subscribed else frozenset()
    monkeypatch.setattr(relays, "relay_actors", actors)
    return {"id": "https://relay.example/activities/a1", "type": "Announce", "actor": RELAY,
            "to": [config.PUBLIC], "object": note["id"]}


def test_a_subscribed_relays_post_is_stored_as_the_post_itself(world, monkeypatch):
    ann = _setup(world, monkeypatch)
    assert run(inbox.process(ann, RELAY)) == "relayed post stored"
    kinds = sorted(e["kind"] for e in world["relay"].values())
    assert kinds == [1] or kinds.count(6) == 0, "the relay itself must not appear to repost it: %s" % kinds
    note = next(e for e in world["relay"].values() if e["kind"] == 1)
    assert "nobody here follows" in note["content"]
    assert run(inbox.process(ann, RELAY)) == "already stored", "a re-sent Announce must not store a second copy"


def test_an_unsubscribed_relay_is_still_ignored(world, monkeypatch):
    ann = _setup(world, monkeypatch, subscribed=False)
    assert run(inbox.process(ann, RELAY)) == "ignored: boost of something nobody here asked for"
    assert not [e for e in world["relay"].values() if e["kind"] == 1]
