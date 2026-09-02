"""THE SHELL'S OWN POPUPS HAVE TO GET ABOVE FLOATING APPLICATIONS.

Reported separately, minutes apart: "start menu is not going over windows" and "notifications do not
go over open windows". They are one bug.

sway paints floating windows above tiled ones unconditionally, and this shell IS the tiled window.
Anything drawn inside its surface — the start menu, the notification panel, the Alt+Tab chooser — is
therefore underneath Firefox and Telegram whatever z-index it carries. It was measured for the
chooser with `grim`: a screenshot of the chooser's exact rectangle came back as Firefox's page.
Focus is not stacking, and nothing inside the page can change it.

The chooser already worked around it by fullscreening the shell for the length of the gesture.
Nobody applied that to the other two, so two of the three surfaces a person uses constantly were
invisible whenever an application was open.

Refcounted, because these overlap: the notification panel closes the start menu on its way up, and
an unconditional release would lower the shell out from under whichever is still open. Released only
on the EDGES for the same reason — `toggleStart(false)` is called defensively from several places.
"""
from __future__ import annotations

import re
from pathlib import Path

OS_JS = (Path(__file__).resolve().parents[2] / "static/js/client/os.js").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    start = OS_JS.index(f"  function {name}(")
    depth, i = 0, OS_JS.index("{", start)
    for j in range(i, len(OS_JS)):
        if OS_JS[j] == "{":
            depth += 1
        elif OS_JS[j] == "}":
            depth -= 1
            if depth == 0:
                return OS_JS[start:j + 1]
    raise AssertionError(name)


def test_the_start_menu_raises_the_shell():
    assert "_raiseShellOverlay(true)" in _fn("toggleStart"), (
        "the start menu opens underneath every floating application")


def test_the_notification_panel_raises_the_shell():
    assert "_raiseShellOverlay(true)" in _fn("toggleNoti"), (
        "the notification panel opens underneath every floating application")


def test_both_release_it_again():
    for fn in ("toggleStart", "toggleNoti", "hideNoti"):
        assert "_raiseShellOverlay(false)" in _fn(fn), f"{fn} never lowers the shell again"


def test_it_is_refcounted_so_overlapping_popups_do_not_fight():
    body = _fn("_raiseShellOverlay")
    assert "_ovlN" in body
    assert "if(_ovlN > 0) return;" in body, (
        "closing one popup lowers the shell while another is still open")


def test_release_only_happens_on_the_closing_edge():
    """`toggleStart(false)` is called defensively when no menu is open; releasing there would
    decrement a refcount it never incremented."""
    for fn, flag in (("toggleStart", "wasStart"), ("toggleNoti", "was")):
        body = _fn(fn)
        assert f"if({flag}) _raiseShellOverlay(false)" in body or \
               f"if(!{flag}) _raiseShellOverlay(true)" in body, fn


def test_a_shell_already_fullscreen_is_left_alone():
    """Lowering it afterwards would un-fullscreen something the user put there themselves."""
    body = _fn("_raiseShellOverlay")
    assert "shell.fullscreen" in body


def test_there_is_a_backstop():
    """A shell left fullscreen hides every window on the workspace with nothing on screen to say
    why — the failure mode the chooser's own comment warns about."""
    body = _fn("_raiseShellOverlay")
    assert re.search(r"setTimeout\(.*_raiseShellOverlay\(false\).*\d{4,}\)", body, re.S)


def test_it_degrades_where_there_is_no_compositor():
    """In a browser there is no pcWM at all, and every one of these surfaces must still open."""
    body = _fn("_raiseShellOverlay")
    assert "if(!window.pcWM || !pcWM.fullscreen) return;" in body
