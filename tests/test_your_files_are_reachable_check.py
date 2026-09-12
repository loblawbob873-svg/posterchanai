"""The reachability check must stay ABLE TO FAIL, and stay in the suite.

`scripts/check_your_files_are_reachable.py` exists because three bugs shipped in one week where a
surface rendered perfectly and showed none of the user's files. Its whole value is that it can be
SHOWN to catch each of them — `--simulate <bug>` puts each one back, from the harness, by rewriting
the shipped source as it is served rather than on disk.

That makes it exactly the kind of check this repo has watched go quietly vacuous: every simulation
is anchored on a string in `app.js` / `os.js`, and the moment one of those anchors is renamed the
simulation stops reproducing anything. The check itself keeps passing, so nothing says so.

These tests do not open a browser. They ask the one question a browser cannot: does putting the bug
back still CHANGE the code that gets served? If it does not, the proof is gone and the check is a
green row that means nothing.
"""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def chk():
    return _load("check_your_files_are_reachable")


def test_it_is_registered_in_the_suite():
    """Unregistered checks run anyway, but they run with no timeout of their own and no reason
    written down next to them. This one is a release gate for data visibility; name it."""
    checkall = _load("checkall")
    assert "check_your_files_are_reachable" in checkall.CHECKS
    meta = checkall.CHECKS["check_your_files_are_reachable"]
    assert meta["group"] == "ui", "it serves static/ itself and needs no instance — it is a ui check"


def test_the_shipped_functions_can_still_be_lifted(chk):
    """A lift that stops matching must FAIL, never silently test a copy of the code."""
    lifted, is_enc = chk.lifted_app_js(None)
    assert "blossomPicker" in lifted
    assert "_renderFilesGrid" in lifted
    assert "musicEntries" in lifted
    assert "_fxSideHTML" in lifted
    # The encryption rule comes out of app.js, never restated here: bug 2 is that isEncFolder() is
    # RIGHT about Music and its callers were wrong, so a harness carrying its own copy could be
    # "fixed" by making the copy lie.
    assert "isEncFolder" in is_enc and "Music" in is_enc


@pytest.mark.parametrize("sim", ["bounded-folder-rows", "hide-encrypted"])
def test_each_app_js_simulation_still_changes_the_code(chk, sim):
    """Bugs 1 and 2. If the anchor is gone, `lifted_app_js` raises; if it is there but matched
    nothing, the two texts are identical and the simulation proves nothing."""
    clean, _ = chk.lifted_app_js(None)
    broken, _ = chk.lifted_app_js(sim)
    assert broken != clean, (
        "--simulate %s no longer rewrites anything, so the check can no longer be shown to catch "
        "that bug" % sim)


def test_the_wallpaper_simulation_still_changes_os_js(chk):
    """Bug 3. The picker must still have something to be stopped from doing."""
    shipped = open(chk.OS_JS, encoding="utf-8").read()
    assert chk.patched_os_js() != shipped
    assert chk.OS_NO_ENSURE_ANCHOR in shipped


def test_the_desktop_harness_is_reused_not_copied(chk):
    """The wallpaper picker needs a whole stubbed shell. It is borrowed from check_os_desktop.py so
    there is one of them — and the one substitution (a LAZY drive index, which is the entire bug) has
    to still land."""
    page = chk.desktop_page(["Backgrounds-0.jpg", "notes.txt"], None)
    assert "window.__idxEnsures" in page, "the lazy drive stub was not substituted in"
    assert page.count("window.__PC.filesIdx") == 1, (
        "check_os_desktop's eager drive stub is still in the page beside ours — with two, the "
        "later assignment wins and which one that is depends on where it sits in the file")
