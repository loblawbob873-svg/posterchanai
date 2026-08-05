"""Real two-browser bookmark sync — two Brave instances, the built extension, a local relay, CDP.

This is the test that a single-engine mock could not be: the socket hands events to the sync engine
CONCURRENTLY and a local publish RACES the remote absorb of the same URL. Both DUPLICATED and crashed
the browser at scale, and both are invisible without two real browsers on a real relay. Every one of
those cost the user days precisely because the node sim (tests/client/two_browser_sim.js) awaits each
absorb and so never saw them.

Heavy and environment-dependent by nature: it needs node (21+, for global fetch + WebSocket), Brave,
and a built extension/dist/chrome. It SKIPS cleanly when any is missing, so it is safe in CI; run it
by hand on a machine that has Brave to guard the cross-browser behaviour. The relay is dependency-free
(raw-node WebSocket in relay.js) so nothing needs installing.
"""
import json
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVER = os.path.join(HERE, "two_browser_live.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@pytest.fixture(scope="module")
def outcome():
    r = subprocess.run(["node", DRIVER], capture_output=True, text=True, timeout=300)
    # The driver prints exactly one JSON line last: a skip object or a results array.
    line = next((l for l in reversed(r.stdout.splitlines()) if l.strip().startswith(("{", "["))), "")
    if not line:
        raise AssertionError(f"driver produced no JSON:\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
    data = json.loads(line)
    if isinstance(data, dict) and "skip" in data:
        pytest.skip(f"real-browser test skipped: {data['skip']}")
    return {row["name"]: row for row in data}


def _check(outcome, name):
    assert name in outcome, f"scenario missing: {name!r} (have {list(outcome)})"
    row = outcome[name]
    assert row["ok"], f"{name}: {json.dumps(row.get('detail'))}"


def test_an_add_reaches_the_other_browser(outcome):
    _check(outcome, "an add on A reaches B")


def test_same_url_on_both_is_not_duplicated(outcome):
    """The concurrent local-publish vs remote-absorb race — the 'Poster-Chan' duplicate."""
    _check(outcome, "same url on both -> one each (no concurrent-race dup)")


def test_a_settled_pair_goes_quiet(outcome):
    _check(outcome, "a settled pair goes quiet (no publish storm)")


def test_delete_propagates_and_stays_dead(outcome):
    _check(outcome, "delete on A removes on both and stays dead")


def test_deleting_a_duplicate_keeps_the_survivor(outcome):
    """Deleting one of two duplicates must keep the surviving copy synced, not strand it on one side."""
    _check(outcome, "deleting a duplicate keeps the survivor on both")


def test_no_service_worker_errors(outcome):
    _check(outcome, "no service-worker errors")
