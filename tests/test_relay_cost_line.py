"""The relay logs what its sends go to, every 5 minutes: frames/s out, split into live delivery and answers.

Most of the relay's CPU is building, compressing and writing frames (py-spy, server1 2026-10-16), and nothing
measured how many or why -- each optimisation that day was found by sampling stacks instead.
"""
import logging
from types import SimpleNamespace

from app.services.nostr_relay import server as S


def test_fanout_counts_the_frames_it_sends():
    subs = S.SubscriptionManager()
    a, b = object(), object()
    subs.add(a, "s1", [{"kinds": [1]}])
    subs.add(a, "s2", [{"kinds": [1, 7]}])
    subs.add(b, "s3", [{"kinds": [7]}])
    sent = []
    subs.fanout({"id": "1" * 64, "pubkey": "2" * 64, "kind": 1, "created_at": 1, "tags": [], "content": ""},
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
