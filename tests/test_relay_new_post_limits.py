"""New-post limits: posts per minute and the same thing over and over -- without breaking federation.

"focus more on new post limits per min and saying the same thing over and over" / "make sure you test
and make test cases so it does not break federation". Runs the shipped guard, the relay's own event
path, and checks every route (direct, firehose, sync, backfill, fediverse puppets) consults it.
"""
import asyncio
import inspect
from unittest import mock

from app.services.nostr.event import build_event
from app.services.nostr_relay import ingest, thread
from app.services.nostr_relay.server import RelayServer
from app.services.nostr_relay.spamguard import SpamGuard

ALICE = bytes.fromhex("a1" * 32)


def _ev(kind=1, content=None, pk="a1", ts=1_000_000):
    return {"kind": kind, "pubkey": pk * 32, "created_at": ts, "id": "x", "tags": [],
            "content": content if content is not None else f"post at {ts} {pk}"}


class Gate:
    def __init__(self, puppets=()):
        self.puppets = set(puppets)
    def is_member(self, _pk): return True
    def is_operator(self, _pk): return False
    def is_puppet_event(self, ev): return ev.get("pubkey") in self.puppets
    def is_blocked(self, _pk): return False


class Store:
    def __init__(self):
        self.saved = []
    async def add_event(self, ev, origin="direct", **_kw):
        self.saved.append(ev["id"])
        return True
    async def has_event(self, eid):
        return eid in self.saved


def _server(puppets=(), **cfg):
    srv = RelayServer(Store(), Gate(puppets), {"wot_enabled": False, **cfg})
    srv.sent = []
    srv._send = lambda conn, obj: srv.sent.append((conn, obj))
    srv._conn_ips.update({"you": "8.8.4.4", "lan": "192.168.0.85"})
    return srv


# --- the two limits -------------------------------------------------------------------------------

def test_people_are_never_limited_but_a_feed_burst_is():
    g = SpamGuard({})
    person = [g.check(_ev(ts=1_000_000 + i * 10)) for i in range(5)]            # 5 in one minute
    assert set(person) == {""}
    burst = [g.check(_ev(pk="b2", ts=2_000_000)) for _ in range(35)]           # a feed bot's minute
    assert burst[:10] == [""] * 10 and all(r.startswith("rate-limited:") for r in burst[10:])


def test_saying_the_same_thing_over_and_over_is_stopped():
    g = SpamGuard({})
    scam = "Peace be upon you, my brothers and sisters, please help my family survive."
    res = [g.check(_ev(content=scam, ts=1_000_800 + i * 120)) for i in range(13)]   # one clock hour
    assert res[:3] == [""] * 3 and all(r.startswith("rate-limited:") for r in res[3:])
    assert g.check(_ev(content="gm", ts=1_000_050)) == "", "a short greeting is not spam"


def test_a_catch_up_sync_is_not_a_burst():
    """Posts stamped across an hour, all arriving at once, are judged by their own timestamps."""
    g = SpamGuard({})
    assert set(g.check(_ev(ts=1_000_000 + i * 60)) for i in range(60)) == {""}


def test_zero_turns_each_check_off():
    g = SpamGuard({"posts_per_min": 0, "same_per_hour": 0})
    assert set(g.check(_ev(content="the same words again and again", ts=1)) for _ in range(100)) == {""}


# --- federation must not break --------------------------------------------------------------------

def test_boosts_reactions_and_dms_are_never_counted():
    """A popular fediverse post boosted by many: the boosts carry identical content."""
    g = SpamGuard({"posts_per_min": 1, "same_per_hour": 1})
    for kind in (6, 16, 7, 4, 1059, 30078, 0, 3):
        assert set(g.check(_ev(kind=kind, content="identical boosted content!!!", ts=5)) for _ in range(20)) == {""}


def test_many_people_saying_the_same_thing_is_fine():
    """Fediverse friends all posting 'Happy New Year everyone, all the best!' are people, not spam."""
    g = SpamGuard({})
    text = "Happy New Year everyone, wishing you all the very best for the year!"
    assert set(g.check(_ev(content=text, pk=f"{i:02x}", ts=1_000_000)) for i in range(50)) == {""}


def test_one_noisy_fediverse_account_does_not_hold_up_the_rest():
    keys = [bytes([i + 1]) * 32 for i in range(12)]
    noisy = [build_event(keys[0], 1, f"headline number {n}", []) for n in range(15)]
    stamp = noisy[0]["created_at"]
    others = [build_event(k, 1, "an ordinary post from someone else", [], created_at=stamp) for k in keys[1:]]
    srv = _server(puppets={e["pubkey"] for e in noisy + others})
    async def run():
        for e in noisy + others:
            await srv._on_event("lan", e)
    with mock.patch("app.services.nostr_relay.server.time.time", return_value=float(stamp)):
        asyncio.new_event_loop().run_until_complete(run())
    refused = [m[1] for _c, m in srv.sent if isinstance(m, list) and m[0] == "OK" and m[2] is False]
    noisy_ids = {e["id"] for e in noisy}
    assert len(refused) == 5 and set(refused) <= noisy_ids, ("only the noisy account's extra posts are refused", len(refused))
    assert {e["id"] for e in others} <= set(srv.store.saved), "other fediverse accounts' posts were held up"


def test_fediverse_posts_are_checked_though_the_bridge_is_on_the_lan_and_our_app_is_not():
    srv = _server(puppets={"c3" * 32}, posts_per_min=2)
    def limited(conn, pk):
        res = []
        for i in range(3):
            ev = _ev(pk=pk, ts=1_000_000)
            res.append(bool(srv.spam.applies(ev) and (srv.gate.is_puppet_event(ev) or not srv._is_internal(srv._conn_ips.get(conn, "?"))) and srv.spam.check(ev)))
        return res
    assert limited("lan", "c3") == [False, False, True], "a fediverse puppet's burst was not limited"
    assert limited("lan", "d4") == [False, False, False], "our own app on the LAN was limited"
    assert limited("you", "e5") == [False, False, True]


def test_the_inbox_answers_remote_servers_before_the_relay_decides():
    """A refused post must never become an HTTP error to the sending server (Mastodon marks failing
    inboxes dead): the inbox schedules the work and answers 202 first."""
    from app.routers import activitypub as ap
    src = inspect.getsource(ap)
    i = src.index("if not inbox.schedule(activity, signer):")
    assert "return Response(status_code=202)" in src[i:i + 200]


def test_every_route_into_the_timeline_consults_the_limits():
    assert "_spam.check(ev)" in inspect.getsource(thread), "the live firehose skips it"
    assert inspect.getsource(ingest).count("server.spam.check(ev)") >= 2, "the sync or backfill skips it"
    src = inspect.getsource(RelayServer._on_event)
    assert "_spam.check(ev)" in src and "is_puppet_event" in src


def test_a_client_publishing_here_is_told_rate_limited():
    srv = _server(posts_per_min=1)
    evs = [build_event(ALICE, 1, f"a new post number {i}", []) for i in range(2)]
    evs[1]["created_at"] = evs[0]["created_at"]
    evs[1] = build_event(ALICE, 1, "a new post number 1", [], created_at=evs[0]["created_at"])
    async def run():
        for e in evs:
            await srv._on_event("you", e)
    asyncio.new_event_loop().run_until_complete(run())
    oks = [m for _c, m in srv.sent if isinstance(m, list) and m[0] == "OK"]
    assert any(m[2] is False and m[3].startswith("rate-limited:") for m in oks), srv.sent
