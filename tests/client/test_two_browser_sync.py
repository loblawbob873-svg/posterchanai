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


def test_a_large_tree_is_not_quadratic(results):
    """WHY THE BROWSER HUNG. getTree() serialises the WHOLE bookmark tree across the extension
    boundary, and the engine called it for every arriving bookmark — twice, via the folder lookup.
    Measured at 603 full-tree reads for 300 bookmarks, against 4 now.

    Ten-node trees are why no test saw it: the cost is invisible until the tree is real, and then it
    is the browser locking up while it syncs. The roots are fixed for the life of a profile, so they
    are read once."""
    _check(results, "a large tree does not read the whole tree per bookmark")


def test_restoring_a_backup_is_not_quadratic(results):
    """The same full-tree read, driven by the USER's own bulk action instead of the far side's. The
    browser fires one event per bookmark and publishing one needs its path, so restoring a backup
    serialised the whole tree 300 times on the UI thread — 302 reads measured, against 3 batched."""
    _check(results, "restoring a backup does not read the whole tree per bookmark")


def test_overlapping_merges_do_not_pile_up(results):
    """Concurrent merges are the NORMAL case, not an edge one: EOSE fires once per relay, again on
    every reconnect, and again from the periodic connect check. Overlapping merges each read the whole
    tree and each plan against a map the others are still mutating."""
    _check(results, "overlapping merges do not pile up or duplicate")


def test_the_off_switch_is_real(results):
    """A user who does not want this must not be able to be slowed down by it: with the toggle off the
    engine makes NO bookmark API calls at all, however much the user edits."""
    _check(results, "with sync off the engine never touches the bookmark api")


def test_reconnecting_does_not_reapply_everything(results):
    """A relay re-sends everything it holds on every connection, with the timestamps it already had.
    The engine dropped those on a strict newer-than, so each re-delivered event was decrypted and
    re-applied for a bookmark that had not changed — the whole library, per relay, every reconnect."""
    _check(results, "reconnecting does not re-apply the whole library")


def test_wholesale_loss_asks_first(results):
    _check(results, "a wholesale disappearance asks before deleting everywhere")


def test_wholesale_loss_obeys_once_confirmed(results):
    _check(results, "…and obeys once confirmed")


def test_deleting_a_folder_syncs_live(results):
    """The bug a real user hit that no single-engine test could see. A browser fires ONE onRemoved for
    a deleted folder — never one per bookmark inside it — so an engine that only tombstones the ids it
    is handed leaves every bookmark in that folder alive on the relay and on the other browser. It
    "synced" only if the user then pressed Merge, which is not what deleting means. The engine now
    reconciles the tree against its map on any removal and tombstones what vanished, live.

    This also depends on the sim's SHARED CLOCK: the old relay counter (1000+) against the engine's
    real-Date.now() `_at` (~1.7e9) made a publisher reject every tombstone as "older", masking this as
    a pass."""
    _check(results, "deleting a folder syncs the bookmarks inside it, live")


def test_folder_delete_leaves_tombstones_for_offline_devices(results):
    """The sweep must TOMBSTONE the orphaned children, not merely forget them locally, or a device that
    was offline during the delete never learns of it."""
    _check(results, "a folder delete leaves tombstones, so an offline device also drops them")


def test_a_move_does_not_duplicate(results):
    """Dragging a bookmark to another folder is a location edit, not a new bookmark: no duplicate, and
    it lands in the new folder on the other browser."""
    _check(results, "a move does not duplicate and lands in the new folder")


def test_editing_a_url_syncs_new_and_drops_old(results):
    """The sync id is derived from the URL, so editing a URL is an add plus a delete — the old must go
    and the new must arrive, with no orphan on either browser."""
    _check(results, "editing a url syncs the new and drops the old")


def test_confirming_bulk_delete_stays_deleted_two_browsers(results):
    """"108 missing… comes back no matter how many times I click Merge." The confirm path (union's
    removal loop) tombstoned only the one mapped id per URL, not the duplicate siblings — so a leftover
    duplicate resurrected the bookmark and the second browser kept it alive, forever. Confirming a bulk
    delete must kill EVERY event for each URL."""
    _check(results, "confirming a bulk delete stays deleted with duplicate events and a second browser")


def test_deleting_stays_deleted_with_stale_duplicate_events(results):
    """"Brave brings back everything I delete." A relay polluted by older versions holds several live
    events per URL (random ids). Three bugs conspired: absorbing each duplicate created another copy
    (the duplicates + the lock-up); deleting tombstoned only the mapped id so a stale one recreated it;
    and `forget` cleared the reverse mapping the merge had just handed the winning id, so a later delete
    found no id to tombstone at all. A delete must kill EVERY event for the URL and stay dead."""
    _check(results, "deleting stays deleted even with stale duplicate events on the relay")
