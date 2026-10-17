"""A query for a RARE kind reads that kind's index instead of walking the whole store -- same answer.

py-spy on server1 (2026-10-16) caught {"kinds":[54],"since":...} polled by a client: the newest-first walk never
reached `limit` matches for a rare kind, so it scanned every event per request. The walk also stops at `since` now.
"""
import os
import sys

from app.services.posterchandb import store as store_mod
from app.services.posterchandb.store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _events(n):
    sys.path.insert(0, ROOT)
    from tests.test_posterchandb_disk_full import _events as ev
    return ev(11, n)


def test_rare_kind_answers_match_the_walk(tmp_path, monkeypatch):
    st = Store(str(tmp_path / "s"), flush_interval=3600, snapshots=False)
    try:
        evs = _events(800)
        for e in evs:
            st.put(e)
        rare = 7 if sum(e["kind"] == 7 for e in evs) < sum(e["kind"] == 1 for e in evs) else 6
        oldest = min(e["created_at"] for e in evs)
        filters = [{"kinds": [rare]}, {"kinds": [rare], "since": oldest + 50}, {"kinds": [rare], "limit": 3},
                   {"kinds": [rare, 1], "limit": 20}, {"kinds": [1], "since": oldest + 100, "limit": 500}]
        fast = [[e["id"] for e in st.query(f)] for f in filters]
        monkeypatch.setattr(store_mod, "_RARE_KIND_POSTINGS", -1)       # force the walk
        walked = [[e["id"] for e in st.query(f)] for f in filters]
        assert fast == walked
        assert all(len(a) == len(set(a)) for a in fast)
    finally:
        st.close(take_snapshot=False)
