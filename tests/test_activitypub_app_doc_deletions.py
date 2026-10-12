"""A MEMBER'S DELETION OF AN APP DOCUMENT NEVER REACHES THE FEDIVERSE.

2026-10-11, measured on server1: the node's own tables (DocTable: the fedi ledger prune, push
records, ...) delete their 30078 documents with kind-5s signed by the OPERATOR key -- and on a node
whose operator is also a person with an account, that key is a MEMBER. Delivery read each of those
~9,400 deletions a day as "this member deleted a post": two relay reads apiece, then a `Delete` of a
Note that never existed sent to every follower's inbox. Every delivery pass outran its time limit
before it could save its cursor, so for 22 hours nothing a member did federated -- posts, likes and
a report filed to an Akkoma server included -- while followers' servers were sent thousands of
Deletes of nothing.

A deletion that names only things the fediverse never received (an `a` coordinate or a `k` kind
outside what delivery sends) plans nothing, without a relay read. A real post deletion still goes out.
"""
from app.services.activitypub import outbox
from tests.test_activitypub import ALICE, member_post, run
from tests.test_activitypub import world  # noqa: F401  (fixture)

FOLLOWER = "https://other.example/inbox"


def _follower(world):
    world["docs"][f"pcai:ap:follower:{ALICE}:2"] = {"actor": "https://other.example/users/f", "inbox": FOLLOWER}


def test_deleting_an_app_document_sends_nothing_and_reads_nothing(world, monkeypatch):
    _follower(world)
    reads = []
    real_rec, real_ev = outbox._sent_rec, outbox._event
    monkeypatch.setattr(outbox, "_sent_rec", lambda i: reads.append(i) or real_rec(i))
    monkeypatch.setattr(outbox, "_event", lambda i: reads.append(i) or real_ev(i))
    doc = member_post("", kind=5, tags=[["e", "7a" * 32],
                                        ["a", f"30078:{ALICE}:pcai:t:fedi_ledger_index:u:191dd27c"]])
    assert run(outbox.plan(doc, ALICE)) == [], "an app document's deletion was sent as a Delete of a Note"
    assert reads == [], "a deletion of an app document cost relay reads in the delivery pass"
    by_k = member_post("", kind=5, tags=[["e", "7b" * 32], ["k", "30078"]], created=1_700_000_101)
    assert run(outbox.plan(by_k, ALICE)) == []


def test_a_post_deletion_still_goes_out(world):
    _follower(world)
    gone = "ab" * 32
    (job,) = run(outbox.plan(member_post("", kind=5, tags=[["e", gone], ["k", "1"]], created=1_700_000_102), ALICE))
    assert job[0] == FOLLOWER and job[1]["type"] == "Delete"
    # A deletion naming a post AND an app document still deletes the post.
    mixed = member_post("", kind=5, tags=[["e", gone], ["k", "1"], ["k", "30078"]], created=1_700_000_103)
    assert [a["type"] for _, a in run(outbox.plan(mixed, ALICE))] == ["Delete"]


def test_a_pass_cut_off_by_the_time_limit_keeps_what_it_did(world, monkeypatch):
    """The tick runs under a time limit; a pass cancelled there used to save nothing, so the next
    tick re-read the same backlog from the same cursor -- for ever, if the backlog never fit."""
    import asyncio
    from app.services import nostr_store
    from app.services.activitypub import state
    _follower(world)
    evs = [member_post(f"post {i}", created=1_700_000_001 + i) for i in range(3)]

    async def ws_query(port, filters, strict=False, **kw):
        return sorted(evs, key=lambda e: -e["created_at"])
    monkeypatch.setattr(nostr_store, "_ws_query", ws_query)
    monkeypatch.setattr(outbox, "_SAVE_EVERY", 0)
    real_plan = outbox.plan

    async def plan(ev, member):
        if ev["id"] == evs[2]["id"]:
            await asyncio.sleep(3600)             # the event the time limit lands on
        return await real_plan(ev, member)
    monkeypatch.setattr(outbox, "plan", plan)
    world["docs"][state.CURSOR] = {"since": 1_700_000_000}

    async def tick_with_limit():
        try:
            await asyncio.wait_for(outbox.tick(), timeout=0.5)
        except asyncio.TimeoutError:
            pass
    run(tick_with_limit())
    assert world["docs"][state.CURSOR]["since"] == 1_700_000_002, "the work done before the time limit was lost"
