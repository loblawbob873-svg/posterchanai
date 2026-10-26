"""The deploy gate's required stage splits into balanced groups on a machine that has never run it -- a CI runner.

Desktop CI was killed at 2,100 s twice on 2026-10-26 (e2928a73, 7d1872e1): with no local duration cache the groups
were balanced by FILE SIZE, which put ~1,050 s of work in one group and ~1,660 s in the other; on the slower runner
the big one overran while the small one finished in 1,279 s. scripts/test-durations.json (refreshed by
scripts/snapshot_test_durations.py) carries the measured times with the repo.
"""
import json
import os
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _gate(tmp_path):
    old = os.environ.get("XDG_CACHE_HOME")
    os.environ["XDG_CACHE_HOME"] = str(tmp_path)          # no local cache: what a fresh runner sees
    try:
        return runpy.run_path(str(ROOT / "scripts/deploy_regression_gate.py"))
    finally:
        if old is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = old


def test_the_snapshot_covers_the_required_list(tmp_path):
    g = _gate(tmp_path)
    snap = json.loads((ROOT / "scripts/test-durations.json").read_text())
    files = list(dict.fromkeys(t.split("::")[0] for t in g["TESTS"]))
    missing = [f for f in files if f not in snap]
    assert len(missing) <= len(files) // 10, (
        f"{len(missing)} of {len(files)} required files have no measured time -- CI will balance them by size. "
        f"Run scripts/snapshot_test_durations.py after a gate run. e.g. {missing[:5]}")


def test_a_runner_with_no_cache_gets_balanced_groups(tmp_path):
    g = _gate(tmp_path)
    durations = g["load_durations"]()
    assert durations, "no measured times at all on a fresh machine"
    snap = json.loads((ROOT / "scripts/test-durations.json").read_text())
    for jobs in (2, 3, 4):
        groups = g["required_shards"](list(g["TESTS"]), jobs, durations)
        cost = [sum(snap.get(f, 0) for f in dict.fromkeys(e.split("::")[0] for e in grp)) for grp in groups]
        assert max(cost) <= min(cost) * 1.15 + 30, (jobs, [round(c) for c in cost])
