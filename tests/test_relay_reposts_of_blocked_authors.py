"""A blocked author's posts do not come back as somebody else's repost.

2026-10-17: "how is The_Toddstr's posts still making it to our service despite being blocked at the relay?" The
client reads only this relay, and the relay held none of his posts -- but it held 17 kind-6 reposts of them from
other people, and a repost CARRIES the reposted post (its content is that event's JSON). The block checked who
SIGNED, so every repost passed and the client drew his post inside it. Drives the relay's own event path and the
real PosterChanDB purge.
"""
import asyncio
import json

from app.services.nostr.event import build_event
from app.services.nostr.nostr_service import derive_pubkey
from app.services.nostr_relay.server import RelayServer
from tests.test_relay_prune import _ev, _run, store_factory  # noqa: F401  (fixture)

TODD, ALICE, BOB = bytes([7]) * 32, bytes([8]) * 32, bytes([9]) * 32
TODD_PK = derive_pubkey(TODD)


class Gate:
    def is_member(self, _pk): return True
    def is_operator(self, _pk): return False
    def is_puppet_event(self, _ev): return False
    def is_blocked(self, pk): return pk == TODD_PK


class Store:
    def __init__(self):
        self.saved = []
    async def add_event(self, ev, origin="direct", **_kw):
        self.saved.append(ev["id"])
        return True
    async def has_event(self, eid):
        return eid in self.saved


def _server():
    srv = RelayServer(Store(), Gate(), {"wot_enabled": False})
    srv.sent = []
    srv._send = lambda conn, obj: srv.sent.append((conn, obj))
    srv._conn_ips.update({"you": "8.8.4.4"})
    return srv


def test_a_repost_of_a_blocked_author_is_refused_and_one_of_anybody_else_is_not():
    his = build_event(TODD, 1, "a post from a blocked account", [])
    hers = build_event(BOB, 1, "an ordinary post", [])
    reposts = {
        "kind 6 carrying his post": build_event(ALICE, 6, json.dumps(his), [["e", his["id"]], ["p", TODD_PK]]),
        "kind 16 naming him": build_event(ALICE, 16, "", [["e", his["id"]], ["p", TODD_PK], ["k", "1"]]),
        "kind 6 carrying his post, p tag left off": build_event(ALICE, 6, json.dumps(his), [["e", his["id"]]]),
    }
    fine = build_event(ALICE, 6, json.dumps(hers), [["e", hers["id"]], ["p", hers["pubkey"]]])
    srv = _server()

    async def run():
        for e in [*reposts.values(), fine]:
            await srv._on_event("you", e)
    asyncio.new_event_loop().run_until_complete(run())
    oks = {m[1]: (m[2], m[3]) for _c, m in srv.sent if isinstance(m, list) and m[0] == "OK"}
    for name, e in reposts.items():
        assert oks[e["id"]][0] is False and "blocked" in oks[e["id"]][1], (name, oks.get(e["id"]))
        assert e["id"] not in srv.store.saved, name
    assert fine["id"] in srv.store.saved, "a repost of somebody not blocked was refused"


def test_the_block_purge_removes_reposts_already_stored(store_factory):  # noqa: F811
    todd, other = "d" * 64, "e" * 64

    def repost(i, author, embedded=True):
        ev = _ev(i, kind=6, pubkey="c" * 64)
        ev["tags"] = [["e", f"{i + 1000:064x}"], ["p", author]]
        if embedded:
            ev["content"] = json.dumps({"id": f"{i + 1000:064x}", "pubkey": author, "kind": 1, "content": "x"})
        return ev

    async def go(loop):
        store = store_factory(loop)
        await store.add_events_bulk([repost(1, todd), repost(2, todd, embedded=False), repost(3, other),
                                     _ev(4, kind=1, pubkey="c" * 64)])
        assert await store.delete_reposts_of([todd]) == 2
        left = {e["id"] for e in await store.query([{"limit": 100}])}
        assert left == {f"{3:064x}", f"{4:064x}"}, left
    _run(go)


def test_an_old_post_stored_late_is_not_pushed_down_every_open_feed():
    """93 backfilled posts in 150 s reached every phone's live feed (2026-10-17); none could be shown as new.
    A subscription that asked for the thread, the person or the id still gets it."""
    import time as _t
    from app.services.nostr_relay.server import SubscriptionManager
    subs = SubscriptionManager()
    subs.add("feed", "global", [{"kinds": [1, 6, 1068, 5, 30023], "limit": 120}])
    subs.add("thread", "t", [{"kinds": [1], "#e": ["a" * 64]}])
    subs.add("me", "n", [{"kinds": [1, 7], "#p": ["b" * 64]}])
    old = {"id": "1" * 64, "pubkey": "c" * 64, "kind": 1, "created_at": int(_t.time()) - 7200, "content": "x",
           "tags": [["e", "a" * 64], ["p", "b" * 64]]}
    new = {**old, "id": "2" * 64, "created_at": int(_t.time())}
    for ev in (old, new):
        sent = []
        subs.fanout(ev, lambda c, m: sent.append(c), lambda c, e: True)
        if ev is old:
            assert sorted(sent) == ["me", "thread"], sent
        else:
            assert sorted(sent) == ["feed", "me", "thread"], sent


def test_the_live_firehose_refuses_a_members_repost_of_a_blocked_author():
    """Security review 2026-10-18: only the direct-write path refused these. A member's repost arriving from upstream
    (the live firehose -- how almost the whole public feed arrives) was stored and served until the next block purge,
    and that purge finds reposts by `p` tag, so one naming the author only in its content stayed for good."""
    from tests import test_json_spam_every_path as F

    class Blocking(F.Member):
        def is_blocked(self, pk):
            return pk == TODD_PK

    fh, store, srv = F.firehose()
    fh.__globals__["gate"] = Blocking()
    todd_post = build_event(TODD, 1, "a post by a blocked account", [])
    no_p = build_event(ALICE, 6, json.dumps(todd_post), [["e", todd_post["id"]]])            # author only in content
    tagged = build_event(ALICE, 6, json.dumps(todd_post), [["e", todd_post["id"]], ["p", TODD_PK]])
    for ev in (no_p, tagged):
        asyncio.run(fh(ev))
    store.add_event.assert_not_called()
    srv.subs.fanout.assert_not_called()
    asyncio.run(fh(build_event(ALICE, 1, "an ordinary post", [])))             # and nothing else is affected
    store.add_event.assert_called_once()
