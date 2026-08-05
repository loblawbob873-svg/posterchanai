"""Bookmark sync between TWO browsers — the situation every bug in this feature was actually in.

This should have existed before the first line of sync code. It did not, and the cost was somebody
spending hours as the test harness: duplicate folders, duplicates that came back, a toggle that
cleared itself, bookmarks landing in the wrong container, a delete that boomeranged, and once an
entire Firefox tree deleted. Every one of those is invisible to a single-engine test with a
hand-written list of "remote" items, which is all this had.

`two_browser_sim.js` runs two independent copies of the engine — separate vm contexts, so separate
maps, items and listeners — against one shared relay, and drives the scenarios that broke:

  same-url-different-place  A link on Chrome's toolbar lives in Firefox's Bookmarks Menu. Identity
                            used to include the location, so neither matched the other and each
                            created the other's copy. Sync ids are DERIVED FROM THE URL now, so both
                            browsers compute the same id without coordinating and the relay keeps one.
  idempotent                Merging nine times must change nothing. Growth here is the duplication.
  one-folder                A folder full of bookmarks arrives as ONE folder, not one per bookmark.
  delete-propagates         Deleting with nothing listening must reach the other browser, not be
                            restored from the relay on the next merge.
  wholesale-asks            Losing everything at once asks first — a restore and a deliberate purge
                            are indistinguishable, and one of them must not delete everywhere.
"""
import json
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.join(HERE, "two_browser_sim.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@pytest.fixture(scope="module")
def results():
    r = subprocess.run(["node", SIM], capture_output=True, text=True, timeout=300)
    assert r.stdout.strip(), f"the simulation produced nothing:\n{r.stderr[-2000:]}"
    try:
        rows = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise AssertionError(f"simulation crashed:\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
    return {row["name"]: row for row in rows}


def _check(results, name):
    assert name in results, f"scenario missing from the simulation: {name!r} (have {list(results)})"
    row = results[name]
    assert row["ok"], f"{name}: {json.dumps(row['detail'])}"


def test_same_url_filed_differently_stays_one_bookmark(results):
    _check(results, "same url filed differently stays one bookmark each")


def test_an_add_propagates(results):
    _check(results, "an add propagates")


def test_merging_repeatedly_is_idempotent(results):
    _check(results, "merging repeatedly is idempotent")


def test_a_folder_arrives_once(results):
    _check(results, "a folder arrives once, not once per bookmark")


def test_a_delete_propagates(results):
    _check(results, "a delete propagates instead of coming back")


def test_a_settled_pair_goes_quiet(results):
    """Stable counts are not enough: two browsers can republish the same bookmarks at each other
    forever while the numbers never move."""
    _check(results, "a settled pair publishes nothing further")


def test_no_write_storm(results):
    """THE LOCK-UP, as a number. A browser fires its listeners for the extension's OWN writes, and it
    fires them as part of the write resolving — before the line that records "I am writing this id".
    So the engine republished its own creations as though the user had made them: every apply caused
    a publish, every publish caused an apply on the other browser, and the pair saturated the relay
    and the bookmark database until the browser had to be force-quit.

    Ten bookmarks means ten publishes. Measured at twenty before the fix, and it compounds with tree
    size and with every subscription round."""
    _check(results, "no write storm")


def test_wholesale_loss_asks_first(results):
    _check(results, "a wholesale disappearance asks before deleting everywhere")


def test_wholesale_loss_obeys_once_confirmed(results):
    _check(results, "…and obeys once confirmed")
