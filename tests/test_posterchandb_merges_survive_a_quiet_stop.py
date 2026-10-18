"""Housekeeping's run merges are kept across a clean stop even when nothing new was stored.

2026-10-18, scripts/pcdb_scale_check.py: a 30M-event store's index runs were merged (71 merges, 13 minutes, the
store grew from 11 to 29 GB while they were written) and then closed -- and the manifest still named every pre-merge
run. `close()` checkpointed only when EVENTS had arrived since the last checkpoint, so merge work on a quiet store
was thrown away at every stop and redone at every start, and its files lay unreferenced on disk until some later
checkpoint swept them. An idle node -- a PosterChanOS desktop's local relay above all -- paid that on every restart.
"""
import os
import re

from app.services.posterchandb.store import Store
from tests.pcdb_gen import Gen

RUN = re.compile(r"^run-(.+)\.\d+\.bin$")


def _disk_runs(st):
    return sum(len(lv.disk_names()) for lv in st._leveled())


def _files(path):
    return {m.group(1) for m in (RUN.match(f) for f in os.listdir(path)) if m}


def test_merges_on_a_quiet_store_survive_a_clean_stop(tmp_path):
    path = str(tmp_path)
    st = Store(path, flush_interval=3600, direct_durable=False)
    gen = Gen(7)
    for _ in range(6):                               # six checkpoints: six sealed runs per index
        for _ in range(300):
            st.put(gen.event(), origin="wot")
        st.snapshot()
    ids = {e["id"] for e in st.query({"kinds": [1], "limit": 5000})}
    st.close()

    st = Store(path, flush_interval=3600, direct_durable=False)
    before = _disk_runs(st)
    merged = 0
    while True:
        k = st.merge_disk_runs(max_steps=64)
        if not k:
            break
        merged += k
    assert merged, "nothing to merge -- the test did not build several runs"
    after = _disk_runs(st)
    assert after < before
    st.close()                                       # NOTHING new was stored since it opened

    st = Store(path, flush_interval=3600, direct_durable=False)
    try:
        assert _disk_runs(st) == after, "the merges were lost at the stop: %d runs again, not %d" % (
            _disk_runs(st), after)
        live = {n for lv in st._leveled() for n in lv.disk_names()}
        assert _files(path) == live, "run files nothing refers to: %s" % sorted(_files(path) - live)
        assert {e["id"] for e in st.query({"kinds": [1], "limit": 5000})} == ids
    finally:
        st.close()
