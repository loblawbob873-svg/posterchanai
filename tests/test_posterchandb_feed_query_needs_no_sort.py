"""A feed query over many authors answers exactly as before, without sorting its whole candidate list.

The relay's main CPU on 2026-10-16 (py-spy, 40% of the relay's samples): `_union` -> np.unique over every posting
list of a 4,109-account follow list, on every home-feed request -- 197 ms with kinds, 1.6 s without, measured on
the production copy. Per-author (and per-author-and-kind) lists are disjoint once the keys are unique, so when
they are the only selective part of a `query` they are concatenated. These pin the ANSWER: identical events, no
duplicate (a follow list naming one account twice), nothing missing, and `match` (COUNT/negentropy) still
ascending and unique.
"""
import os
import sys

import numpy as np

from app.services.posterchandb import store as store_mod
from app.services.posterchandb.store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _events(n):
    sys.path.insert(0, ROOT)
    from tests.test_posterchandb_disk_full import _events as ev
    return ev(7, n)


def _ids(evs):
    return [e["id"] for e in evs]


def test_feed_queries_answer_exactly_as_the_sorted_path(tmp_path, monkeypatch):
    st = Store(str(tmp_path / "s"), flush_interval=3600, snapshots=False)
    try:
        evs = _events(600)
        for e in evs:
            st.put(e)
        authors = list(dict.fromkeys(e["pubkey"] for e in evs))
        filters = [
            {"authors": authors, "kinds": [1, 6], "limit": 100},
            {"authors": authors, "limit": 200},
            {"authors": authors + authors[:5], "kinds": [1, 7, 7], "limit": 300},      # a key named twice
            {"authors": authors[:3], "limit": 5000},
        ]
        fast = [_ids(st.query(f)) for f in filters]
        # the old path: every union sorted and de-duplicated
        monkeypatch.setattr(store_mod, "_concat", store_mod._union)
        slow = [_ids(st.query(f)) for f in filters]
        for f, a, b in zip(filters, fast, slow):
            assert a == b, f
            assert len(a) == len(set(a)), ("a duplicate event in the answer", f)
        m = st.match({"authors": authors + authors[:5], "kinds": [1, 6]})
        assert len(m) and bool(np.all(m[1:] > m[:-1])), "match() must stay ascending and unique"
    finally:
        st.close(take_snapshot=False)
