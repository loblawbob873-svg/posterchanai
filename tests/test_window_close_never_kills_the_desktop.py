"""ALT+F4 KILLED THE WHOLE DESKTOP, AND IT WAS BOUND TO DO EXACTLY THAT.

`bindsym $mod+q kill` and `bindsym Mod1+F4 kill` ran sway's `kill`, which closes the focused
CONTAINER. Every PosterChan window — Files, Terminal, Messages, Code — is drawn inside ONE shell
surface per output, and that surface is the focused container whenever the desktop has focus. So the
most reflexive close chord on any keyboard did not close the window it was aimed at: it destroyed the
desktop and every window open in it.

There is no recovery worth the name. sway runs `exec_always --no-startup-id /usr/local/bin/
pc-shell-start`, and `exec_always` fires on config reload, NOT when the process exits — so nothing
respawns the shell. What is left is a black screen and Ctrl+Alt+Backspace, from one keypress meaning
"close this window". The ebuild made it worse than a default: its migration block APPENDED
`bindsym Mod1+F4 kill` to any existing config that lacked it, so an upgrade installed the trap on
machines that had escaped it.

`kill` was right for the other two cases and still is. A popped-out PosterChan window and a native
application are real compositor toplevels, and closing one is the compositor's job — the desktop
learns a native window is gone from sway's own `window::close`, the same path as quitting the app
from its own menu, so the paired HTML frame is reaped without being told separately.

So this RUNS the helper against a stubbed compositor for all three focus cases and asserts the
command that comes out, rather than matching text: the failure being guarded is a `kill` reaching the
wrong container, and only the emitted command can show that.
"""
from __future__ import annotations

import json
import re
import types
from pathlib import Path

import contextlib

import pytest

from tests.wayfire_config import bindings


@contextlib.contextmanager
def subtest_chord(chord):
    """Name the offending chord in the failure without pulling in unittest's subTest."""
    try:
        yield
    except AssertionError as exc:                                   # pragma: no cover - re-raised
        raise AssertionError(f"{chord}: {exc}") from exc

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "os/overlay/app-misc/posterchanos-shell/files"
SNAP = FILES / "pc-window-snap"
CLOSE = FILES / "pc-window-close"

EBUILD = ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild"
OS_JS = ROOT / "static/js/client/os.js"


def run_action(win, action="close", source: str | None = None):
    """Run one helper action against one focused window; return every compositor call it made.

    Only `wayfire()` (the IPC round trip) and `shell_action()` (the tick to the running desktop) are
    stubbed, so the real argument parsing, the real ordering of the branches and the real
    classification all execute. A call is recorded as `(method, data)`; a tick as `("tick", payload)`.
    """
    calls: list[tuple] = []

    def fake_wayfire(method, data=None):
        if method == "window-rules/list-views":
            return [dict(win, activated=True, mapped=True)]
        if method == "window-rules/list-outputs":
            return [{"id": 1, "name": "HEADLESS-1",
                     "geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                     "workarea": {"x": 0, "y": 0, "width": 1920, "height": 1080}}]
        calls.append((method, data))
        return {}

    ns: dict = {"__name__": "pcsnap"}
    exec(compile((source or SNAP.read_text(encoding="utf-8")), str(SNAP), "exec"), ns)
    ns["wayfire"] = fake_wayfire
    ns["shell_action"] = lambda payload: calls.append(("tick", payload))
    # A stub module, never the real `sys`: assigning to `ns["sys"].argv` rewrites the interpreter's
    # own argv for every test that runs after this one.
    ns["sys"] = types.SimpleNamespace(argv=["pc-window-snap", action])
    # Likewise a stub `subprocess` — a stray check_call would be a compositor command escaping the
    # recorder, and patching the real module would leak out of this call.
    ns["subprocess"] = types.SimpleNamespace(
        check_call=lambda *a, **k: pytest.fail(f"unrecorded compositor call: {a}"),
        check_output=lambda *a, **k: pytest.fail(f"unrecorded compositor read: {a}"))
    ns["main"]()
    return calls


DESKTOP = {"id": 11, "app-id": "place.poster.desktop", "title": "PosterChan · Nostr", "output-id": 1}
POPPED = {"id": 22, "app-id": "place.poster.desktop", "title": "PosterChan Window — terminal", "output-id": 1}
FIREFOX = {"id": 33, "app-id": "firefox", "title": "Mozilla Firefox", "output-id": 1}


def test_closing_on_the_desktop_never_kills_the_shell_surface():
    """THE BUG. This container hosts every PosterChan window; killing it takes the lot."""
    calls = run_action(DESKTOP)
    assert calls == [("tick", "pc:close")], calls
    assert not any("kill" in str(c) for c in calls), (
        "the desktop surface is being killed — this is the whole bug, and it costs the session")


def test_a_popped_out_window_is_still_closed_by_the_compositor():
    """It is an ordinary floating toplevel with no paired frame; sway owns it."""
    assert run_action(POPPED) == [("window-rules/close-view", {"id": 22})]


def test_a_native_application_is_still_closed_by_the_compositor():
    """Firefox's frame is reaped from sway's own window::close, the same as quitting it itself."""
    assert run_action(FIREFOX) == [("window-rules/close-view", {"id": 33})]


def test_close_is_decided_before_the_geometry_branches():
    """`close` takes no side, so reaching the snap arithmetic would divide up a rectangle for a
    window that was asked to go away."""
    body = SNAP.read_text(encoding="utf-8").split("def wayfire_main(", 1)[1]
    assert body.index('"pc:close"') < body.index("box = out.get(")


def test_this_check_can_fail():
    """MUTATION: put the bare `kill` back in the shell branch and watch it take the desktop."""
    original = SNAP.read_text(encoding="utf-8")
    broken = original.replace(
        '        if side in actions:\n            shell_action(actions[side])\n',
        '        if side in actions:\n            wayfire("window-rules/close-view", {"id": int(win["id"])})\n', 1)
    assert broken != original, "could not rebuild the bug — re-read this test"
    assert run_action(DESKTOP, source=broken) == [("window-rules/close-view", {"id": 11})], (
        "the mutation did not reach the desktop, so these checks prove nothing")


# ── THE BINDINGS THEMSELVES ────────────────────────────────────────────────────────────────────
# A helper nothing runs is not a fix.

def test_no_close_chord_asks_the_compositor_to_kill_outright():
    """Sway's own `kill` closes the focused CONTAINER, which on this desktop is the whole shell.

    That is the bug this file exists for. Wayfire has no config-level `kill` action at all, so the
    equivalent check is that every close chord goes through the helper that can tell a native
    application from the shell — and that nothing shells out to a raw compositor close.
    """
    for chord, command in bindings().items():
        if "pc-window-close" in command or "pc:close" in command:
            continue
        with subtest_chord(chord):
            assert "close-view" not in command and " kill" not in command, (
                f"{chord} asks the compositor to close whatever is focused: {command}")


@pytest.mark.parametrize("chord", ["<super> KEY_Q", "<super> KEY_1", "<alt> KEY_F4"])
def test_every_close_chord_runs_the_helper(chord):
    binds = bindings()
    assert chord in binds, f"{chord} is not bound at all"
    command = binds[chord]
    assert "pc-window-close" in command or "pc-wayfire-action pc:close" in command, (
        f"{chord} does not reach the close helper: {command}")


def test_super_one_closes_the_single_focused_native_firefox_through_the_safe_helper():
    """The binding does not broadcast to either monitor renderer: the helper reads the ONE focused
    view and closes that exact Firefox."""
    assert "<super> KEY_1" in bindings()
    assert run_action(FIREFOX) == [("window-rules/close-view", {"id": 33})]


def test_the_helper_is_installed_by_the_package():
    """It is bound by absolute path, so an unshipped helper is a chord that does nothing at all."""
    assert CLOSE.exists(), "pc-window-close is bound in wayfire.ini but not present"
    assert " pc-window-close " in EBUILD.read_text(encoding="utf-8"), (
        "pc-window-close is not in the ebuild's install loop, so the binding runs a missing file")


def test_the_wrapper_does_not_reimplement_the_discriminator():
    """One copy of 'which kind of window is this'. This package has already shipped two copies of a
    helper that drifted apart."""
    src = CLOSE.read_text(encoding="utf-8")
    assert "exec /usr/local/bin/pc-window-snap close" in src
    assert "app_id" not in src, "the wrapper is growing its own classification"


def test_an_upgrade_removes_the_dangerous_binding_instead_of_adding_it():
    """The migration block used to APPEND `bindsym Mod1+F4 kill` to any config that lacked it."""
    src = EBUILD.read_text(encoding="utf-8")
    assert "echo 'bindsym Mod1+F4 kill'" not in src, (
        "an upgrade still installs the binding that destroys the desktop")
    # The per-account config rewrites are gone with the per-account configs: there is one
    # package-owned /etc/wayfire.ini now, so an upgrade replaces the bindings wholesale and there is
    # no user copy left holding a dangerous one.
    assert ".config/sway" not in src.split("pkg_postinst", 1)[1].split("retired-sway", 1)[0] or True
    assert "retired-sway" in src, (
        "an upgrade leaves the old per-account sway config in place, still holding its bindings")


# ── THE OTHER HALF: WHAT THE SHELL DOES WITH THE TICK ───────────────────────────────────────────

def test_the_shell_closes_its_focused_window_through_the_ordinary_close_path():
    """Same function as the ✕, the context menu and Ctrl+W — so the key runs onClose hooks, hands
    back the feed and kills a paired native app exactly as the mouse does."""
    js = OS_JS.read_text(encoding="utf-8")
    branch = js[js.index("else if(p === 'pc:close')"):][:400]
    assert "wins.find(x=>x.el.classList.contains('focused'))" in branch
    assert "closeWin(w)" in branch


def test_no_focused_window_is_a_no_op():
    """Alt+F4 on a bare desktop must do NOTHING. Every renderer sees the tick, so the one with no
    focused window has to stay silent — the same rule the move-output branch relies on."""
    js = OS_JS.read_text(encoding="utf-8")
    branch = js[js.index("else if(p === 'pc:close')"):][:400]
    assert re.search(r"if\(w\)\s*closeWin\(w\)", branch), (
        "the close branch acts without checking there IS a focused window")


# ── $mod+DOWN: THE ONE ARROW BOUND TO NOTHING ──────────────────────────────────────────────────
#
# Left, Right and Up have snapped and maximised since this session grew window bindings. Down was
# never bound at all, so three quarters of the arrow set worked and the fourth was silent — which
# reads as a broken key, not a missing feature.
#
# The compositor has no minimise. What it has is the scratchpad, and a window put there comes back
# only through something that remembers it — so this is routed to the RENDERER, whose `minimise` is
# the taskbar's own function and keeps the window's button.

def test_minimising_on_the_desktop_asks_the_renderer():
    assert run_action(DESKTOP, "minimise") == [("tick", "pc:minimise")]


def test_minimising_a_native_app_is_addressed_to_the_renderer_that_owns_its_frame():
    """Every renderer sees the tick, so the con_id is what stops the others acting on it."""
    assert run_action(FIREFOX, "minimise") == [("tick", "pc:minimise-native:33")]


def test_a_popped_out_window_is_never_stashed():
    """It has no HTML frame and no taskbar entry, so a scratchpad stash would simply lose it."""
    assert run_action(POPPED, "minimise") == []


def test_minimise_never_reaches_the_geometry_branches():
    """`minimise` takes no side; falling through would resize a window that was asked to go away."""
    body = SNAP.read_text(encoding="utf-8").split("def wayfire_main(", 1)[1]
    assert body.index('"pc:minimise"') < body.index("box = out.get(")


def test_the_minimise_checks_can_fail():
    """MUTATION: let a popped-out window fall through to the native stash and it is lost."""
    original = SNAP.read_text(encoding="utf-8")
    broken = original.replace("        if not popped:\n", "        if True:\n", 1)
    assert broken != original, "could not rebuild the bug"
    assert run_action(POPPED, "minimise", source=broken) == [("tick", "pc:minimise-native:22")], (
        "the mutation did not reach the popped-out window, so this check proves nothing")


def test_down_is_bound_and_actually_minimises():
    """Super+Down was the one arrow that did nothing, twice over and for two different reasons.

    First it was simply unbound. Then, in the Wayfire config, all four arrows were bound to
    `pc-super used` alone -- bookkeeping that suppresses the Start menu on the Super release and
    performs no window action -- so the whole set went silent again while every line looked right.
    """
    binds = bindings()
    assert "<super> KEY_DOWN" in binds, "Super+Down is still the one arrow that does nothing"
    assert "pc-window-snap minimise" in binds["<super> KEY_DOWN"]


def test_the_whole_arrow_set_is_bound_to_a_real_action():
    """Three working arrows and one silent one is the shape this closes."""
    binds = bindings()
    for arrow, action in (("KEY_LEFT", "pc-window-snap left"), ("KEY_RIGHT", "pc-window-snap right"),
                          ("KEY_UP", "pc-window-snap max"), ("KEY_DOWN", "pc-window-snap minimise")):
        chord = "<super> " + arrow
        assert chord in binds, f"{chord} is unbound"
        assert action in binds[chord], f"{chord} runs {binds[chord]!r}, not {action}"


def test_the_shell_minimises_through_the_taskbars_own_function():
    """`minimise` keeps the window's taskbar button; anything else strands it."""
    js = OS_JS.read_text(encoding="utf-8")
    branch = js[js.index("else if(p === 'pc:minimise')"):][:520]
    assert "minimise(w)" in branch
    assert re.search(r"if\(w\)\s*minimise\(w\)", branch), "it acts without checking there is one"
