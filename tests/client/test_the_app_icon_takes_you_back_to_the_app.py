"""AN APP ICON MUST TAKE YOU BACK TO THE APP, NOT JUST TO ITS WINDOW.

Reported as "i just opened a profile from social and now can't go back to social on posterchanOS".

`renderProfileView` and `renderThread` set the client's `VIEW` themselves and never go through
`switchView` — that is stated in app.js and is deliberate. So when you open a profile from inside the
Social window, the window still calls itself `home` while its feed holds a profile.

`openApp` had:

    if(existing.view === view && !shouldSelectMessagesTab(existing, view)){
      focusWin(existing); return existing;      // focus only, never a repaint
    }

so pressing Social focused the window and handed the profile straight back. The Social icon — the
one thing a person reaches for to get out — was what kept them in. On a desktop window there is no
sidebar and no browser Back, so there was no other route home.

The Messages/Concord branch immediately below has always repainted, for exactly this reason. This is
the same rule for every other app: a launch is a request for the APP, not merely for its frame.
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


def _same_view_branch() -> str:
    body = _open_app()
    start = body.index("if(existing.view === view && !shouldSelectMessagesTab(existing, view)){")
    return body[start:body.index("/* Messages and Concord are tabs", start)]


def test_the_branch_no_longer_only_focuses():
    """THE BUG, in one line."""
    branch = _same_view_branch()
    assert "focusWin(existing); return existing;" not in branch, (
        "launching an app whose window exists still only focuses it, so a window navigated to a "
        "profile hands the profile back and there is no way home")


def test_it_repaints_the_apps_own_view():
    branch = _same_view_branch()
    assert "switchView(view)" in branch


def test_it_only_repaints_when_the_view_actually_differs():
    """An ordinary focus must stay free — this runs on every icon press."""
    branch = _same_view_branch()
    assert re.search(r"if\(live && live !== view\)", branch), (
        "the repaint is unconditional; focusing a window you are already on would redraw it")


def test_it_reads_the_live_view_defensively():
    """`PC()` is the client bridge and may be absent mid-boot; a throw here would break every icon."""
    branch = _same_view_branch()
    assert "try{" in branch and "catch" in branch


def test_the_repaint_is_guarded_against_making_another_window():
    """Without the guard, switchView can route outward and open a SECOND frame — which is the
    duplicate the branch below this one exists to prevent."""
    branch = _same_view_branch()
    assert "repainting++" in branch and "repainting--" in branch


def test_the_windows_bookkeeping_is_updated_too():
    """`appView` is what a parked window repaints from. Left saying 'profile', the window would go
    back to the profile the next time it was restored."""
    branch = _same_view_branch()
    assert "existing.appView = view" in branch


def test_the_messages_tab_branch_is_untouched():
    """It solved this for tabs long ago and must keep working — a Direct Messages launch onto a
    Communities window still has to select the tab."""
    body = _open_app()
    assert "shouldSelectMessagesTab(existing, view)" in body
    assert "existing.messagesTab=view" in body
