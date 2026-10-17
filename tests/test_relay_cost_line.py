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
    assert S.RelayServer._req_origin(srv, outside, "repo1a2b") == "R"
