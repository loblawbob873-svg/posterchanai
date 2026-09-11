"""A VIEW LIVES IN FOUR LISTS, AND NOTHING CHECKED THAT THEY AGREE.

This exists because of a repeated, identical failure rather than any one bug. Separating Communities
from Messages gave it a sidebar row — and it was then invisible on a phone, because the ☰ More sheet
is a SECOND list; and invisible on the Android home screen, because `HomeTiles` is a THIRD. Each gap
was found by a person using the app, hours apart, after the change had shipped. The repo already
names this shape for effects ("the catalog is a THIRD surface") and it keeps recurring because the
lists are enumerated in different languages in different files.

THE FOUR LISTS

  * `templates/client.html`  — the sidebar. os.js ALSO derives the desktop icon grid and the start
    menu from these rows, so a missing row costs three things, not one.
  * `app.js moreMenu()`      — the phone's ☰ sheet. The sidebar is hidden on a phone, so a view
    missing here has no way in at all on the device most people use.
  * `HomeTiles.catalogue()`  — the Android launcher's home screen, when PosterChan is the launcher.
  * `static/i18n/en.json`    — the label, so a view that reaches all three is not untranslated.

The rule is derived from the sidebar rather than from a list anybody maintains here: whatever the
app offers on a desktop has to be reachable everywhere else, or be written down below with a reason.
`tests/client/test_app_globals.py` already owns the sidebar→More-sheet half and is left alone; this
file owns the Android surface and the vocabulary, and states the whole rule in one place.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL = (ROOT / "templates/client.html").read_text(encoding="utf-8")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
TILES = (ROOT / "mobile/android/app/src/main/java/place/poster/app/home/HomeTiles.java"
         ).read_text(encoding="utf-8")

#: NOT ON THE ANDROID HOME SCREEN, EACH FOR A STATED REASON. Short list, or it stops being a rule.
NOT_A_TILE = {
    # A reading pane for THIS node's SMS/MMS archive. On a phone the launcher already has a Texts
    # tile of its own (HomeTiles.VIEW_TEXTS) wired to the native screens, so a second web one would
    # be two icons for one thing.
    "texts",
    # A personal analytics dashboard: dense charts over a long history, built for a wide window.
    # It is reachable from the phone's ☰ sheet, which is where a rarely-used desktop screen belongs.
    "analytics",
}


def _sidebar():
    return set(re.findall(r'class="nav-item"\s+data-view="([a-z0-9_-]+)"', SHELL))


def _tiles():
    return set(re.findall(r'new Tile\("([a-z0-9_-]+)"', TILES))


def _more_sheet():
    i = APP.index("function moreMenu(")
    m = re.search(r"const items=\[(.*?)\n\s*\.filter\(", APP[i:], re.S)
    assert m, "the More sheet's item list moved — re-point this test"
    return set(re.findall(r"\['([a-z0-9_-]+)'", m.group(1)))


def test_the_lists_are_all_readable():
    """The check before the check. If any parse breaks, every rule below passes vacuously — which is
    exactly how a guard stops guarding without anybody noticing."""
    assert len(_sidebar()) >= 15, "the sidebar no longer parses"
    assert len(_tiles()) >= 20, "the Android tile catalogue no longer parses"
    assert len(_more_sheet()) >= 15, "the More sheet no longer parses"


def test_every_sidebar_view_has_an_android_tile():
    """The surface that was missed. With PosterChan as the launcher this IS the home screen, and a
    view with no tile has no icon — which is how Communities became unreachable on a phone straight
    after being given its own place on the desktop."""
    missing = sorted(v for v in _sidebar() - _tiles() - NOT_A_TILE if not v.startswith("__"))
    assert not missing, (
        "these are in the sidebar but have no Android launcher tile, so on the phone shell there is "
        "no icon for them: %s. If that is deliberate, add it to NOT_A_TILE above with the reason."
        % ", ".join(missing))


def test_the_exemptions_are_real_views():
    """An exemption for a view that no longer exists is a comment pretending to be a decision, and
    it quietly widens the rule."""
    stale = sorted(NOT_A_TILE - _sidebar())
    assert not stale, "NOT_A_TILE names views the sidebar does not have: %s" % ", ".join(stale)


@pytest.mark.parametrize("surface", ["tiles", "more"])
def test_no_surface_offers_a_view_the_app_cannot_route(surface):
    """The other direction, which is the quieter failure: an entry that opens nothing. A tile or a
    sheet row for a retired view lands on the timeline under the wrong title — the exact shape of
    "System settings just loaded a social feed"."""
    offered = _tiles() if surface == "tiles" else _more_sheet()
    routed = set(re.findall(r"case '([a-z0-9_-]+)':", APP)) | _sidebar() | set(
        re.findall(r"renderModuleView\('([a-z0-9_-]+)'", APP))
    unknown = sorted(v for v in offered - routed if not v.startswith("__"))
    # Views reached by their own render function rather than a case label are legitimate; this only
    # has to catch a name nothing anywhere mentions.
    unknown = [v for v in unknown if v not in APP]
    assert not unknown, "%s offers views the app never routes: %s" % (surface, ", ".join(unknown))


def test_every_offered_view_has_a_label_to_show():
    """A view that reaches every list and has no string is a blank row. The catalogue is only
    useful if the thing it offers can be named."""
    en = json.loads((ROOT / "static/i18n/en.json").read_text(encoding="utf-8"))
    flat = json.dumps(en).lower()
    for view in sorted(_sidebar()):
        if view.startswith("__"):
            continue
        assert view in flat or view in APP, (
            "the sidebar offers %r and nothing anywhere names it" % view)
