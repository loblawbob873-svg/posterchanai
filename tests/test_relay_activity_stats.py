"""Server Stats → 🛰️ Relay: the numbers behind the relay-activity panel.

The panel's whole value is that it distinguishes states that look identical from the outside, so
each of these tests pins down one such pair:

  * a queue that is EMPTY vs one whose relay is still running an older build and reports nothing —
    a confident "0 queued" over a relay that isn't reporting is exactly the false-green this
    codebase keeps re-learning about;
  * an event DROPPED on overflow (never left the queue) vs one GIVEN UP on after its retries (left
    the queue and was refused) — the second means "this was not broadcast", the first means "we
    never tried";
  * a firehose stream that is disconnected vs one that was removed from the upstream list, which
    must disappear rather than sit there reading red forever.
"""
import asyncio

import pytest

from app.services.nostr_relay import firehose as fh
from app.services.nostr_relay.outbox import Outbox
from app.services.nostr_relay.server import RelayServer


# --- outbound queue ----------------------------------------------------------------------------

def test_stats_reports_depth_and_capacity_without_touching_the_queue():
    ob = Outbox(["wss://a.example", "wss://b.example"], maxsize=4)
    for i in range(4):
        ob.enqueue({"id": f"{i:064x}"})
    st = ob.stats()
    assert (st["queued"], st["max"]) == (4, 4)
    assert st["relays"] == 2
    assert st["sent"] == 0 and st["dropped"] == 0
    assert ob._q.qsize() == 4          # reading stats must not consume anything


def test_overflow_counts_as_dropped_not_as_sent():
    """A drop never leaves the queue, so it can never be a send. Conflating the two would report a
    blasting client as healthy traffic."""
    ob = Outbox(["wss://a.example"], maxsize=2)
    for i in range(5):
        ob.enqueue({"id": f"{i:064x}"})
    st = ob.stats()
    assert st["queued"] == 2
    assert st["dropped"] == 3
    assert st["sent"] == 0


def test_a_drain_counts_sent_and_full_acceptance_separately(monkeypatch):
    """`sent` is what left the queue; `full` is what EVERY target accepted first time. A relay that
    is quietly refusing half the fleet has a healthy `sent` and a sinking `full` — which is the
    signal, so the two must not be one counter."""
    accepted = {"set": {"wss://a.example"}}

    async def fake_publish_to(upstream, ev, direct=False):
        return set(accepted["set"])

    monkeypatch.setattr("app.services.nostr.relay.publish_to", fake_publish_to)

    async def go():
        ob = Outbox(["wss://a.example", "wss://b.example"], min_interval=0, retries=0)
        ob.enqueue({"id": "1" * 64})
        ob.start()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if ob.stats()["sent"]:
                break
        ob.stop()
        return ob.stats()

    st = asyncio.run(go())
    assert st["sent"] == 1
    assert st["full"] == 0             # b.example missed it
    assert st["last_at"] > 0

    accepted["set"] = {"wss://a.example", "wss://b.example"}
    st2 = asyncio.run(go())
    assert (st2["sent"], st2["full"]) == (1, 1)


def test_stats_never_names_a_relay():
    """This feeds a PUBLIC page. Counts are fine; the private mirror's targets are the operator's
    own machines and must not be enumerable from it."""
    ob = Outbox(["wss://secret-mirror.internal"], label="private-mirror")
    assert "secret-mirror" not in repr(ob.stats())
    assert ob.stats()["label"] == "private-mirror"
    assert ob.stats()["relays"] == 1


# --- firehose stream state ---------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_firehose_status():
    fh._STATUS.clear()
    yield
    fh._STATUS.clear()


def test_a_stream_reports_connected_then_disconnected():
    fh._mark("wss://a.example", " (WoT)", connected=True)
    fh._mark("wss://a.example", " (WoT)", event=True)
    fh._mark("wss://a.example", " (WoT)", event=True)
    row = fh.firehose_status()[0]
    assert row["relay"] == "wss://a.example"
    assert row["label"] == "(WoT)"
    assert row["connected"] is True and row["events"] == 2
    assert row["since"] > 0 and row["last"] > 0

    fh._mark("wss://a.example", " (WoT)", connected=False)
    row = fh.firehose_status()[0]
    assert row["connected"] is False
    assert row["events"] == 2          # the count survives a reconnect; it is since-start, not since-connect


def test_the_same_relay_on_two_subscriptions_stays_two_rows():
    """The WoT stream and the DM-inbox stream hit the same URL with different filters. Collapsing
    them would hide one of them being down."""
    fh._mark("wss://a.example", " (WoT)", connected=True)
    fh._mark("wss://a.example", " (DM inbox)", connected=False)
    assert len(fh.firehose_status()) == 2


def test_a_removed_upstream_relay_stops_being_reported():
    """After an upstream change the old rows would otherwise sit there disconnected forever, and the
    panel would report streams this relay no longer has."""
    fh._mark("wss://old.example", " (WoT)", connected=True)
    fh._mark("wss://keep.example", " (WoT)", connected=True)
    fh._mark("wss://old.example", " (DM inbox)", connected=True)
    fh._prune_status(" (WoT)", ["wss://keep.example"])
    rows = {(r["relay"], r["label"]) for r in fh.firehose_status()}
    assert ("wss://old.example", "(WoT)") not in rows
    assert ("wss://keep.example", "(WoT)") in rows
    assert ("wss://old.example", "(DM inbox)") in rows   # a different group's rows are not its business


# --- write-path tallies ------------------------------------------------------------------------

def test_ok_frames_are_tallied_by_verdict_even_for_a_vanished_connection():
    """The verdict is a fact about the write. A client that hung up before its OK could be enqueued
    still had its event accepted or refused, and the tally has to say so."""
    srv = RelayServer.__new__(RelayServer)
    srv._outq = {}
    srv._conn_ips = {}
    srv._send("gone", ["OK", "a" * 64, True, ""])
    srv._send("gone", ["OK", "b" * 64, False, "blocked: not in web of trust"])
    srv._send("gone", ["OK", "c" * 64, False, "invalid: bad id or signature"])
    srv._send("gone", ["EVENT", "s1", {"id": "d" * 64}])   # not a verdict
    assert (srv._accepted, srv._rejected) == (1, 2)


def test_tallies_are_per_relay_not_shared_by_the_class():
    a, b = RelayServer.__new__(RelayServer), RelayServer.__new__(RelayServer)
    a._outq = b._outq = {}
    a._send("gone", ["OK", "a" * 64, True, ""])
    assert a._accepted == 1
    assert b._accepted == 0


# --- the payload the page renders ---------------------------------------------------------------

def test_missing_keys_stay_missing_instead_of_becoming_zero(monkeypatch):
    """An older relay subprocess reports no queue/subscription keys at all. Rendering that as 0 is
    the false-green: it reads as "nothing queued, nothing rejected" when the truth is "not measured"."""
    from app.services import stats_service
    monkeypatch.setattr(stats_service, "_TTL", 0)
    monkeypatch.setattr("app.services.nostr_relay.thread.relay_status",
                        lambda: {"running": True, "members": 7, "conns": 3, "online": 2})
    r = stats_service._relay(1_700_000_000, {})
    assert r["running"] is True and r["members"] == 7
    assert r["subs"] is None and r["accepted"] is None and r["rejected"] is None
    assert r["outbox"] is None and r["private_outbox"] is None
    assert r["uptime"] is None
    assert r["firehose"] == [] and r["firehose_up"] == 0


def test_a_relay_that_is_down_says_so_rather_than_raising(monkeypatch):
    from app.services import stats_service

    def boom():
        raise RuntimeError("no status file")

    monkeypatch.setattr("app.services.nostr_relay.thread.relay_status", boom)
    r = stats_service._relay(1_700_000_000, {})
    assert r["running"] is False


def test_uptime_and_stream_health_are_derived_from_the_status_file(monkeypatch):
    from app.services import stats_service
    now = 1_700_000_000
    monkeypatch.setattr("app.services.nostr_relay.thread.relay_status", lambda: {
        "running": True, "members": 1, "conns": 1, "online": 1, "subs": 12,
        "accepted": 40, "rejected": 5, "started": now - 3600, "ts": now - 10,
        "outbox": {"label": "outbox", "queued": 2, "max": 500},
        "firehose": [{"relay": "wss://a", "label": "(WoT)", "connected": True, "events": 9, "since": now - 60},
                     {"relay": "wss://b", "label": "(WoT)", "connected": False, "events": 0, "since": 0}],
    })
    r = stats_service._relay(now, {"direct": {"total": 10, "day": 2}})
    assert r["uptime"] == 3600
    assert r["stale"] == 10
    assert r["subs"] == 12 and r["accepted"] == 40 and r["rejected"] == 5
    assert r["firehose_up"] == 1 and len(r["firehose"]) == 2
    assert r["origins"]["direct"]["day"] == 2
