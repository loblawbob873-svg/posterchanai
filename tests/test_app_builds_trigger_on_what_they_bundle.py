"""A FILE THE APPS BUNDLE MUST REBUILD THE APPS.

`desktop/build-www.sh` derives the bundle's stylesheets from `templates/client.html` rather than
keeping a list — that derivation exists because `monero-wallet.css` was once present on the web and
absent from the bundle, so the whole Monero wallet rendered unstyled in the APK and perfectly in a
browser ("monero works fine on web, android is broken").

The CI trigger paths had the list the build script deliberately stopped keeping: `static/css/client.css`
and nothing else. So a change to `concord.css` — which IS copied into both bundles — shipped to the
web and rebuilt neither app. The same failure one layer out: right on the web, stale in the apps,
with nothing to say so.

Both are `static/css/**` now, and this asserts the rule rather than the two names: every stylesheet
the shell references has to be covered by the paths that rebuild the things carrying it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL = (ROOT / "templates/client.html").read_text(encoding="utf-8")
WORKFLOWS = {name: (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
             for name in ("android.yml", "desktop.yml")}


def _bundled_stylesheets():
    """Exactly what build-www.sh copies: the hrefs the shell references, plus the runtime-only one."""
    found = set(re.findall(r'href="/static/css/([^"?]+)', SHELL))
    found.add("rtl.css")           # loaded at runtime by i18n.js, in no template
    return found


def _trigger_paths(yml):
    head = yml.split("jobs:", 1)[0]
    return set(re.findall(r"^\s*-\s*'([^']+)'", head, re.M))


def _covers(paths, rel):
    for p in paths:
        if p == rel:
            return True
        if p.endswith("/**") and rel.startswith(p[:-2]):
            return True
    return False


def test_the_shell_really_does_reference_more_than_one_stylesheet():
    """Proof this file is about something — with one stylesheet the rule would be vacuous."""
    sheets = _bundled_stylesheets()
    assert len(sheets) >= 3, sheets
    assert "concord.css" in sheets, (
        "re-point this test: the shell no longer loads concord.css, which is the one that exposed this")


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_every_bundled_stylesheet_rebuilds_the_apps(name):
    paths = _trigger_paths(WORKFLOWS[name])
    missing = sorted(s for s in _bundled_stylesheets()
                     if not _covers(paths, "static/css/%s" % s))
    assert not missing, (
        "%s does not rebuild when these stylesheets change, and both bundles COPY them in: %s. "
        "The change reaches the web and the apps keep the old one." % (name, ", ".join(missing)))


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_the_client_code_itself_is_covered_too(name):
    """The other half of what the bundles carry. Cheap to assert and the same failure if it goes."""
    paths = _trigger_paths(WORKFLOWS[name])
    for rel in ("static/js/client/app.js", "templates/client.html", "static/i18n/en.json"):
        assert _covers(paths, rel), "%s no longer rebuilds for %s" % (name, rel)


def _zapstore_icon():
    m = re.search(r"^icon:\s*\./(\S+)", (ROOT / "zapstore.yaml").read_text(encoding="utf-8"), re.M)
    assert m, "zapstore.yaml names no icon"
    return m.group(1)


def _desktop_icon():
    script = __import__("json").loads((ROOT / "desktop/package.json").read_text(encoding="utf-8"))["scripts"]["icon"]
    m = re.search(r"copyFileSync\('\.\./([^']+)'", script)
    assert m, "desktop/package.json's icon script copies no file -- re-point this test"
    return m.group(1)


def test_a_new_app_icon_reaches_zapstore_and_the_desktop_app():
    """"make sure to update the zapstore icon" (2026-10-29). The Zapstore listing's icon is the file zapstore.yaml
    names, published by the APK build -- which did not run for that file, so an icon change reached the web and left
    Zapstore (and the desktop app, which copies the same file at build time) on the old one until some unrelated
    change rebuilt them."""
    assert _covers(_trigger_paths(WORKFLOWS["android.yml"]), _zapstore_icon()), (
        "android.yml does not rebuild (and so does not republish to Zapstore) when %s changes" % _zapstore_icon())
    assert _covers(_trigger_paths(WORKFLOWS["desktop.yml"]), _desktop_icon()), (
        "desktop.yml does not rebuild when the icon it copies in (%s) changes" % _desktop_icon())
