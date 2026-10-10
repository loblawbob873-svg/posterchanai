"""The unfollow bot on Nostr ("make our unfollow bot work on Nostr").

It ran only against Pleroma's database (`following_relationships`) and refused a Nostr bot outright. Now a
Nostr bot reads who follows the instance's members from the node: Nostr contact lists (kind 3) and the
fediverse's Undo(Follow) tombstones recorded by the ActivityPub server.

The decision table is tested on its own (`diff` is pure), one rule per test, because every rule here exists to
stop a FALSE "X unfollowed Y" going out publicly with a real person's name in it.
"""
import os
import sys
import time

import pytest

from tests.test_community_bots import ALICE, BOB, CAROL_PUPPET, STRANGER, _botmod, _ev, _run_bot, run, world  # noqa: F401

F1, F2, BOT = "e5" * 32, "f6" * 32, "99" * 32


def _l(author, at, follows, size=None):
    return {"author": author, "at": at, "size": len(follows) if size is None else size, "follows": list(follows)}


@pytest.fixture
def bot(monkeypatch, tmp_path):
    here = os.path.join(os.path.dirname(__file__), "..", "botframework")
    monkeypatch.syspath_prepend(here)
    mod, _ = _botmod(monkeypatch, tmp_path, "nostr_unfollowbot")
    return mod


def _diff(bot, old, picture, confirmed=(), bots=(), fedi=(), fedi_at=0, had=True):
    return bot.diff(old, list(picture), list(confirmed), set(bots), list(fedi), fedi_at, had)


def test_a_newer_list_without_the_member_is_an_unfollow(bot):
    old = {F1: {"at": 100, "size": 3, "follows": [ALICE, BOB]}}
    found, state = _diff(bot, old, [_l(F1, 200, [BOB], size=2)])
    assert found == [(F1, ALICE, "nostr")]
    assert state["nostr"][F1]["follows"] == [BOB]


def test_the_first_look_only_remembers(bot):
    found, state = _diff(bot, {}, [_l(F1, 200, [ALICE])], fedi=[{"member": ALICE, "actor": "a", "gone": True,
                                                                 "at": 50, "ref": "@x@y"}], had=False)
    assert found == [] and F1 in state["nostr"] and state["fedi_at"] == 50


def test_no_newer_list_is_no_unfollow(bot):
    old = {F1: {"at": 200, "size": 2, "follows": [ALICE, BOB]}}
    assert _diff(bot, old, [_l(F1, 200, [BOB])])[0] == []        # same timestamp: not a newer decision


def test_a_list_that_vanished_from_the_relay_is_no_evidence(bot):
    old = {F1: {"at": 100, "size": 2, "follows": [ALICE]}}
    found, state = _diff(bot, old, [], confirmed=[])                # pruned, purged, never reached us
    assert found == []
    assert state["nostr"][F1] == old[F1], "forgot the follower, so a later real unfollow could never be seen"


def test_a_follower_who_now_follows_none_of_our_members_is_confirmed_from_their_own_list(bot):
    old = {F1: {"at": 100, "size": 40, "follows": [ALICE]}}
    found, state = _diff(bot, old, [], confirmed=[_l(F1, 300, [], size=39)])
    assert found == [(F1, ALICE, "nostr")]
    assert F1 not in state["nostr"], "somebody following none of our members stays in the snapshot for ever"


def test_a_contact_list_wipe_is_not_announced(bot):
    old = {F1: {"at": 100, "size": 300, "follows": [ALICE, BOB]}}
    assert _diff(bot, old, [], confirmed=[_l(F1, 200, [], size=2)])[0] == [], "a client bug announced as unfollows"


def test_dropping_many_members_at_once_is_a_cleanup_not_news(bot):
    many = ["%064x" % i for i in range(1, 9)]
    old = {F1: {"at": 100, "size": 50, "follows": many}}
    assert _diff(bot, old, [_l(F1, 200, many[:1], size=43)])[0] == []


def test_a_pass_with_too_many_unfollows_announces_none(bot):
    old = {("%064x" % i): {"at": 100, "size": 5, "follows": [ALICE]} for i in range(1, 30)}
    confirmed = [_l(a, 200, [], size=4) for a in old]
    found, _ = _diff(bot, old, [], confirmed=confirmed)
    assert found == []


def test_bots_are_never_announced_on_either_side(bot):
    old = {BOT: {"at": 100, "size": 2, "follows": [ALICE]}, F1: {"at": 100, "size": 2, "follows": [BOT, BOB]}}
    found, _ = _diff(bot, old, [_l(BOT, 200, [], size=1), _l(F1, 200, [BOB], size=1)], bots=[BOT])
    assert found == []


def test_members_and_fediverse_accounts_count_nostr_strangers_do_not(bot):
    """"only work for local instance nip05 users" + "fediverse accounts that unfollow local nip05 accounts should
    count as well": the unfollowed is always a member; the unfollower a member or a fediverse account."""
    members = {ALICE, BOB}
    old = {BOB: {"at": 100, "size": 3, "follows": [ALICE]}, F1: {"at": 100, "size": 3, "follows": [ALICE]}}
    fedi = [{"member": ALICE, "actor": "https://m.example/users/carol", "gone": True, "at": 500, "ref": "@carol@m.example"},
            {"member": STRANGER, "actor": "https://m.example/users/dan", "gone": True, "at": 500, "ref": "@dan@m.example"},
            {"member": BOB, "actor": "https://m.example/users/old", "gone": True, "at": 90, "ref": "@old@m.example"},
            {"member": BOB, "actor": "https://m.example/users/eve", "gone": False, "at": 600, "ref": "@eve@m.example"}]
    found, state = bot.diff(old, [_l(BOB, 200, [], size=2), _l(F1, 200, [], size=2)], [], set(), fedi, 100, True,
                            members=members)
    assert sorted(found) == sorted([(BOB, ALICE, "nostr"), ("@carol@m.example", ALICE, "fediverse")]), found
    again, _ = bot.diff({}, [], [], set(), fedi, state["fedi_at"], True, members=members)
    assert again == [], "a fediverse unfollow was announced twice"


def test_the_post_tags_both_people_and_an_ai_rewording_must_keep_every_name(bot, monkeypatch):
    names = {F1: "nostr:npub1f1", ALICE: "@alice@poster.place"}
    refs = {F1: "nostr:npub1f1", ALICE: "nostr:npub1alice"}
    plain = bot.message([(F1, ALICE, "nostr")], names, refs)
    assert plain == "nostr:npub1f1 unfollowed nostr:npub1alice."
    monkeypatch.setattr(bot, "_ai_on", lambda: True)
    monkeypatch.setattr(bot, "generate_reply", lambda p: "BREAKING: a coward abandoned @alice@poster.place!")
    assert bot.message([(F1, ALICE, "nostr")], names, refs) == "BREAKING: a coward abandoned nostr:npub1alice!"
    monkeypatch.setattr(bot, "generate_reply", lambda p: "Someone left somebody. Drama!")
    assert bot.message([(F1, ALICE, "nostr")], names, refs) == plain, "a reply that lost the member's name went out"


def test_end_to_end_could_not_ask_changes_nothing_and_a_real_unfollow_posts_once(bot, monkeypatch):
    posted = []
    monkeypatch.setattr(bot, "_post", lambda text, *a, **k: posted.append(text))
    pic = {"nostr": [_l(F1, 100, [ALICE, BOB])], "fedi": [], "bots": [], "names": {}, "refs": {F1: "nostr:npub1f1",
           ALICE: "nostr:npub1alice", BOB: "nostr:npub1bob"}, "members": {F1: "@f1@poster.place",
           ALICE: "@alice@poster.place", BOB: "@bob@poster.place"}}
    calls = {"fail": False}

    def get(path, **k):
        if calls["fail"]:
            raise bot.community_api.Unavailable("relay down")
        return pic if path == "follows" else {"lists": []}
    monkeypatch.setattr(bot.community_api, "get", get)
    bot.unfollows()                                   # first look
    assert posted == []
    calls["fail"] = True
    bot.unfollows()                                   # could not ask
    calls["fail"] = False
    pic["nostr"] = [_l(F1, 200, [BOB])]
    bot.unfollows()
    assert posted == ["nostr:npub1f1 unfollowed nostr:npub1alice."], posted
    bot.unfollows()
    assert len(posted) == 1, "announced twice"


# ---------------------------------------------------------------- the server side
def test_the_follows_endpoint_reads_contact_lists_and_fediverse_tombstones(world, monkeypatch):
    from app.services import community_stats, nostr_store
    from app.services.activitypub import state
    now = int(time.time())
    world["relay"] += [_ev(1, BOB, 3, [["p", ALICE], ["p", STRANGER]], created=now - 50),
                       _ev(2, F2, 3, [["p", STRANGER]], created=now - 40),
                       _ev(3, BOB, 3, [["p", ALICE], ["p", BOB]], created=now - 900),   # an older list of the same author
                       _ev(4, F1, 3, [["p", ALICE]], created=now - 30)]   # a stranger following a member: not ours

    async def list_docs(port, prefix, **kw):
        return {prefix + ALICE + ":h1": {"actor": "https://m.example/users/carol", "gone": True, "at": now - 5},
                prefix + STRANGER + ":h2": {"actor": "https://x.example/u/z", "at": now}}
    monkeypatch.setattr(nostr_store, "list_docs", list_docs)
    monkeypatch.setattr(state, "_seckey", lambda: b"\x01" * 32)
    out = run(community_stats.follows())
    assert [(r["author"], r["follows"], r["size"]) for r in out["nostr"]] == [(BOB, [ALICE], 2)], \
        "a non-member's contact list was returned"
    assert [(f["member"], f["gone"]) for f in out["fedi"]] == [(ALICE, True)], "a non-member's follower leaked in"
    lists = run(community_stats.contact_lists([F2]))
    assert lists and lists[0]["follows"] == [] and lists[0]["size"] == 1


def test_a_capped_answer_is_refused_not_read_as_unfollows(world, monkeypatch):
    from app.services import community_stats
    monkeypatch.setattr(community_stats, "_FOLLOW_CAP", 2)
    now = int(time.time())
    world["relay"] += [_ev(i, "%064x" % (100 + i), 3, [["p", ALICE]], created=now) for i in range(3)]
    with pytest.raises(RuntimeError):
        run(community_stats.follows())


def test_a_nostr_only_unfollow_bot_takes_the_nostr_path():
    r = _run_bot("--unfollows-print", env={"NOSTR_NSEC": "11" * 32, "POSTERCHANAI_API_ENDPOINT": "http://127.0.0.1:9"})
    out = r.stdout + r.stderr
    assert "PLEROMA_ENDPOINT is not configured" not in out and "psycopg" not in out, out[-2000:]
    assert "could not read follows" in out, out[-2000:]
