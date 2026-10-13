"""The relay counts its own store (#161): Server Stats, the NostrStats bot and the relay trace ask the relay
process (nostr_relay/aggregates.py), which counts from PosterChanDB -- here a real PosterChanDB store, over one
set of events whose every number is stated below.

Run: venv-unified/bin/python -m pytest tests/test_relay_aggregates.py -q
"""
import asyncio
import json
import os
import threading
import time

import pytest

from app.services.nostr_relay import aggregates as A
from app.services.relay_reader import Unavailable
from tests.relay_agg_fixture import ev, pcdb_source, pk

NOW = int(time.time()) - 5
H, D = 3600, 86400


def dataset():
    out = []
    for who in ("alice", "bob", "carol"):
        out += [ev(who, 1, NOW - 30), ev(who, 1, NOW - 5 * H), ev(who, 1, NOW - 10 * D), ev(who, 7, NOW - 90)]
    for i in range(20):                                   # every DM is signed by a one-time key
        out.append(ev("wrap%02d" % i, 1059, NOW - 60 - i))
    for i in range(3):                                    # zap receipts, signed by the LNURL service
        out.append(ev("lnurl", 9735, NOW - 120 - i))
    # Monero tips: a repeated hashtag counts once; another coin, a wrong kind, and synced copies do not count.
    out += [ev("alice", 1, NOW - 60, tags=[("t", "monerotip"), ("t", "monerotip"), ("amount_xmr", "0.1")]),
            ev("bob", 1, NOW - 2 * H, tags=[("t", "monerotip")]),
            ev("bob", 1, NOW - 3 * D, tags=[("t", "monerotip")]),
            ev("carol", 1, NOW - 70, tags=[("t", "monero")]),
            ev("carol", 7, NOW - 80, tags=[("t", "monerotip")]),
            ev("stranger", 1, NOW - 50, tags=[("t", "monerotip")], origin="wot")]
    for i in range(10):
        out.append(ev("wot%d" % i, 1, NOW - 100 - i, origin="wot"))
    for i in range(3):
        out.append(ev("anc%d" % i, 1, NOW - 40 * D, origin="ancestor"))
    # Games: one distinct d-tag is one game, whoever wrote it and however often.
    out += [ev("alice", 30078, NOW - 100, tags=[("d", "pcai:chesstr:g1")]),
            ev("bob", 30078, NOW - 200, tags=[("d", "pcai:chesstr:g1")]),
            ev("alice", 30078, NOW - 40 * D, tags=[("d", "pcai:chesstr:g0")]),
            ev("carol", 30078, NOW - 2 * H, tags=[("d", "pcai:ttt:t1")], origin="wot"),
            ev("bob", 30078, NOW - 20 * H, tags=[("d", "pcai:holdem:h1")])]
    # AI chat transcript turns (counted by d-tag, never read).
    for i, age in enumerate((H, 2 * H, 2 * D, 10 * D, 40 * D)):
        out.append(ev("alice", 30078, NOW - age, tags=[("d", "pcai:msg:conv:%d" % i)], content="sealed"))
    # Profiles: who holds a NIP-05.
    out += [ev("alice", 0, NOW - 50 * D, content=json.dumps({"name": "a", "nip05": "alice@x.example"})),
            ev("bob", 0, NOW - 50 * D, content=json.dumps({"name": "b", "nip05": "  "})),
            ev("carol", 0, NOW - 50 * D, content=json.dumps({"name": "c"})),
            ev("dave", 0, NOW - 50 * D, content=json.dumps({"nip05": "dave@x.example"}), origin="wot"),
            ev("puppet", 0, NOW - 50 * D, content=json.dumps({"nip05": "pup@x.example"}), origin="wot"),
            ev("eve", 0, NOW - 50 * D, content="{not json nip05")]
    out += [ev("dave", 1, NOW - 1 * D - 100, origin="wot"), ev("dave", 1, NOW - 1 * D - 200, origin="wot"),
            ev("puppet", 1, NOW - 1 * D - 300, origin="wot"), ev("dave", 1, NOW - 200 * D, origin="wot")]
    out.append(ev("carol", 30311, NOW - 3 * H, tags=[("d", "s1")]))
    return out


EVENTS = dataset()


@pytest.fixture
def src(tmp_path):
    return pcdb_source(EVENTS, tmp_path / "pcdb", NOW)


def _strip(d):
    d = dict(d)
    d.pop("backend", None)
    d.pop("db_bytes", None)
    return d


# ------------------------------------------------------------------ what the numbers mean (every backend)
def test_server_stats_mean_what_the_page_says(src):
    s = A.server_stats(src, NOW)
    minute = s["windows"]["minute"]
    # people active in the last hour: alice, bob, carol -- the 20 gift-wrap keys and the zap service are not people
    assert minute["people"] == 3
    # every event published here in the hour counts, DMs and zaps included
    direct_hour = [e for e, o in EVENTS if o == "direct" and NOW - 3600 <= e["created_at"] < NOW]
    assert minute["events"] == len(direct_hour)
    assert sum(n for _b, n in minute["monero"]) == 1                  # alice's (tag repeated: once)
    assert sum(n for _b, n in s["windows"]["hour"]["monero"]) == 2
    assert sum(n for _b, n in s["windows"]["day"]["monero"]) == 3
    assert minute["by_game"]["chess"] == 1 and s["windows"]["day"]["by_game"]["tictactoe"] == 1
    assert s["games"]["chess"] == 2                                   # g0 is older than every window
    assert s["origins"]["wot"]["total"] == sum(1 for _e, o in EVENTS if o == "wot")
    assert s["origins"]["ancestor"] == {"total": 3, "day": 0}
    assert s["pubkeys_24h"] == 3
    assert s["ai_requests"] == 5 and s["ai_requests_24h"] == 2
    assert sum(n for _d, n in s["chat_daily"]) == 4                    # the 40-day-old turn is outside 30 days
    assert s["notes"] == 3 * 3 + 3 + 1                                 # plain notes + three tip notes + carol's
    assert s["profiles"] == 4 and s["streams"] == 1                    # alice, bob, carol, eve published here


def test_nip05_activity_counts_holders_per_day_and_drops_puppets(src):
    out = A.nip05_activity(src, NOW - 30 * D, exclude={pk("puppet")})
    assert set(out["nip05"]) == {pk("alice"), pk("dave")}
    rows = {(d, out["nip05"][i]): n for d, i, n in out["activity"]}
    day = (NOW - D - 100) // D * D
    assert sum(n for (d, p), n in rows.items() if p == pk("dave")) == 2       # the 200-day-old note is out
    assert rows.get((day, pk("dave"))) in (1, 2)
    assert all(p != pk("puppet") for (_d, p) in rows)
    # alice: NOW-30 s, NOW-5 h, NOW-10 d and the tip note at NOW-60 s -- every one within 30 days
    assert sum(n for (_d, p), n in rows.items() if p == pk("alice")) == 4


def test_trace_groups_per_kind_and_origin(src):
    rows = {(k, o): (n, lo, hi) for k, o, n, lo, hi in src.author_groups(pk("bob"))}
    assert rows[(1, "direct")][0] == 3 + 2
    assert rows[(0, "direct")][0] == 1
    assert src.author_groups(pk("nobody")) == []


def test_a_part_that_cannot_be_counted_is_unknown_not_zero(tmp_path):
    class Broken(A._Pcdb):
        def people_since(self, since):
            raise RuntimeError("disk on fire")
    s = A.server_stats(Broken(pcdb_source(EVENTS, tmp_path / "pcdb", NOW).st, NOW), NOW)
    assert s["pubkeys_24h"] is None and s["pubkeys_30d"] is None
    assert s["notes"] == 13                                           # the rest still answers


# ------------------------------------------------------------------ PosterChanDB: never hold the store lock long
def test_column_copies_are_taken_in_pieces_with_the_lock_released_between(tmp_path):
    p = pcdb_source(EVENTS, tmp_path / "p", NOW)
    st = p.st
    full = st.stats_columns()
    acquired = []

    class Counting:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            acquired.append(1)
            return self.inner.__enter__()

        def __exit__(self, *a):
            return self.inner.__exit__(*a)
    real = st._lock
    st._lock = Counting(real)
    try:
        part = st.stats_columns(chunk=7)
    finally:
        st._lock = real
    assert len(acquired) >= full["n"] // 7
    for k in ("created", "kind", "origin", "author", "dead", "expires"):
        assert (part[k] == full[k]).all()


# ------------------------------------------------------------------ the IPC: ask / answer through the control dir
class _FakeStore:
    """The relay's store as aggregates sees it: a PosterChanDB `store` and the puppet registry."""

    def __init__(self, path):
        self.store = pcdb_source(EVENTS, path, NOW).st

    def _bridge_puppets_all_sync(self):
        return {pk("puppet")}


def _relay_thread(ctrl, store, stop):
    while not stop.is_set():
        for req in A.take_requests(ctrl):
            A.answer_one(store, ctrl, req)
        time.sleep(0.02)


def test_ask_round_trip_through_the_control_dir(tmp_path):
    A._CACHE.clear()
    ctrl = str(tmp_path / "ctrl")
    os.makedirs(ctrl)
    store = _FakeStore(tmp_path / "pcdb")
    stop = threading.Event()
    t = threading.Thread(target=_relay_thread, args=(ctrl, store, stop), daemon=True)
    t.start()
    try:
        got = A.ask("nip05-activity", {"since": NOW - 30 * D}, control_dir=ctrl, check_running=False, timeout=10)
        assert set(got["nip05"]) == {pk("alice"), pk("dave")}           # the relay dropped its own puppets
        with pytest.raises(Unavailable):
            A.ask("no-such-thing", {}, control_dir=ctrl, check_running=False, timeout=10)
    finally:
        stop.set()
        t.join(2)
    assert not os.listdir(os.path.join(ctrl, "answers"))               # every answer was collected


def test_a_relay_that_does_not_answer_is_unavailable_and_the_request_is_withdrawn(tmp_path):
    ctrl = str(tmp_path / "ctrl")
    with pytest.raises(Unavailable):
        A.ask("server-stats", {}, control_dir=ctrl, check_running=False, timeout=0.3)
    assert not [f for f in os.listdir(ctrl) if f.startswith("ask_")]


def test_a_stopped_relay_is_unavailable_without_waiting(monkeypatch, tmp_path):
    monkeypatch.setattr("app.services.nostr_relay.thread.relay_status", lambda: {"running": False})
    t = time.monotonic()
    with pytest.raises(Unavailable):
        A.ask("server-stats", {}, control_dir=str(tmp_path), timeout=30)
    assert time.monotonic() - t < 1


def test_requests_the_asker_gave_up_on_are_not_computed(tmp_path):
    ctrl = str(tmp_path)
    with open(os.path.join(ctrl, "ask_old.json"), "w") as f:
        json.dump({"what": "server-stats", "rid": "a" * 32, "ts": time.time() - 3600}, f)
    assert A.take_requests(ctrl) == []
    assert not os.listdir(ctrl) or os.listdir(ctrl) == ["answers"] or os.listdir(ctrl) == []


def test_a_bad_request_id_cannot_name_a_path(tmp_path):
    ctrl = str(tmp_path / "ctrl")
    A.answer_one(_FakeStore(tmp_path / "pcdb"), ctrl, {"what": "server-stats", "rid": "../../etc/x"})
    assert not os.path.exists(os.path.join(ctrl, "answers"))


def test_the_relay_process_runs_the_ask_poller_and_it_answers(tmp_path):
    """The shipped coroutine, on its own loop, answering a real ask -- and the relay's main loop starts it."""
    import inspect
    from app.services.nostr_relay import thread as relay_thread
    assert "ask_poller(store" in inspect.getsource(relay_thread._main)
    A._CACHE.clear()
    ctrl = str(tmp_path / "ctrl")
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()
    t = threading.Thread(target=lambda: loop.run_until_complete(A.ask_poller(_FakeStore(tmp_path / "pcdb"), ctrl, stop, interval=0.05)),
                         daemon=True)
    t.start()
    try:
        got = A.ask("server-stats", {}, control_dir=ctrl, check_running=False, timeout=15)
        assert got["backend"] == "posterchandb" and got["windows"]["minute"]["people"] == 3
    finally:
        loop.call_soon_threadsafe(stop.set)
        t.join(5)
