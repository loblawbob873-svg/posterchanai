"""Server Stats, the NostrStats bot and the relay trace never open the relay's Postgres -- and a relay that cannot
be asked reads as UNKNOWN, never as zero (#161, retiring Postgres as the relay's store).

All three used to SELECT from the relay's `events`/`event_tags`/`wot`/`relay_kv` tables directly. They now ask the
relay process (nostr_relay/aggregates.py), which counts from whatever it serves from. Each test here makes every
Postgres door raise -- psycopg2.connect AND the app's SessionLocal -- so a path that still opens one fails.

The old Server Stats path had a second defect these pin: its per-figure `scalar()` answered 0 for a query that
failed, so a database that could not be read rendered as a node where nothing had ever happened.

Run: venv-unified/bin/python -m pytest tests/test_stats_no_relay_postgres.py -q
"""
import asyncio

import pytest

from app.services import relay_reader, relay_trace, stats_bot_service, stats_service
from app.services.nostr_relay import aggregates
from app.services.relay_reader import Unavailable
from tests.relay_agg_fixture import pk, sql_source
from tests.test_relay_aggregates import EVENTS, NOW


@pytest.fixture()
def no_postgres(monkeypatch):
    """Every way these paths ever reached a database now raises."""
    import psycopg2

    def refuse(*a, **k):
        raise AssertionError("opened a Postgres connection")
    monkeypatch.setattr(psycopg2, "connect", refuse)
    monkeypatch.setattr("app.database.SessionLocal", refuse)
    monkeypatch.setattr(stats_service, "_cache", {"at": 0.0, "data": None})
    aggregates._CACHE.clear()


def _relay_answers(monkeypatch, src):
    def fake_ask(what, args=None, **kw):
        if what == "server-stats":
            return aggregates.server_stats(src, NOW)
        if what == "nip05-activity":
            return aggregates.nip05_activity(src, int(args["since"]), {pk("puppet")})
        if what == "trace":
            return {"groups": [list(g) for g in src.author_groups(args["pubkey"])], "wot": [1, NOW],
                    "tier_row": [[2, 5], NOW, 3, 3]}
        if what == "wot-tiers":
            return {p: [1, 2] for p in args["pubkeys"][:1]}
        raise AssertionError(what)
    monkeypatch.setattr(aggregates, "ask", fake_ask)


def _relay_down(monkeypatch):
    def fake_ask(what, args=None, **kw):
        raise Unavailable("the relay is not running")
    monkeypatch.setattr(aggregates, "ask", fake_ask)


# ------------------------------------------------------------------ Server Stats
def test_server_stats_open_no_database(no_postgres, monkeypatch):
    _relay_answers(monkeypatch, sql_source(EVENTS))
    data = stats_service._compute()
    assert data["relay_unavailable"] is False
    t = data["totals"]
    assert t["notes"] == 13 and t["profiles"] == 4 and t["pubkeys_24h"] == 3 and t["ai_requests"] == 5
    assert t["events"] == sum(1 for _e, o in EVENTS if o == "direct")
    m = data["windows"]["minute"]
    assert m["totals"]["people"] == 3 and sum(m["series"]["monero_zaps"]) == 1
    assert data["games"]["total"] == 2 + 1 + 1          # chess g0 + g1, tictactoe, holdem
    assert sum(data["chat"]["series"]) == 4


def test_an_unreachable_relay_is_unknown_never_zero(no_postgres, monkeypatch):
    _relay_down(monkeypatch)
    data = stats_service._compute()
    assert data["relay_unavailable"] is True
    for k, v in data["totals"].items():
        assert v is None, "%s rendered as %r for a relay nobody could ask" % (k, v)
    for key, w in data["windows"].items():
        assert w["series"]["notes"] is None and w["series"]["monero_zaps"] is None
        assert w["totals"]["events"] is None and w["totals"]["people"] is None and w["totals"]["games"] is None
    assert data["games"]["total"] is None
    assert data["chat"]["series"] is None and data["chat"]["unknown"] is True
    assert data["relay"]["origins"] is None
    # local counters (image/music/…) do not depend on the relay and still answer
    assert "metrics" in data["counters"]


def test_an_unanswered_snapshot_is_not_served_for_a_whole_minute(no_postgres, monkeypatch):
    _relay_down(monkeypatch)
    first = asyncio.run(stats_service.get_stats())
    assert first["relay_unavailable"]
    _relay_answers(monkeypatch, sql_source(EVENTS))
    stats_service._cache["at"] -= 11                     # ten seconds later the page asks again
    again = asyncio.run(stats_service.get_stats())
    assert again["relay_unavailable"] is False and again["totals"]["notes"] == 13


# ------------------------------------------------------------------ the NostrStats bot
def test_the_stats_bot_opens_no_database(no_postgres, monkeypatch):
    _relay_answers(monkeypatch, sql_source(EVENTS))
    st = stats_bot_service._collect_stats()
    assert st["nip05_total"] == 2                         # alice and dave; the puppet is dropped
    assert st["posts_month"] >= 2


def test_the_stats_bot_never_posts_zeros_for_a_relay_it_could_not_ask(no_postgres, monkeypatch):
    _relay_down(monkeypatch)
    with pytest.raises(Unavailable):
        stats_bot_service._collect_stats()
    with pytest.raises(Unavailable):
        asyncio.run(stats_bot_service.build_stats())


# ------------------------------------------------------------------ the relay trace
def _upstream(monkeypatch, followers):
    async def fake_followers(p, relays):
        return list(followers)
    monkeypatch.setattr(relay_trace, "_followers", fake_followers)


def test_the_relay_trace_opens_no_database(no_postgres, monkeypatch):
    src = sql_source(EVENTS)
    _relay_answers(monkeypatch, src)
    bob = pk("bob")

    def fake_query(filters, **kw):
        f = filters[0]
        return [e for e, _o in EVENTS if e["pubkey"] in f["authors"] and e["kind"] in f["kinds"]][:f["limit"]]
    monkeypatch.setattr(relay_reader, "query", fake_query)
    _upstream(monkeypatch, [pk("alice"), pk("carol")])
    facts = asyncio.run(relay_trace.gather(bob, ["wss://x.example"]))
    assert facts["rows"]["profile"] and len(facts["rows"]["recent"]) == 5
    assert facts["rows"]["recent"][0][2] >= facts["rows"]["recent"][-1][2]       # newest first
    out = relay_trace.explain(facts)
    assert out["stored"]["total"] == sum(1 for e, _o in EVENTS if e["pubkey"] == bob)
    assert out["followers"]["in_wot"] == 1 and out["wot"]["tier"] == 2


def test_a_relay_trace_against_an_unreachable_relay_fails_rather_than_reporting_an_empty_account(
        no_postgres, monkeypatch):
    _relay_down(monkeypatch)
    with pytest.raises(Unavailable):
        asyncio.run(relay_trace.gather(pk("bob"), []))


def test_followers_whose_membership_could_not_be_asked_are_unknown_not_outsiders(no_postgres, monkeypatch):
    src = sql_source(EVENTS)
    _relay_answers(monkeypatch, src)
    real_ask = aggregates.ask

    def ask(what, args=None, **kw):
        if what == "wot-tiers":
            raise Unavailable("timed out")
        return real_ask(what, args, **kw)
    monkeypatch.setattr(aggregates, "ask", ask)
    monkeypatch.setattr(relay_reader, "query", lambda filters, **kw: [])
    _upstream(monkeypatch, [pk("alice"), pk("carol")])
    out = relay_trace.explain(asyncio.run(relay_trace.gather(pk("bob"), [])))
    assert out["followers"]["found"] == 2
    assert out["followers"]["in_wot"] is None
    assert all(f["in_wot"] is None for f in out["followers"]["shown"])
    assert any("Could not ask this relay" in s["text"] for s in out["signals"])

