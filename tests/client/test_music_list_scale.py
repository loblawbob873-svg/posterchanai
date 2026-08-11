"""A big music library must not cost the whole app.

Run: venv-unified/bin/python -m pytest tests/client/test_music_list_scale.py

The Music window rendered EVERY track in the library at once. A row is seven elements, four of them
buttons carrying an inline `<svg><use>`, and each `<use>` instantiates a shadow tree — so the 2422-track
library this file's own tidy-up code was written for put roughly 17,000 nodes into the document and left
them there for as long as the window was open. Every style recalculation and every full-page layout then
walked them, which is how opening MUSIC made dragging an unrelated desktop WINDOW slow.

It was reported with nothing playing, in both Firefox and the packaged Windows app — which is what ruled
out the visualiser and the player's glow/wiggle animations, all of which are gated on actually playing.
Nothing was looping. The cost was the document.

On top of that, each render re-bound the row handlers with four `$$('.track-*').forEach` passes over the
whole library — ~10,000 closures per paint, and the list repaints on every keystroke of the search box.

These are source assertions, not behaviour: the renderer lives inside app.js's IIFE with FilesIdx,
MusicOffline and the player around it, and a harness faithful enough to run it would be asserting its own
stubs. What can regress silently is the SHAPE — an unpaged map(), or per-row wiring creeping back — so
that is what is pinned here.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")


@pytest.fixture(scope="module")
def src():
    with open(APP, encoding="utf-8") as fh:
        return fh.read()


def _fn(src, name):
    """One function declaration, closed by brace counting — the next `function` keyword is not a
    reliable end marker (`_updateMusicListBtns` is followed by `const MusicPlayer = {`)."""
    i = src.index("function " + name + "(")
    depth, j, started = 0, i, False
    while j < len(src):
        if src[j] == "{":
            depth += 1
            started = True
        elif src[j] == "}":
            depth -= 1
            if started and depth == 0:
                return src[i : j + 1]
        j += 1
    raise AssertionError("could not find the end of " + name)


def _render_music_list(src):
    return _fn(src, "_renderMusicList")


def test_the_list_is_paged(src):
    body = _render_music_list(src)
    assert "MUS_PAGE" in body, (
        "the track rows are no longer sliced into pages — a large library goes back into the document "
        "all at once, which is what made the whole desktop sluggish")
    assert re.search(r"rows\.slice\(0,\s*MUS_PAGE\)", body), "expected the first page to be a slice"
    assert "_musRest" in body, "the unrendered rows must be stashed for the Show-more button"


def test_more_rows_are_appended_not_re_rendered(src):
    """Re-rendering with a bigger slice rebuilds every row already on screen, so walking to the end of
    a large library would cost exactly the quadratic pile of DOM work the paging exists to avoid."""
    body = _fn(src, "_musMore")
    assert "insertAdjacentHTML" in body, "_musMore must append the next page"
    assert "_renderMusicList(" not in body, (
        "_musMore re-renders the whole list instead of appending — that is the quadratic path")


def test_rows_are_not_wired_one_by_one(src):
    body = _render_music_list(src)
    for cls in ("track-play", "track-keep", "track-add", "track-dl", "track-del"):
        assert not re.search(r"\$\$\('\." + cls + r"'[^)]*\)\.forEach", body), (
            f"{cls} is bound per row again — that is ~one closure per track per repaint, and the list "
            "repaints on every keystroke of the search box. Use the delegated grid.onclick.")
    assert "grid.onclick" in body, "the delegated row handler is gone"


def test_the_delegated_handler_cannot_accumulate(src):
    """`grid.onclick =` is a single slot, so a re-render replaces it. addEventListener would stack a
    second handler on every paint and fire each action twice, then three times."""
    body = _render_music_list(src)
    assert "grid.addEventListener('click'" not in body, (
        "a re-render would leave the previous handler attached — use the assignable onclick slot")


def test_the_playing_row_is_marked_without_destroying_its_icon(src):
    body = _fn(src, "_updateMusicListBtns")
    assert "textContent" not in body, (
        "the play button holds an inline <svg>; assigning textContent to it deletes the icon for good")
    assert "classList.toggle" in body, "expected the playing row to be marked with a class"
    assert re.search(r"\$\$\('\.track-play',\s*scope\)", body), (
        "this queries the whole DOCUMENT again — it runs on every render and every play/pause, and "
        "walking the entire page is the cost this change is about")


def test_the_stylesheet_swaps_the_glyph(src):
    with open(os.path.join(ROOT, "static", "css", "client.css"), encoding="utf-8") as fh:
        css = fh.read()
    assert ".track-play.playing" in css, (
        "nothing renders the paused/playing state — the class is set and shows nothing")
    assert ".mus-more" in css, "the Show-more control has no styling"
