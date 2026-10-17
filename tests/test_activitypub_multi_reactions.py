"""SEVERAL EMOJI REACTIONS FROM ONE PERSON ON ONE POST, across the fediverse bridge.

Outbound: a member's FIRST live reaction on a post goes as the `Like` that every server understands;
a further, DIFFERENT emoji on the same post goes as an `EmojiReact` (Pleroma/Akkoma keep many per
person; Mastodon ignores the type -- a second Like would be a duplicate favourite there, and undoing it
would take away the favourite the first reaction still holds). The same emoji twice goes once. Each
deletion sends the Undo of exactly the activity that went out, of the SAME type.

Inbound: each EmojiReact (or Misskey Like) is its own kind-7 from the sender's puppet; an Undo of one
deletes only that one."""
import pytest

from app.services.activitypub import convert, inbox, outbox
from tests import test_activitypub as ap
from tests.test_activitypub import ALICE, BASE, EMO, REMOTE, _mirrored_carol_note, _relay_filter, member_post, run


@pytest.fixture
def world(monkeypatch):
    """The AP suite's in-memory world (settings, relay, documents, remote servers)."""
    return ap.world.__wrapped__(monkeypatch)

TARGET = "7" * 64
INBOX = "https://mastodon.example/inbox"


def _react(world, content, created, extra=()):
    ev = member_post(content, kind=7, tags=[["e", TARGET], ["p", world["carol"]], ["k", "1"], *extra],
                     created=created)
    world["relay"][ev["id"]] = ev          # what the delivery pass reads it from
    return ev


def _setup(world, monkeypatch):
    world["carol"] = _mirrored_carol_note(world)
    _relay_filter(world, monkeypatch)


def test_a_second_different_emoji_goes_out_as_an_emoji_react(world, monkeypatch):
    _setup(world, monkeypatch)
    fire = _react(world, "🔥", 1_700_000_100)
    laugh = _react(world, "😂", 1_700_000_101)
    blob = _react(world, ":blobcat:", 1_700_000_102, extra=[["emoji", "blobcat", EMO]])

    first = run(outbox.plan(fire, ALICE))
    assert [(i, a["type"], a["content"]) for i, a in first] == [(INBOX, "Like", "🔥")]

    second = run(outbox.plan(laugh, ALICE))
    assert [(i, a["type"]) for i, a in second] == [(INBOX, "EmojiReact")], second
    act = second[0][1]
    assert act["content"] == "😂" and act["object"] == "https://mastodon.example/notes/7"
    assert act["id"] == convert.activity_url(BASE, laugh["id"])
    assert "_misskey_reaction" not in act

    third = run(outbox.plan(blob, ALICE))[0][1]
    assert third["type"] == "EmojiReact" and third["content"] == ":blobcat:"
    assert third["tag"][0]["icon"]["url"] == EMO, "a custom extra reaction lost its image"


def test_the_same_emoji_twice_goes_out_once(world, monkeypatch):
    _setup(world, monkeypatch)
    fire = _react(world, "🔥", 1_700_000_100)
    again = _react(world, "🔥️", 1_700_000_101)
    assert run(outbox.plan(fire, ALICE))[0][1]["type"] == "Like"
    assert run(outbox.plan(again, ALICE)) == []


def test_removing_one_reaction_undoes_exactly_that_one_with_its_own_type(world, monkeypatch):
    _setup(world, monkeypatch)
    fire = _react(world, "🔥", 1_700_000_100)
    laugh = _react(world, "😂", 1_700_000_101)
    run(outbox.plan(fire, ALICE))
    run(outbox.plan(laugh, ALICE))

    del world["relay"][laugh["id"]]        # the relay drops a deleted reaction
    undo = run(outbox.plan(member_post("", kind=5, tags=[["e", laugh["id"]], ["k", "7"]], created=1_700_000_110), ALICE))
    assert len(undo) == 1 and undo[0][0] == INBOX
    a = undo[0][1]
    assert a["type"] == "Undo" and a["object"]["type"] == "EmojiReact", \
        "an Undo(Like) of the extra reaction would remove the person's one favourite on Mastodon"
    assert a["object"]["id"] == convert.activity_url(BASE, laugh["id"])
    assert a["object"]["object"] == "https://mastodon.example/notes/7"

    undo_fire = run(outbox.plan(member_post("", kind=5, tags=[["e", fire["id"]], ["k", "7"]], created=1_700_000_111), ALICE))
    assert [(x[1]["object"]["type"], x[1]["object"]["id"]) for x in undo_fire] \
        == [("Like", convert.activity_url(BASE, fire["id"]))]


def test_once_the_first_is_gone_the_next_reaction_is_a_like_again(world, monkeypatch):
    _setup(world, monkeypatch)
    fire = _react(world, "🔥", 1_700_000_100)
    run(outbox.plan(fire, ALICE))
    del world["relay"][fire["id"]]
    laugh = _react(world, "😂", 1_700_000_120)
    assert run(outbox.plan(laugh, ALICE))[0][1]["type"] == "Like"


def test_an_unreadable_relay_falls_back_to_a_like(world, monkeypatch):
    world["carol"] = _mirrored_carol_note(world)
    from app.services import nostr_store

    async def down(*a, **kw):
        raise RuntimeError("relay did not answer")
    monkeypatch.setattr(nostr_store, "_ws_query", down)
    ev = _react(world, "😂", 1_700_000_101)
    assert run(outbox.plan(ev, ALICE))[0][1]["type"] == "Like"


def test_several_incoming_emoji_reacts_are_several_reactions_and_an_undo_removes_one(world):
    mine = member_post("react to me")
    world["relay"][mine["id"]] = mine
    target = convert.object_url(BASE, mine["id"])
    acts = [{"id": f"https://mastodon.example/react/{i}", "type": "EmojiReact", "actor": REMOTE,
             "object": target, "content": e} for i, e in enumerate(["🔥", "😂", "🎉"])]
    for a in acts:
        assert run(inbox.process(a, REMOTE)) == "like stored"
    got = [e for e in world["relay"].values() if e["kind"] == 7]
    assert sorted(e["content"] for e in got) == ["🎉", "🔥", "😂"]
    assert len({e["pubkey"] for e in got}) == 1, "one person, one puppet"
    by = {e["content"]: e["id"] for e in got}

    assert run(inbox.process({"id": "https://mastodon.example/undo/1", "type": "Undo", "actor": REMOTE,
                              "object": acts[1]}, REMOTE)) == "undone"
    dels = [e for e in world["relay"].values() if e["kind"] == 5]
    assert len(dels) == 1 and [t[1] for t in dels[0]["tags"] if t[0] == "e"] == [by["😂"]]
