"""The relay logs what its sends go to, every 5 minutes: frames/s out, split into live delivery and answers.

Most of the relay's CPU is building, compressing and writing frames (py-spy, server1 2026-10-16), and nothing
measured how many or why -- each optimisation that day was found by sampling stacks instead.
"""
import logging
import time
from types import SimpleNamespace

from app.services.nostr_relay import server as S


def test_fanout_counts_the_frames_it_sends():
    subs = S.SubscriptionManager()
    a, b = object(), object()
    subs.add(a, "s1", [{"kinds": [1]}])
    subs.add(a, "s2", [{"kinds": [1, 7]}])
    subs.add(b, "s3", [{"kinds": [7]}])
    sent = []
    subs.fanout({"id": "1" * 64, "pubkey": "2" * 64, "kind": 1, "created_at": int(time.time()), "tags": [], "content": ""},
                lambda c, m: sent.append(m), lambda c, e: True)
    assert len(sent) == 2 and subs.fanout_frames == 2


def test_the_cost_line_splits_live_delivery_from_answers(caplog):
    subs = SimpleNamespace(fanout_frames=0, total=lambda: 7)
    srv = SimpleNamespace(_frames_out=0, _accepted=0, _cost_at=0.0, _cost_base=(0, 0, 0), _outq={1: 1, 2: 2}, subs=subs)
    S.RelayServer._cost_line(srv, 1000.0)                    # the first call only sets the baseline
    srv._frames_out, subs.fanout_frames, srv._accepted = 3000, 1200, 300
    with caplog.at_level(logging.INFO):
        S.RelayServer._cost_line(srv, 1300.0)
    line = [r.getMessage() for r in caplog.records if "cost:" in r.getMessage()][0]
    assert "10 frames/s out (live delivery 4/s, answers+control 6/s), 1.0 events/s accepted, 2 conns, 7 subs" in line, line


def test_the_cost_line_names_the_queries_the_answers_go_to(caplog):
    """490 of 508 frames/s on server1 after 2026-10-17's fixes were ANSWERS -- the line now says to which REQ
    shapes (kinds, fields, author-list size; never whose), costliest first, and starts over each window."""
    srv = S.RelayServer.__new__(S.RelayServer)
    srv._frames_out, srv._accepted, srv._cost_at, srv._cost_base, srv._outq = 0, 0, 0.0, (0, 0, 0), {}
    srv.subs = SimpleNamespace(fanout_frames=0, total=lambda: 0)
    for _ in range(3):
        srv._note_req_cost([{"kinds": [30078], "authors": ["a" * 64], "#d": ["x"], "limit": 256}], 200)
    srv._note_req_cost([{"kinds": [1, 6], "since": 5, "limit": 50}], 40)
    srv._note_req_cost([{"ids": ["b" * 64]}], 1)
    S.RelayServer._cost_line(srv, 1000.0)
    srv._frames_out = 700
    with caplog.at_level(logging.INFO):
        S.RelayServer._cost_line(srv, 1300.0)
    line = [r.getMessage() for r in caplog.records if "top answers" in r.getMessage()][0]
    assert line.index("[30078|a1|#d] x3 -> 600 frames") < line.index("[1,6||s] x1 -> 40 frames"), line
    assert "a" * 64 not in line, "a pubkey reached the log"
    assert srv._req_cost == {}, "the next window must start from zero"


def test_a_loopback_connection_is_this_nodes_own_even_when_its_source_was_rewritten():
    """server1's NAT rewrites the source of loopback connections to its LAN address: the relay's own end (127.0.0.1)
    is what says the asker is a process on this machine. Read by the peer, every one of them was tagged remote."""
    srv = S.RelayServer.__new__(S.RelayServer)
    class Conn:
        def __init__(self, here):
            self.local_address = (here, 3052)
    natted, outside = Conn("127.0.0.1"), Conn("192.168.0.2")
    srv._conn_ips = {natted: "192.168.0.2", outside: "192.168.0.1"}
    assert S.RelayServer._req_origin(srv, natted, "repo1a2b") == "L:repo"
    assert S.RelayServer._req_origin(srv, outside, "repo1a2b") == "R:repo"


def test_a_library_read_by_label_names_the_app():
    shape = S.RelayServer._req_shape([{"kinds": [30078], "authors": ["a" * 64], "#l": ["pcai-notes"], "limit": 5000}])
    assert shape == "[30078|a1|#l=pcai-notes]", shape
    assert "aaaa" not in S.RelayServer._req_shape([{"kinds": [1], "authors": ["a" * 64]}]), "an author key was printed"


def test_one_connection_asking_hundreds_of_times_a_minute_is_named_in_the_log(caplog, monkeypatch):
    """2026-10-17: one desktop client sent ~15 REQs/s for 12 minutes (1.1 GB) and nothing named it."""
    srv = S.RelayServer.__new__(S.RelayServer)

    class Conn:
        _pcai_ip = "192.168.0.102"
    c, t = Conn(), [1000.0]
    monkeypatch.setattr(S.time, "time", lambda: t[0])
    flt = [{"kinds": [30078], "authors": ["a" * 64], "until": 5}]
    for _ in range(400):
        srv._note_heavy(c, "R:sub-notes-", flt, 380)
    t[0] += 61
    with caplog.at_level(logging.WARNING):
        srv._note_heavy(c, "R:sub-notes-", flt, 380)
    msg = [r.getMessage() for r in caplog.records if "heavy client" in r.getMessage()]
    assert msg and "400 REQs" in msg[0] and "192.168.0.102" in msg[0] and "R:sub-notes-" in msg[0] \
        and "[30078|a1|u]" in msg[0], msg
    caplog.clear()
    for _ in range(10):
        srv._note_heavy(c, "R:sub-notes-", flt, 380)
    t[0] += 61
    with caplog.at_level(logging.WARNING):
        srv._note_heavy(c, "R:sub-notes-", flt, 380)
    assert not [r for r in caplog.records if "heavy client" in r.getMessage()], "an ordinary client was reported"


def test_the_cost_line_names_where_live_delivery_goes(caplog):
    """1,900 live frames/s on production (2026-10-17) and nothing could say to which subscriptions."""
    subs = S.SubscriptionManager()
    subs.add("feed", "global", [{"kinds": [1]}], label="R:sub-timeli [1||]")
    subs.add("me", "n", [{"kinds": [1], "#p": ["b" * 64]}], label="R:sub-notifs [1||#p]")
    for i in range(3):
        subs.fanout({"id": "%064x" % i, "pubkey": "c" * 64, "kind": 1, "created_at": int(time.time()),
                     "tags": [], "content": ""}, lambda c, m: None, lambda c, e: True)
    srv = S.RelayServer.__new__(S.RelayServer)
    srv._frames_out, srv._accepted, srv._cost_at, srv._cost_base, srv._outq = 0, 0, 0.0, (0, 0, 0), {}
    srv.subs = subs
    S.RelayServer._cost_line(srv, 1000.0)
    srv._frames_out = 3
    with caplog.at_level(logging.INFO):
        S.RelayServer._cost_line(srv, 1300.0)
    line = [r.getMessage() for r in caplog.records if "live delivery --" in r.getMessage()]
    assert line and "R:sub-timeli [1||] -> 3 frames" in line[0], line
    assert "notifs" not in line[0], "an event that does not match a subscription was counted for it"
