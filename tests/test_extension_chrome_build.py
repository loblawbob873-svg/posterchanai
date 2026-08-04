"""The Chrome build of the password extension.

Chrome would not load this extension at all, and exactly one manifest key was why: Firefox MV3 takes
`"background": {"scripts": [...]}` and runs them as an event page; Chrome MV3 takes one
`service_worker` and REFUSES an extension that lists `scripts`. Everything else was already portable —
every file aliases `browser ?? chrome`, and no background script touches the DOM, localStorage or
XMLHttpRequest, none of which exist in a worker.

So Chrome gets a generated manifest over the SAME sources (never a second checked-in one — two
manifests drift, and the one that drifts is the one nobody loads daily) plus a one-line worker that
importScripts the same files.

THE DRIFT THIS GUARDS: that import list and the Firefox manifest's background.scripts are two
spellings of one thing. If they diverge, Chrome silently runs a different set of scripts than Firefox
— which is not a crash, it is one browser quietly missing a fix the other has.

Verified by loading dist/chrome in real Chrome: it installs, and chrome-extension://<id>/popup.html
resolves, which only happens for an extension Chrome accepted. Full API behaviour needs a headed
browser this box does not have (same limit as Electron).
"""
import json
import os
import re

EXT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extension")


def _read(name):
    with open(os.path.join(EXT, name), encoding="utf-8") as fh:
        return fh.read()


def test_the_worker_loads_exactly_what_firefox_loads():
    manifest = json.loads(_read("manifest.json"))
    firefox = manifest["background"]["scripts"]
    # findall, not search: the header comment mentions importScripts() with empty parens, and matching
    # that reports an empty list — a failure about nothing, which is worse than no test.
    calls = [c for c in re.findall(r"importScripts\(([^)]*)\)", _read("background-chrome.js")) if c.strip()]
    assert calls, "background-chrome.js no longer importScripts anything"
    chrome = [s.strip().strip("'\"") for s in calls[-1].split(",") if s.strip()]
    assert chrome == firefox, (
        f"Chrome loads {chrome} and Firefox loads {firefox} — one browser is running a different set "
        "of background scripts than the other, silently")


def test_the_chrome_manifest_is_generated_not_committed():
    assert not os.path.exists(os.path.join(EXT, "manifest.chrome.json")), \
        "a second checked-in manifest is a drift waiting to happen; build.sh generates it"
    build = _read("build.sh")
    assert "'service_worker': 'background-chrome.js'" in build, \
        "build.sh no longer rewrites background for Chrome — Chrome will refuse to load the result"
    assert "browser_specific_settings" in build and "pop(" in build, \
        "the Firefox-only key should be dropped from the Chrome manifest"


def test_the_chrome_entry_point_is_shipped():
    """It is not in FILES (that list is the Firefox bundle), so the Chrome staging step copies it
    explicitly. Miss that and Chrome loads an extension whose service worker 404s."""
    build = _read("build.sh")
    assert "cp background-chrome.js dist/chrome/" in build


def test_the_background_scripts_stay_worker_safe():
    """A service worker has no DOM, no localStorage and no XMLHttpRequest. Firefox's event page has
    all three, so a change that uses one breaks ONLY Chrome, and only at runtime."""
    for name in ("background.js", "vaultcore.js"):
        src = _read(name)
        for banned in ("document.", "localStorage", "XMLHttpRequest", "window."):
            assert banned not in src, (
                f"{name} uses {banned}, which does not exist in a service worker — the Chrome build "
                "would break at runtime while Firefox stayed fine")


def test_nip07_goes_into_the_page_world_directly_on_chrome():
    """Firefox has to smuggle inject.js into the page as an inline <script> built by content.js,
    because a `src` to the extension would publish a per-install UUID to every page — a supercookie.
    Chrome can register a content script with `world: MAIN`, which needs no inline script (nothing for
    a site's CSP to refuse), injects no node, and leaks only "this extension exists", identically for
    every install.

    Reported as "on Brave, says no NIP-07 extension", and CONFIRMED by the fix: with the inline
    <script> path Brave found no signer on any site, and with this one it does. (It could not be
    reproduced on this box — headless Chrome never activates extensions here, so no content-script
    world is ever created — which is why it shipped as the Chrome-native path on its merits first.)
    """
    build = _read("build.sh")
    assert "'world': 'MAIN'" in build, "inject.js is not registered in the page's world for Chrome"
    inject = _read("inject.js")
    assert "chrome.runtime.id" in inject and "__pcNostrProvider()" in inject, (
        "inject.js must self-invoke when it finds itself in a world without chrome.runtime.id — in "
        "the MAIN world nothing else will call it")


def test_bookmark_sync_ships_in_both_bundles():
    """A background file listed for one browser and not the other is the drift the test above exists
    for; this is the same check from the other end, for the file most recently added."""
    manifest = json.loads(_read("manifest.json"))
    assert "bookmarks.js" in manifest["background"]["scripts"]
    assert "bookmarks" in manifest["permissions"], "the engine cannot read the tree without it"
    assert "bookmarks.js" in _read("build.sh"), "bookmarks.js is not in the shipped file list"
