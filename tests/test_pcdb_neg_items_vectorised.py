"""Negentropy's item list is built with numpy, and is EXACTLY what the per-event Python loop built.

2026-10-18: the relay's 70-130% CPU spikes were NEG-OPENs (py-spy caught `_neg_items_sync` and the exhaustive match
behind it): a Python slice + tuple + dict entry per event up to the 50k cap, then a Python sort. A sync answer that
differs by one id or one ordering makes negentropy reconcile the wrong set, silently -- so the new builder is held
to the old one's output over overlapping filters, equal timestamps and the cap.
"""
import asyncio
import random
import tempfile
import time

import numpy as np

from app.services.nostr_relay.pcdb_store import PcdbRelayStore, init_new
from tests.test_posterchandb import mk


def _reference(st, filters, cap):
    """The loop this replaced, verbatim in effect."""
    now = int(time.time())
    seen = {}
    for flt in filters:
        seqs = st._match(dict(flt), now)
        if not len(seqs):
            continue
        c = st.store.created.view(np.uint64)[seqs].astype(np.int64)
        if cap and len(seqs) > cap:
            keep = np.argsort(-c, kind="stable")[:cap]
            seqs, c = seqs[keep], c[keep]
        for s, ts in zip(seqs, c):
            raw = bytes(st.store.ids[int(s) * 32:int(s) * 32 + 32])
            seen[raw] = (int(ts), raw)
    return sorted(seen.values(), key=lambda x: (x[0], x[1]))


def test_the_vectorised_items_equal_the_loop_exactly():
    r = random.Random(7)
    with tempfile.TemporaryDirectory() as t:
        init_new(t + "/db")
        st = PcdbRelayStore(t + "/db", max_events=0, retention_days=0, maintenance=False)
        loop = asyncio.new_event_loop()
        st.open(loop)
        try:
            now = int(time.time())
            evs = [mk(kind=r.choice([1, 6, 7]), created_at=now - r.randrange(3000), content="n%d" % i)
                   for i in range(3000)]                   # many equal timestamps: the id breaks the tie
            loop.run_until_complete(st.add_events_bulk(evs, origin="wot"))
            for filters, cap in (([{"kinds": [1]}], 0), ([{"kinds": [1]}], 500),
                                 ([{"kinds": [1, 6]}, {"kinds": [6, 7], "since": now - 1000}], 0),
                                 ([{"kinds": [7]}, {"kinds": [1]}], 300), ([{"kinds": [9999]}], 0)):
                got = st._neg_items_sync(filters, cap)
                want = _reference(st, filters, cap)
                assert got == want, (filters, cap, len(got), len(want))
                assert all(isinstance(x[0], int) and isinstance(x[1], bytes) and len(x[1]) == 32 for x in got)
        finally:
            st.close()
