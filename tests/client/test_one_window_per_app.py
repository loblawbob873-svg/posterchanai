"""LAUNCHING AN APP THAT IS ALREADY OPEN MUST FOCUS IT, NOT OPEN A SECOND ONE.

Found while measuring the machine for something else: the live tree carried
`PosterChan Window — messages` TWICE and `PosterChan Window — notifications` TWICE, from ordinary
icon presses.

`openApp` looks for an existing window with `wins.find(...)` — and `wins` holds IN-PAGE frames only.
A window popped out to a real compositor toplevel lives in `nativeTasks`, so every later launch of
that app sailed straight past the check and opened another one. It is the same shape as "i just
opened a profile from social and now can't go back": a launch that fails to find the window you
already have. One layer further out.

A minimised window is focused AND restored, because a launch that silently does nothing is the other
way for this to read as broken.
"""
from __future__ import annotations

import re
from pathlib import Path

OS_JS = (Path(__file__).resolve().parents[2] / "static/js/client/os.js").read_text(encoding="utf-8")


def _open_app() -> str:
    start = OS_JS.index("  function openApp(view, label, icon, render, noFeed, direct){")
    depth, i = 0, OS_JS.index("{", start)
    for j in range(i, len(OS_JS)):
        if OS_JS[j] == "{":
            depth += 1
        elif OS_JS[j] == "}":
            depth -= 1
            if depth == 0:
                return OS_JS[start:j + 1]
    raise AssertionError("openApp")


def test_an_existing_toplevel_is_looked_for():
    """THE BUG. `wins` cannot see a popped-out window, so it was never consulted about one."""
    body = _open_app()
    assert "nativeTasks.find(" in body, (
        "openApp still only searches `wins`, so launching an app already open in a real window "
        "opens a second one")


def test_the_search_happens_before_a_new_window_is_opened():
    """Order is the whole fix: PCOSWin.open() unconditionally created a toplevel."""
    body = _open_app()
    assert body.index("nativeTasks.find(") < body.index("PCOSWin.open(")


def test_it_matches_on_the_view_not_the_title():
    """A title is a label and gets localised and renamed; the view is the identity."""
    body = _open_app()
    line = re.search(r"nativeTasks\.find\([^\n]*", body).group(0)
    assert "r.view === view" in line and "r.own" in line


def test_only_our_own_windows_are_matched():
    """`nativeTasks` also carries Firefox and Telegram. Focusing Firefox because somebody pressed
    Notes would be a far stranger bug than the one being fixed."""
    body = _open_app()
    line = re.search(r"nativeTasks\.find\([^\n]*", body).group(0)
    assert "r.own" in line


def test_a_minimised_window_is_restored_not_just_focused():
    """Focusing a scratchpad window without showing it is a launch that does nothing at all."""
    body = _open_app()
    block = body[body.index("nativeTasks.find("):body.index("PCOSWin.open(")]
    assert "stashed" in block and "pcWM.show" in block


def test_it_does_not_fall_through_and_open_one_anyway():
    body = _open_app()
    block = body[body.index("nativeTasks.find("):body.index("let real = null;")]
    assert "return null;" in block


def test_it_is_guarded_where_there_is_no_compositor():
    """In a browser there is no pcWM and no toplevels; this must not throw on every icon press."""
    body = _open_app()
    block = body[body.index("nativeTasks.find("):body.index("let real = null;")]
    assert "window.pcWM" in block and "try{" in block
