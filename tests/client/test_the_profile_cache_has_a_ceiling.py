"""THE PROFILE STORE HAD NO CEILING AND NOTHING ANYWHERE DELETED FROM IT.

Reported from Windows as "posterchan ran out of memory", at start, while the SAME bundle ran fine on
a PosterChanOS laptop that had read less.

`Store.saveProfile` writes an IndexedDB record for every author whose kind-0 arrives -- which, on a
client that reads the global feed, is every author on the network the socket has ever named.
`_pruneIDB` trims `events` and has never touched `profiles`, so that store was append-only for the
life of an install, and `init()` read ALL of it with `getAll()` before the app painted a pixel. It
is the one allocation in this file that grows with USE rather than with the user's own data, which
is exactly the shape of "this was working perfectly fine before".

And it got worse every time: the prune is scheduled 8 seconds AFTER the hydrate, so a hydrate that
dies is a store that is never trimmed.

A profile is the one thing here that is safe to evict, and the reasoning is `_isPinned` inverted:
every pin on that list is a document only its author can decrypt, where dropping it means it is gone
until a relay hands it back. A kind-0 is public, on every relay, and this client refetches one the
moment `haveProfile` says no.

The shipped store.js runs here against a FAKE INDEXEDDB, because the bug lives entirely in the
IndexedDB paths and `tests/test_client_store_pinning.py` runs with `indexedDB: undefined` -- blind to
this by construction. The fake counts its calls: the hydrate's METHOD is part of the fix, not only
its result, since a `getAll()` that is then trimmed pays the whole allocation first and passes every
assertion about what it kept.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tests/client/profile_store_sim.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run(plan):
    out = subprocess.run(["node", str(SIM), json.dumps(plan)],
                         cwd=ROOT, text=True, capture_output=True, timeout=120)
    assert out.returncode == 0, out.stderr[:3000]
    return json.loads(out.stdout)


def test_a_huge_store_is_trimmed_instead_of_growing_for_ever():
    r = _run({"seedCount": 30000})
    assert len(r["disk"]) == 8000, f"the prune left {len(r['disk'])} profiles on disk"
    assert r["hydrated"] == 8000, f"boot loaded {r['hydrated']} profiles into memory"


def test_boot_never_materialises_the_whole_profile_store():
    """The method matters, not only the result. `getAll()` on a store holding hundreds of thousands
    of records IS the allocation being fixed; trimming the copy afterwards fixes nothing."""
    r = _run({"seedCount": 30000})
    assert "profiles" not in r["calls"]["getAll"], \
        "boot still reads the whole profile store at once: " + str(r["calls"]["getAll"])
    assert "profiles" in r["calls"]["openCursor"]


def test_it_keeps_the_ones_this_device_saw_most_recently():
    r = _run({"seedCount": 30000})
    assert "p29999" in r["held"], "the newest profile was evicted"
    assert "p0" not in r["held"], "the oldest profile survived a full prune"


def test_a_profile_from_before_this_existed_ranks_below_anything_seen_since():
    """An old install's whole store carries no `at`. Those are precisely the records nothing has
    confirmed are still worth holding, so they must lose to everything that has."""
    r = _run({"seedCount": 12000, "legacy": 6000})
    kept = set(r["held"])
    assert not any(("p%d" % i) in kept for i in range(0, 3000)), \
        "legacy records outranked profiles this device has actually seen"
    assert "p11999" in kept


def test_your_own_profile_is_never_evicted_however_old_it_is():
    """Evicting it turns the header into 'anon' and hands a profile edit a half-loaded record to
    publish over. It is the one profile that is not merely a cache."""
    r = _run({"seedCount": 30000, "viewer": "p0"})
    assert "p0" in r["held"], "the signed-in user's own profile was evicted"
    assert "p0" in r["disk"], "the signed-in user's own profile was deleted from disk"


def test_seeing_a_profile_again_keeps_it():
    """For anybody you already have, every kind-0 that arrives lands in the not-newer branch, which
    used to return without recording that it had. Ranked on the create path alone, a contact whose
    profile was cached a year ago and received a thousand times since is the FIRST thing evicted."""
    r = _run({"seedCount": 30000, "save": [{"pubkey": "p0", "created_at": 1000}]})
    assert "p0" in r["held"], "a profile seen again was still treated as the oldest thing on disk"
    assert r["diskRec"]["p0"] and r["diskRec"]["p0"] > 100000, \
        "the re-sighting never reached disk, so the next boot forgets it: " + str(r["diskRec"]["p0"])


def test_a_small_store_is_left_completely_alone():
    """The prune must not be a thing that happens on every boot to everybody."""
    r = _run({"seedCount": 50})
    assert len(r["disk"]) == 50 and r["hydrated"] == 50


def test_the_prune_protects_the_account_that_signed_in_after_boot():
    """The realistic order. `init()` runs before sign-in, so the hydrate genuinely does not know
    whose profile is whose -- which is survivable, because a profile missing from memory is a
    refetch. The prune runs 8 seconds later, by which time it does know, and what it does is DELETE:
    that is the step where getting it wrong is not a refetch."""
    r = _run({"seedCount": 30000, "viewerAfterBoot": "p0"})
    assert "p0" in r["disk"], "the signed-in user's own profile was deleted from disk"


def test_the_prune_is_not_fired_from_boot_itself():
    """It is scheduled behind the hydrate on purpose: a boot that also pays for a full prune is a
    boot that is slower on exactly the install that is already struggling."""
    r = _run({"seedCount": 50})
    assert r["scheduled"] == 1
