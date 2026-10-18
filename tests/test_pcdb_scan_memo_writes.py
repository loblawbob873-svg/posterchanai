"""The relay's scan memo is PROGRESS, so it is written at most once an hour and at a clean stop -- never per pass.

2026-10-18: server1's `scan-memo.json` was 43.5 MB (every historic hit) and was rewritten WHOLE on every incremental
bridge/mirror scan, i.e. on every reload-blocks -- the largest single term in the relay's ~5 GB/day of SSD writes.
"""
import asyncio
import json
import os

from tests.test_pcdb_bridge_scan_cost import BLOCKED, _mk, _store


def _writes(ps):
    box = {"n": 0}
    real = ps._save_scan_memos

    def counted():
        box["n"] += 1
        real()
    ps._save_scan_memos = counted
    return box


def test_incremental_passes_write_the_memo_once_an_hour_and_at_close(tmp_path):
    ps, loop = _store(tmp_path)
    memo = os.path.join(ps.path, ps._SCAN_MEMO)
    try:
        box = _writes(ps)
        bridged = []
        for i in range(5):                              # five reload-blocks, each with something new to scan
            pk = "%064x" % (i + 1)
            e = _mk(pk, 1, tags=[["proxy", "https://mostr.pub/objects/%d" % i, "activitypub"]], n=i)
            ps._add_event_sync(e, "wot")
            bridged.append((pk, e))
            assert pk in loop.run_until_complete(ps.bridged_pubkeys(BLOCKED))
        assert box["n"] == 1, "the scan memo was rewritten %d times in five passes" % box["n"]

        # A hit whose event died is not carried into the file.
        ps._kill_seqs([ps.store.seq_of(bridged[0][1]["id"])])
    finally:
        ps.close()
        loop.close()
    assert box["n"] == 2, "close() did not save the progress the hourly limit held back"
    saved = json.load(open(memo))["bridged"]
    assert saved["hi"] == 5                               # all five passes' progress survived the stop
    assert bridged[0][0] not in saved["hits"].values()
    assert {pk for pk, _ in bridged[1:]} == set(saved["hits"].values())
