"""Live fan-out meets only the subscriptions whose kinds can match, and sends EXACTLY what the full scan sent.

py-spy on server1 2026-10-19: fan-out ran the whole matcher for every open subscription (411) on every accepted event,
~6% of the relay. A kind index now narrows the candidates; the matcher still decides. Held to a brute-force run of the
original loop over random subscriptions/events, including REQ id reuse, CLOSE and disconnects.
"""
import random

from app.services.nostr_relay import server as S


def _brute(subs, ev, allow):
    out = []
    for conn, ss in subs._subs.items():
        if not allow(conn, ev):
            continue
        for sid, filters in ss.items():
            if S._stale_for_feeds(ev) and not S._targeted(filters):
                continue
            if S._matches(filters, ev, {}):
                out.append((conn, sid))
    return sorted(out)


def _random_filter(r):
    f = {}
    if r.random() < 0.8:
        f["kinds"] = r.sample([0, 1, 3, 6, 7, 1059, 30078, 9735], r.randint(1, 3))
    if r.random() < 0.3:
        f["authors"] = ["%064x" % r.randrange(6) for _ in range(r.randint(1, 3))]
    if r.random() < 0.2:
        f["#p"] = ["%064x" % r.randrange(6)]
    return f


def test_the_kind_index_sends_exactly_what_the_full_scan_sent():
    r = random.Random(11)
    subs = S.SubscriptionManager()
    conns = ["c%d" % i for i in range(30)]
    allow = lambda conn, ev: conn != "c7" or ev["kind"] != 30078      # one connection may not see app docs
    import time
    now = int(time.time())
    for step in range(3000):
        op = r.random()
        conn = r.choice(conns)
        if op < 0.25:
            subs.add(conn, "s%d" % r.randrange(5), [_random_filter(r) for _ in range(r.randint(1, 2))])
        elif op < 0.30:
            subs.remove(conn, "s%d" % r.randrange(5))
        elif op < 0.32:
            subs.remove_conn(conn)
        else:
            ev = {"id": "%064x" % step, "pubkey": "%064x" % r.randrange(6), "kind": r.choice([0, 1, 3, 6, 7, 1059, 30078, 9735]),
                  "created_at": now - r.choice([0, 0, 0, 7200]), "content": "x",
                  "tags": [["p", "%064x" % r.randrange(6)]] if r.random() < 0.5 else []}
            sent = []
            subs.fanout(ev, lambda c, msg: sent.append((c, msg[1])), allow)
            assert sorted(sent) == _brute(subs, ev, allow), (step, ev["kind"])


def test_an_event_meets_only_the_subscriptions_that_can_want_its_kind(monkeypatch):
    subs = S.SubscriptionManager()
    for i in range(300):
        subs.add("c%d" % i, "dm", [{"kinds": [1059], "#p": ["%064x" % i]}])
    subs.add("feed", "f", [{"kinds": [1, 6]}])
    calls = []
    real = S._matches
    monkeypatch.setattr(S, "_matches", lambda f, ev, t=None: calls.append(1) or real(f, ev, t))
    import time
    subs.fanout({"id": "a" * 64, "pubkey": "b" * 64, "kind": 1, "created_at": int(time.time()), "tags": [],
                 "content": "hi"}, lambda c, m: None, lambda c, e: True)
    assert len(calls) == 1, "a kind-1 post was matched against %d subscriptions" % len(calls)
