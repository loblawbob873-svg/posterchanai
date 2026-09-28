"""poster.place FETCHES from the fediverse, like Pleroma -- not only waits for deliveries.

Run: venv-unified/bin/python -m pytest tests/test_activitypub_backfill.py

Reported 2026-09-27: JohnSmith87298753@nicecrew.digital "is missing 13 hours of posts" -- measured, 1
of his last 20 posts had reached poster.place. Nobody here follows him, the inbox (rightly) accepts
only what is sent to it, and NOTHING ever read anything back: no initial posts for a newly met
account, no history on follow, one parent level for a reply, no replies for a thread. Built on the
in-memory fediverse of tests/test_activitypub.py (remote servers, relay, database, documents).
"""
import asyncio
import json

import pytest

from app.services.activitypub import backfill, config, inbox, remote, state
from tests.test_activitypub import REMOTE, ALICE, run, world  # noqa: F401  (fixture)

OUTBOX = REMOTE + "/outbox"


def _note(i, author=REMOTE, public=True, **extra):
    return {"id": f"https://mastodon.example/notes/{i}", "type": "Note", "attributedTo": author,
            "content": f"<p>post {i}</p>", "published": f"2026-09-2{i % 10}T10:00:00Z",
            "to": [config.PUBLIC] if public else [], "cc": [], **extra}


@pytest.fixture
def fedi(world, monkeypatch):
    """world + an outbox and paged collections, served by remote.fetch_json from `pages`."""
    world["actors"][REMOTE]["outbox"] = OUTBOX
    pages = {}
    fetched = []

    async def fetch_json(url, signed=True):
        fetched.append(url)
        if url in pages:
            return json.loads(json.dumps(pages[url]))
        if url in world["objects"]:
            return json.loads(json.dumps(world["objects"][url]))
        raise remote.FetchError("not found " + url)
    monkeypatch.setattr(remote, "fetch_json", fetch_json)
    backfill._sighted.clear()
    backfill._inflight.clear()
    backfill._host_last.clear()
    backfill._host_rest.clear()
    backfill._discovery_times.clear()
    backfill._last_bg = 0.0
    monkeypatch.setattr(backfill, "NOTE_GAP", 0.0)
    world["pages"], world["fetched"] = pages, fetched
    return world


def _outbox(pages, items, next_items=None):
    pages[OUTBOX] = {"id": OUTBOX, "type": "OrderedCollection", "first": OUTBOX + "?page=1"}
    first = {"id": OUTBOX + "?page=1", "type": "OrderedCollectionPage", "orderedItems": items}
    if next_items is not None:
        first["next"] = OUTBOX + "?page=2"
        pages[OUTBOX + "?page=2"] = {"id": OUTBOX + "?page=2", "type": "OrderedCollectionPage",
                                    "orderedItems": next_items}
    pages[OUTBOX + "?page=1"] = first


def _stored(world):
    return sorted(e["content"] for e in world["relay"].values() if e.get("kind") == 1)


# ---- the reported case: an account nobody here follows ------------------------------------------

def test_an_unfollowed_accounts_history_comes_in_from_its_own_server(fedi):
    items = [{"type": "Create", "object": _note(i)} for i in range(5, 0, -1)]      # newest first
    _outbox(fedi["pages"], items)
    counts = run(backfill.backfill_actor(REMOTE))
    assert counts.get("stored") == 5, counts
    assert _stored(fedi) == [f"post {i}" for i in range(1, 6)]
    # Oldest first, so the newest lands last -- arrival order, like a live delivery.
    order = [e["content"] for e in fedi["relay"].values() if e.get("kind") == 1]   # insertion order
    assert order == [f"post {i}" for i in range(1, 6)]


def test_only_its_own_public_posts_on_its_own_server(fedi):
    items = [
        {"type": "Create", "object": _note(1)},
        {"type": "Announce", "object": "https://elsewhere.example/notes/9"},        # a boost: not its history
        {"type": "Create", "object": _note(2, public=False)},                       # followers-only
        {"type": "Create", "object": _note(3, author="https://mastodon.example/users/dave")},
        {"type": "Create", "object": {**_note(4), "id": "https://evil.example/notes/4"}},  # not on its server
    ]
    _outbox(fedi["pages"], items)
    run(backfill.backfill_actor(REMOTE))
    assert _stored(fedi) == ["post 1"], "something that is not the account's own public post was stored"


def test_it_reads_on_to_the_next_page_and_stops_at_the_limit(fedi):
    _outbox(fedi["pages"], [{"type": "Announce", "object": "x"}] * 3,
            [{"type": "Create", "object": _note(i)} for i in range(1, 30)])
    counts = run(backfill.backfill_actor(REMOTE))
    assert counts.get("stored") == backfill.RECENT


def test_an_account_is_read_once_per_window_and_a_failed_read_marks_nothing(fedi):
    fedi["pages"].clear()                                   # its outbox cannot be read
    with pytest.raises(remote.FetchError):
        run(backfill.backfill_actor(REMOTE))
    assert not any(k.startswith(backfill._MARK) for k in fedi["docs"]), "a failed read was marked as done"
    _outbox(fedi["pages"], [{"type": "Create", "object": _note(1)}])
    assert run(backfill.backfill_actor(REMOTE)).get("stored") == 1
    assert run(backfill.backfill_actor(REMOTE)) == {"skipped": "read recently"}


# ---- the triggers ---------------------------------------------------------------------------------

def test_a_newly_met_account_brings_its_history_but_not_a_crawl(fedi, monkeypatch):
    """Pleroma's "fetch initial posts": the first sighting in live traffic schedules it once; accounts met
    INSIDE a backfill (a fetched parent's author) do not, or one account's history would crawl the net."""
    asked = []
    monkeypatch.setattr(backfill, "schedule", lambda a, **k: asked.append(a) or True)
    backfill.discovered(REMOTE)
    backfill.discovered(REMOTE)
    assert asked == [REMOTE]
    token = backfill._fetching.set(True)
    try:
        backfill.discovered("https://other.example/users/x")
    finally:
        backfill._fetching.reset(token)
    assert asked == [REMOTE], "an account met during a backfill was queued -- that is a crawler"


def test_a_delivered_post_from_a_new_account_queues_its_history(fedi, monkeypatch):
    fedi["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    asked = []
    monkeypatch.setattr(backfill, "schedule", lambda a, **k: asked.append(a) or True)
    act = {"id": "https://mastodon.example/notes/1/activity", "type": "Create", "actor": REMOTE, "object": _note(1)}
    assert run(inbox.process(act, REMOTE)) == "stored"
    assert asked == [REMOTE]


def test_following_somebody_brings_their_history(fedi, monkeypatch):
    asked = []
    monkeypatch.setattr(backfill, "schedule", lambda a, **k: asked.append(a) or True)
    fid = "https://poster.test/ap/users/alice#follows/x"
    fedi["docs"][f"pcai:ap:following:{ALICE}:{state._h(REMOTE)}"] = {"actor": REMOTE, "inbox": REMOTE + "/inbox",
                                                                     "state": "pending", "id": fid}
    accept = {"type": "Follow", "id": fid, "actor": "https://poster.test/ap/users/alice", "object": REMOTE}
    assert run(inbox._accept("Accept", accept, REMOTE)) == "follow accepted"
    assert asked == [REMOTE]


def test_accounts_followed_before_this_existed_are_caught_up_a_few_at_a_time(fedi, monkeypatch):
    others = [f"https://mastodon.example/users/u{i}" for i in range(5)]
    for i, a in enumerate(others):
        fedi["docs"][f"pcai:ap:following:{ALICE}:{i}"] = {"actor": a, "inbox": "i", "state": "accepted"}
    done = []

    async def fake(actor, **k):
        done.append(actor)
        await state._put(backfill._MARK + state._h(actor), {"actor": actor, "at": 1})
        return {}
    monkeypatch.setattr(backfill, "backfill_actor", fake)
    state.forget_followed_cache()
    assert run(backfill.catch_up(per_tick=2)) == 2
    assert run(backfill.catch_up(per_tick=2)) == 2
    assert run(backfill.catch_up(per_tick=2)) == 1
    assert run(backfill.catch_up(per_tick=2)) == 0, "an account already caught up was read again"
    assert sorted(done) == sorted(others)


# ---- threads --------------------------------------------------------------------------------------

def test_a_reply_brings_its_whole_missing_thread_not_one_level(fedi):
    fedi["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    chain = [_note(1)] + [_note(i, inReplyTo=f"https://mastodon.example/notes/{i - 1}") for i in range(2, 6)]
    for n in chain[:-1]:
        fedi["objects"][n["id"]] = n
    act = {"id": chain[-1]["id"] + "/activity", "type": "Create", "actor": REMOTE, "object": chain[-1]}
    assert run(inbox.process(act, REMOTE)) == "stored"
    assert _stored(fedi) == [f"post {i}" for i in range(1, 6)], "only one ancestor was fetched"


def test_opening_a_thread_brings_its_replies(fedi):
    root = _note(1, replies={"id": "https://mastodon.example/notes/1/replies", "type": "Collection",
                             "first": {"type": "CollectionPage", "items": ["https://mastodon.example/notes/2"],
                                       "next": "https://mastodon.example/notes/1/replies?page=2"}})
    fedi["objects"][root["id"]] = root
    fedi["objects"]["https://mastodon.example/notes/2"] = _note(2, inReplyTo=root["id"])
    dave = "https://other.example/users/dave"
    fedi["actors"][dave] = {**fedi["actors"][REMOTE], "id": dave, "preferredUsername": "dave"}
    fedi["objects"]["https://other.example/notes/3"] = _note(3, author=dave, inReplyTo=root["id"],
                                                             id="https://other.example/notes/3")
    fedi["pages"]["https://mastodon.example/notes/1/replies?page=2"] = {
        "type": "CollectionPage", "items": ["https://other.example/notes/3"]}
    counts = run(backfill.thread_replies(root["id"]))
    assert counts.get("stored", 0) >= 2, counts
    assert {"post 2", "post 3"} <= set(_stored(fedi)), "a reply from an account nobody follows was missed"


# ---- the endpoints can only be pointed at what this node recorded -----------------------------------

def test_the_endpoints_resolve_ids_to_recorded_addresses_only(fedi, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import client as client_router
    asked = []

    async def fake_actor(actor, **k):
        asked.append(("actor", actor))
        return {"stored": 3}

    async def fake_thread(uri, **k):
        asked.append(("thread", uri))
        return {"stored": 1}
    monkeypatch.setattr(backfill, "backfill_actor", fake_actor)
    monkeypatch.setattr(backfill, "thread_replies", fake_thread)
    client_router._ap_fetch_hits.clear()
    app = FastAPI()
    app.include_router(client_router.router)
    c = TestClient(app)
    # A stranger's key: not a fediverse account -- nothing is fetched.
    r = c.post("/client/ap/fetch-profile", json={"pubkey": "c3" * 32}).json()
    assert r == {"ok": True, "fediverse": False} and asked == []
    # A puppet this node recorded: its OWN actor address is read.
    from app.models import FediPuppet
    db = fedi["Session"]()
    db.add(FediPuppet(actor_uri=REMOTE, acct="carol@mastodon.example", instance_host="mastodon.example",
                      pubkey_hex="d4" * 32, nip05_name="carol_mastodon"))
    db.commit()
    db.close()
    r = c.post("/client/ap/fetch-profile", json={"pubkey": "d4" * 32}).json()
    assert r["fediverse"] and r["result"] == {"stored": 3} and asked == [("actor", REMOTE)]
    assert c.post("/client/ap/fetch-profile", json={"pubkey": "https://evil.example/"}).status_code == 400
    assert c.post("/client/ap/fetch-thread", json={"event_id": "e5" * 32}).json() == {"ok": True, "fediverse": False}


# ---- pace: other people's servers and our own relay -------------------------------------------------

def _slept(monkeypatch):
    waits = []
    real = asyncio.sleep

    async def sleep(t, *a, **k):
        waits.append(t)
        await real(0)
    monkeypatch.setattr(backfill.asyncio, "sleep", sleep)
    return waits


def test_background_reads_are_spaced_and_one_server_is_not_read_twice_in_a_row(fedi, monkeypatch):
    waits = _slept(monkeypatch)
    _outbox(fedi["pages"], [{"type": "Create", "object": _note(1)}])
    dave = "https://mastodon.example/users/dave"          # same server as carol
    fedi["actors"][dave] = {**fedi["actors"][REMOTE], "id": dave, "outbox": OUTBOX}
    assert run(backfill.backfill_actor(REMOTE, background=True)).get("stored") == 1
    got = run(backfill.backfill_actor(dave, background=True))
    assert got == {"skipped": "server read recently"}, "one server was read twice in a row in the background"
    assert not any(k.startswith(backfill._MARK + state._h(dave)) for k in fedi["docs"]), \
        "a read skipped for pacing was marked done -- it would never happen"
    # A different server is next in line -- after the node-wide pause.
    other = "https://other.example/users/erin"
    fedi["actors"][other] = {**fedi["actors"][REMOTE], "id": other, "outbox": "https://other.example/outbox"}
    fedi["pages"]["https://other.example/outbox"] = {"orderedItems": []}
    run(backfill.backfill_actor(other, background=True))
    assert any(w > backfill.PACE - 5 for w in waits), f"background reads were not spaced: {waits}"


def test_a_server_that_says_it_is_busy_is_left_alone(fedi, monkeypatch):
    async def busy(url, signed=True):
        raise remote.FetchError("HTTP 429 from mastodon.example")
    monkeypatch.setattr(remote, "fetch_json", busy)
    with pytest.raises(remote.FetchError):
        run(backfill.backfill_actor(REMOTE))
    assert run(backfill.backfill_actor(REMOTE)) == {"skipped": "server is resting"}


def test_discovery_is_capped_per_hour(fedi, monkeypatch):
    asked = []
    monkeypatch.setattr(backfill, "schedule", lambda a, **k: asked.append(a) or True)
    for i in range(backfill.DISCOVERY_PER_HOUR + 30):
        backfill.discovered(f"https://s{i}.example/users/u")
    assert len(asked) == backfill.DISCOVERY_PER_HOUR


def test_our_relay_gets_a_gap_between_posts(fedi, monkeypatch):
    monkeypatch.setattr(backfill, "NOTE_GAP", 0.25)
    waits = _slept(monkeypatch)
    _outbox(fedi["pages"], [{"type": "Create", "object": _note(i)} for i in range(1, 6)])
    run(backfill.backfill_actor(REMOTE))
    assert waits.count(0.25) == 5
