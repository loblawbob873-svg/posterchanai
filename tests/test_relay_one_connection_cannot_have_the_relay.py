"""One connection cannot have the relay: answers are paced per connection by a token bucket of frames.

2026-10-17: a desktop client pulled its whole private library right after a restart -- ~15 REQs a second of ~380
frames each for 12 minutes, 1.1 GB -- and took the relay past a full core while every other client waited.
Replays that flood against the shipped bucket, and an ordinary device's startup, which must never wait.
"""
from app.services.nostr_relay.server import RelayServer


class Conn:
    pass


def _replay(reqs_per_s, frames_each, seconds):
    srv = RelayServer.__new__(RelayServer)
    c, t, waited, sent = Conn(), 0.0, 0.0, 0
    step = 1.0 / reqs_per_s
    while t < seconds:
        w = srv._budget_wait(c, t)
        waited += w
        t += w                                   # the connection sits out its wait
        srv._budget_charge(c, frames_each)
        sent += frames_each
        t += step
    return sent / t, waited


def test_the_flood_that_took_the_relay_is_held_to_the_refill_rate():
    rate, waited = _replay(15, 380, 600)
    assert rate <= RelayServer.BUDGET_RATE * 1.15, rate        # was 5,700 frames/s from one connection
    assert waited > 0


def test_an_ordinary_device_starting_up_never_waits():
    srv = RelayServer.__new__(RelayServer)
    c, t, waited = Conn(), 0.0, 0.0
    # timeline 120, notifications 300, notes library 5,000, DM history 400 + 10 pages of 500, profiles 2,000
    for frames in [120, 300, 5000, 400] + [500] * 10 + [2000]:
        waited += srv._budget_wait(c, t)
        srv._budget_charge(c, frames)
        t += 0.2
    assert waited == 0, waited


def test_a_wait_is_never_long_enough_to_time_a_client_out():
    srv = RelayServer.__new__(RelayServer)
    c = Conn()
    srv._budget_wait(c, 0.0)
    srv._budget_charge(c, 10_000_000)
    assert srv._budget_wait(c, 0.0) == RelayServer.BUDGET_MAX_WAIT
