"""A POPPED-OUT POSTERCHAN WINDOW IS SWAY'S TO MOVE — it is an ordinary floating container.

Reported as "posterchan windows are not moveable on sway now with keyboard", and it is a direct
consequence of making popped-out windows real compositor toplevels.

`pc-window-snap` is what $mod+Shift+Arrow runs. It has always had to tell two things apart:

  * the per-output DESKTOP surface — one tiled container per monitor, holding the whole shell.
    Moving or resizing THAT halves a monitor and misses the in-page window entirely, so the key is
    forwarded back to the renderer, which snaps its own focused window.
  * everything else — Firefox, Telegram, foot — which Sway moves directly.

Popped-out windows are now a third case that did not exist when this was written: real Sway
toplevels carrying the SAME app_id as the desktop that opened them. `is_posterchan_shell` matched on
app_id alone, claimed them, and forwarded the keypress to a renderer that then looked for an in-page
window to snap and found none. Nothing moved, and nothing logged.

The discriminator is the TITLE, and deliberately the same one `sway.config`'s own float rule uses:

    for_window [app_id="…place\\.poster\\.desktop$" title="^PosterChan Window"] floating enable

Two rules disagreeing about what a window is would be this bug again, one layer down — so this file
asserts they agree.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-window-snap"
SWAY_CONF = ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config"


@pytest.fixture(scope="module")
def snap():
    """The helper's pure classification half, with no compositor and no argv."""
    src = SNAP.read_text(encoding="utf-8")
    ns: dict = {}
    exec(compile(src.split("def main()")[0], str(SNAP), "exec"), ns)
    return ns


def win(app_id="place.poster.desktop", name="PosterChan Window — terminal", pid=None):
    return {"app_id": app_id, "name": name, "pid": pid}


DESKTOP = win(name="PosterChan · Nostr")
POPPED = win(name="PosterChan Window — terminal")


def test_the_per_output_desktop_is_still_protected(snap):
    """Moving that container halves a monitor and misses the in-page window — the reason this
    special case exists at all."""
    assert snap["is_posterchan_shell"](DESKTOP) is True


def test_a_popped_out_window_is_not_the_shell(snap):
    """THE BUG. Same app_id, completely different thing."""
    assert snap["is_posterchan_shell"](POPPED) is False


@pytest.mark.parametrize("title", ["PosterChan Window — terminal", "PosterChan Window — mail",
                                   "PosterChan Window — messages", "PosterChan Window"])
def test_every_popped_out_title_is_recognised(snap, title):
    assert snap["is_popped_out_window"](win(name=title)) is True


@pytest.mark.parametrize("title", ["PosterChan · Nostr", "PosterChan", "",
                                   "Not A PosterChan Window"])
def test_the_desktop_and_lookalikes_are_not_popped_out(snap, title):
    assert snap["is_popped_out_window"](win(name=title)) is False


@pytest.mark.parametrize("app_id", ["firefox-bin", "TelegramDesktop", "foot", ""])
def test_another_application_is_never_ours(snap, app_id):
    """A window titled like ours but belonging to somebody else must not be claimed — a browser tab
    can be called anything."""
    assert snap["is_popped_out_window"](win(app_id=app_id, name="PosterChan Window — terminal")) is False
    assert snap["is_posterchan_shell"](win(app_id=app_id, name="Mozilla Firefox")) is False


def test_it_agrees_with_the_rule_sway_itself_uses(snap):
    """`sway.config` floats a window by `title="^PosterChan Window"`. If these two ever disagree,
    one of them is looking at a different set of windows than the other."""
    conf = SWAY_CONF.read_text(encoding="utf-8")
    assert 'title="^PosterChan Window"' in conf, "sway.config's float rule has moved"
    src = SNAP.read_text(encoding="utf-8")
    assert 'startswith("PosterChan Window")' in src, (
        "pc-window-snap no longer keys on the same title prefix sway does")


def test_a_window_with_no_title_yet_is_treated_as_the_shell(snap):
    """A surface mid-map has no title. Guessing 'popped-out' there would let Sway move the whole
    desktop container, which is the expensive mistake; the conservative answer is the safe one."""
    assert snap["is_popped_out_window"](win(name=None)) is False


def test_the_helper_still_parses():
    """It is a script sway runs on a keypress; a syntax error is a dead key with no error anywhere."""
    import py_compile, tempfile, shutil
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "pc_window_snap.py"
        shutil.copy(SNAP, target)
        py_compile.compile(str(target), doraise=True)


def test_this_check_can_fail(snap):
    """MUTATION: restore the app_id-only rule and the popped-out window is claimed again."""
    src = SNAP.read_text(encoding="utf-8")
    broken = src.replace("    if is_popped_out_window(win):\n        return False\n", "", 1)
    assert broken != src, "could not rebuild the pre-fix helper — re-read this test"
    ns: dict = {}
    exec(compile(broken.split("def main()")[0], "broken", "exec"), ns)
    assert ns["is_posterchan_shell"](POPPED) is True, (
        "the pre-fix helper no longer claims a popped-out window, so this test proves nothing")
