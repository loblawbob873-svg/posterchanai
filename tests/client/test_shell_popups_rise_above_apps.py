"""RAISING THE SHELL FOR A PANEL THAT STAYS OPEN HIDES THE WHOLE DESKTOP — do not do it.

This file previously asserted the opposite, and that was wrong.

The problem is real: sway paints floating windows above tiled ones unconditionally and this shell IS
the tiled window, so the start menu and the notification panel really do open underneath Firefox and
Telegram ("start menu is not going over windows", "notifications do not go over open windows").

But the only lever available is FULLSCREENING the shell, and a fullscreen surface hides every other
window on the workspace. For the Alt+Tab chooser that is acceptable — the gesture lasts a moment and
looking at the switcher is the entire point. For a menu that stays open it means pressing Start makes
the desktop vanish, which is what happened: "why does pressing the start menu hide everything on the
desktop! wtf is this", minutes after it shipped. `_altRaiseShell`'s own comment had already warned
that a shell left fullscreen hides every window with nothing on screen to say why; I applied it to a
persistent surface anyway.

A menu drawn under an application is a smaller harm than a menu that hides the application. Doing
this properly needs a layer-shell surface — a real overlay the compositor stacks above everything —
which is a piece of work, not a flag.
"""
from __future__ import annotations

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


def test_the_start_menu_does_not_fullscreen_the_shell():
    """THE REGRESSION, named. Pressing Start must never make the desktop disappear."""
    assert "_raiseShellOverlay" not in _fn("toggleStart"), (
        "the start menu raises the shell again — a fullscreen shell hides every window on the "
        "workspace, so opening the menu empties the desktop")


def test_the_notification_panel_does_not_either():
    for fn in ("toggleNoti", "hideNoti"):
        assert "_raiseShellOverlay" not in _fn(fn), f"{fn} raises the shell again"


def test_the_helper_is_gone_entirely():
    """Left in place unused it is an invitation: the next panel that wants to be on top finds a
    ready-made way to empty the desktop. The only surface allowed to fullscreen the shell is the
    Alt+Tab gesture, which has its own function and ends on its own."""
    assert "_raiseShellOverlay" not in OS_JS, (
        "the general shell-raise helper is back; nothing persistent may use it")


def test_the_alt_tab_chooser_keeps_its_own_raise():
    """It is a momentary gesture and the switcher is what you are looking at — the one case where
    hiding the windows behind it is correct."""
    assert "_altRaiseShell(true)" in OS_JS
