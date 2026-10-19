"""A background scan never holds the store lock longer than its time budget, however slow each record is to read.

server1 2026-10-19: `max_lock_hold_ms` 267, `max_lock_hold_site` _decode -- 128 records per hold, each from a cold
segment (a disk read under the lock), and every query and write waited behind a background scan. Simulated here with a
slow `get` / `_seq_of`; every record must still be delivered, once, in order.
"""
import asyncio
import tempfile
import time

from app.services.nostr_relay import pcdb_store as P
from app.services.nostr_relay.pcdb_store import PcdbRelayStore, init_new
from tests.test_posterchandb import mk


def _store(tmp):
    init_new(tmp + "/db")
    st = PcdbRelayStore(tmp + "/db", max_events=0, retention_days=0, maintenance=False)
    loop = asyncio.new_event_loop()
    st.open(loop)
    evs = [mk(kind=1, created_at=1_700_000_000 + i, content="n%d" % i) for i in range(300)]
    loop.run_until_complete(st.add_events_bulk(evs, origin="wot"))
    return st, evs


def test_decode_holds_are_bounded_by_time_and_lose_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        st, evs = _store(tmp)
        try:
            real = st.store.get
            st.store.get = lambda s: (time.sleep(0.005), real(s))[1]          # a cold page per record
            st.lock_stats = {"max_hold_ms": 0.0, "max_hold_site": ""}
            seqs = sorted(st.store.seq_of(e["id"]) for e in evs)
            seen = []
            st._decode(seqs, lambda s, ev, o: seen.append(s), pace=False)
            assert seen == seqs, "a record was skipped or repeated"
            assert st.lock_stats["max_hold_ms"] < 60, "a scan held the store lock %.0f ms" % st.lock_stats["max_hold_ms"]
        finally:
            st.close()


def test_id_lookups_are_bounded_by_time_and_lose_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        st, evs = _store(tmp)
        try:
            real = st.store._seq_of
            st.store._seq_of = lambda eid: (time.sleep(0.003), real(eid))[1]
            st.lock_stats = {"max_hold_ms": 0.0, "max_hold_site": ""}
            got = st._live_seqs_of([e["id"] for e in evs] + ["nope"])
            assert got[:-1] == [real(e["id"]) for e in evs] and got[-1] is None
            assert st.lock_stats["max_hold_ms"] < 60, "id lookups held the store lock %.0f ms" % st.lock_stats["max_hold_ms"]
        finally:
            st.close()
