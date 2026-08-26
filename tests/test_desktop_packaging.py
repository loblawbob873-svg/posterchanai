"""What electron-builder actually PACKS (desktop/package.json → build.files).

This exists because the desktop app shipped broken to every user on all three platforms, and nothing
anywhere noticed. `desktop/origin.js` was split out of main.js to make it testable, main.js requires
it on line 57 — and `build.files` was a hand-written list of six filenames that nobody added it to.
Electron then throws before the first window:

    A JavaScript error occurred in the main process
    Uncaught Exception: Error: Cannot find module './origin'

Every other check in this repo looks past this. `tests/test_desktop_*.py` load the modules straight
from the source tree, where the file obviously exists. `scripts/check_desktop_standalone.py` drives
the WEB BUNDLE in headless Chrome, because Electron needs an X display this box does not have. So the
one artifact users install was the one thing never inspected — and the failure is total: not a broken
feature, an app that will not open.

The list is patterns now (`*.js`, `*.html`), which cannot forget a new module. This asserts that
stays true: every local file the app requires or references must be matched by one of them.
"""
import fnmatch
import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESKTOP = os.path.join(ROOT, "desktop")

with open(os.path.join(DESKTOP, "package.json"), encoding="utf-8") as fh:
    PKG = json.load(fh)
with open(os.path.join(DESKTOP, "package-lock.json"), encoding="utf-8") as fh:
    LOCK = json.load(fh)
PATTERNS = PKG["build"]["files"]

REQUIRE = re.compile(r"""require\(\s*['"](\./[^'"]+)['"]\s*\)""")
HTML_REF = re.compile(r"""(?:src|href)=["']([^"':#?]+)["']""")


def _packed(rel):
    """Would electron-builder include this path? Its `files` are globs relative to the app dir."""
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel, p.replace("/**/*", "/*"))
               or rel.startswith(p.split("**")[0].rstrip("/") + "/") for p in PATTERNS)


def _local_requires(rel, seen):
    """Every ./module main.js pulls in, transitively — a missing one two hops down is the same crash."""
    if rel in seen:
        return
    seen.add(rel)
    path = os.path.join(DESKTOP, rel)
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    for m in REQUIRE.finditer(src):
        target = m.group(1)[2:]
        if not os.path.splitext(target)[1]:
            target += ".js"
        yield rel, target
        yield from _local_requires(target, seen)


def test_every_module_the_app_requires_is_packed():
    entry = PKG.get("main", "main.js")
    # preload.js is named in main.js as a webPreferences path, not required, so seed it explicitly.
    found = list(_local_requires(entry, set())) + list(_local_requires("preload.js", set()))
    assert found, "no local requires discovered — this test would pass vacuously"
    for importer, target in found:
        assert os.path.isfile(os.path.join(DESKTOP, target)), \
            f"{importer} requires ./{target}, which does not exist"
        assert _packed(target), (
            f"{importer} requires ./{target} and build.files {PATTERNS} does not pack it — the "
            "installer would throw 'Cannot find module' before the first window opens")


def test_the_pages_the_shell_opens_are_packed():
    """boot.html is what the window sits on while Tor bootstraps, so its absence is a blank app at
    exactly the moment the user is told to wait."""
    pages = [f for f in os.listdir(DESKTOP) if f.endswith(".html")]
    assert pages, "no HTML pages found in desktop/"
    for page in pages:
        assert _packed(page), f"{page} is not packed by {PATTERNS}"
        with open(os.path.join(DESKTOP, page), encoding="utf-8") as fh:
            html = fh.read()
        for ref in HTML_REF.findall(html):
            if ref.startswith(("http:", "https:", "data:", "//", "app:")):
                continue
            assert os.path.isfile(os.path.join(DESKTOP, ref)), f"{page} references {ref}, missing"
            assert _packed(ref), f"{page} references {ref} and build.files does not pack it"


def test_files_are_patterns_not_a_handwritten_list():
    """The specific regression. A list of filenames is correct exactly until someone adds a file, and
    what makes it dangerous is that the build still SUCCEEDS — electron-builder has no idea main.js
    needed the thing it just left out, and CI publishes an installer that cannot start."""
    js = [p for p in PATTERNS if p.endswith(".js")]
    assert "*.js" in js, (
        f"build.files enumerates JS files ({js}) instead of globbing them — that is how origin.js was "
        "left out of every installer")


@pytest.mark.parametrize("mod", ["origin.js", "tor.js", "preload.js", "main.js"])
def test_the_modules_that_exist_today(mod):
    assert _packed(mod), f"{mod} would not be packed"


def test_no_comment_keys_in_the_build_config():
    """JSON has no comments and electron-builder does not tolerate the usual workaround: its schema
    REJECTS unknown properties, so a `"//files": "why…"` key next to `files` fails validation and the
    build produces no installer at all. Written here because I did exactly that while fixing the bug
    above — the explanation belongs in desktop/README.md and in this file, not in the config."""
    bad = [k for k in PKG["build"] if k.startswith("//")]
    assert not bad, (
        f"comment keys in build config: {bad} — electron-builder validates its schema strictly and "
        "will refuse to build")


def test_desktop_dependencies_do_not_regress_below_security_floors():
    """These floors close published Electron, builder and YAML vulnerabilities. Keep the assertion
    on the resolved lockfile: a permissive package.json range is not what CI actually ships."""
    def version(path):
        return tuple(int(x) for x in LOCK["packages"][path]["version"].split("-")[0].split("."))

    assert version("node_modules/electron") >= (44, 0, 0)
    assert version("node_modules/electron-builder") >= (26, 15, 3)
    assert version("node_modules/js-yaml") >= (4, 3, 1)


def test_linux_window_identity_is_synchronised_with_its_desktop_entry():
    """The portal-visible desktop id must match the reverse-DNS file installed by PosterChanOS.

    productName remains the human-facing PosterChan label; desktopName is an identifier used by
    xdg-desktop-portal and must not be replaced with that label.
    """
    assert PKG.get("desktopName") == PKG["build"]["appId"] + ".desktop"
    assert PKG["build"]["linux"].get("syncDesktopName") is True
