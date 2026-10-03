"""The relay's spam guard: spam, not volume, on every route into the Social timeline.

"i want to avoid spammers hammering the social timeline of fedi and nostr" / "those users did nothing
wrong, i want to prevent actual spam". Measured over two weeks here: the busiest real accounts posted 41
timeline posts in a minute and 92 in an hour; every author repeating one post more than 3 times an hour
was spam (a donation scam x13, zap-begging x10); the most accounts behind one identical text was 5.
"""
from unittest import mock

from app.services.nostr_relay import server as srvmod
from app.services.nostr_relay.server import RelayServer
from app.services.nostr_relay.spamguard import SpamGuard


class Gate:
    puppets = set()
    def is_member(self, _pk): return True
    def is_operator(self, _pk): return False
    def is_puppet_event(self, ev): return ev.get("pubkey") in self.puppets
    def is_blocked(self, _pk): return False


def _server(**cfg):
    srv = RelayServer(object(), Gate(), {"wot_enabled": False, **cfg})
    srv._conn_ips["you"] = "8.8.4.4"
    srv._conn_ips["lan"] = "192.168.0.85"
    return srv


def _ev(kind=1, content=None, pk="a1", ts=1_000_000):
    return {"kind": kind, "pubkey": pk * 32, "created_at": ts, "id": "x", "tags": [],
            "content": content if content is not None else f"an ordinary post at {ts}"}


def _arrivals(srv, conn, evs, step=1.0):
    out = []
    with mock.patch.object(srvmod.time, "time") as t:
        for i, e in enumerate(evs):
            t.return_value = 1000.0 + i * step
            out.append(srv._rate_limited(conn, e, e["kind"]))
    return out


def test_a_busy_real_poster_is_never_touched():
    """92 timeline posts an hour, 41 in one minute: the busiest real accounts here."""
    g = SpamGuard({})
    hour = [_ev(ts=1_000_000 + i * 39) for i in range(92)]
    burst = [_ev(pk="b2", ts=2_000_000 + i) for i in range(41)]
    assert all(g.check_timestamps(e) == "" for e in hour + burst)
    srv = _server()
    assert set(_arrivals(srv, "you", [_ev(ts=i) for i in range(41)], step=1.4)) == {""}


def test_one_author_pasting_the_same_post_is_stopped():
    g = SpamGuard({})
    scam = "Peace be upon you, my brothers and sisters, may God bless you all. Please help my family."
    res = [g.check_timestamps(_ev(content=scam, ts=1_000_000 + i * 60)) for i in range(13)]
    assert res[:3] == ["", "", ""] and all(r.startswith("rate-limited:") for r in res[3:]), res[:5]
    assert g.check_timestamps(_ev(content="gm", ts=1_000_100)) == "", "a short greeting is not spam"


def test_a_bot_farm_posting_one_text_is_stopped_after_five_accounts():
    g = SpamGuard({})
    text = "Claim your free airdrop now at totally-legit-site dot example, limited time only!!!"
    res = [g.check_timestamps(_ev(content=text, pk=f"{i:02x}", ts=1_000_000 + i)) for i in range(20)]
    assert res[:5] == [""] * 5 and all(r.startswith("rate-limited:") for r in res[5:]), res[:7]


def test_a_machine_gun_flood_is_stopped():
    g = SpamGuard({})
    res = [g.check_timestamps(_ev(ts=1_000_000, content=f"flood post number {i} with its own words")) for i in range(70)]  # 70 in one minute
    assert res[:60].count("") == 60 and res[60].startswith("rate-limited:")


def test_only_timeline_posts_count():
    g = SpamGuard({"dup_per_hour": 1, "rate_per_min": 1})
    for kind in (7, 4, 1059, 30078, 3, 0, 10000):
        assert all(g.check_timestamps(_ev(kind=kind, content="same text same text same text")) == "" for _ in range(10))


def test_fediverse_posts_are_judged_even_though_our_bridge_is_on_the_lan():
    srv = _server()
    srv.gate.puppets = {"c3" * 32}
    scam = "Buy followers now, cheapest prices, DM me for details and discounts!!!"
    res = _arrivals(srv, "lan", [_ev(content=scam, pk="c3", ts=1_000_000 + i) for i in range(5)])
    assert res[:3] == ["", "", ""] and res[3].startswith("rate-limited:"), res
    # Our own app on the LAN (not a puppet) is never limited.
    assert set(_arrivals(srv, "lan", [_ev(content=scam, pk="d4") for _ in range(10)])) == {""}


def test_direct_writes_are_counted_by_arrival_so_backdating_does_not_help():
    srv = _server(rate_per_min=5)
    evs = [_ev(ts=1_000_000 - i * 3600) for i in range(6)]    # stamped an hour apart, all sent now
    res = _arrivals(srv, "you", evs, step=0.0)
    assert res[:5] == [""] * 5 and res[5].startswith("rate-limited:"), res


def test_zero_turns_every_check_off():
    g = SpamGuard({"rate_per_min": 0, "rate_per_hour": 0, "dup_per_hour": 0, "dup_authors_per_hour": 0})
    text = "x" * 60
    assert all(g.check_timestamps(_ev(content=text, pk=f"{i % 50:02x}")) == "" for i in range(500))


def test_the_firehose_and_the_sync_consult_the_guard():
    import inspect
    from app.services.nostr_relay import ingest, thread
    assert "_spam.check_timestamps(ev)" in inspect.getsource(thread)
    src = inspect.getsource(ingest)
    assert src.count("server.spam.check_timestamps(ev)") >= 2, "the sync or the backfill skips the guard"
