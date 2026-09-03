"""SWAY OWNS A NATIVE WINDOW. WE OWN A TASKBAR BUTTON FOR IT.

Hosting Firefox and Telegram inside PosterChan frames is the single cause behind a run of separate
reports, and each one is a different face of the same structural mistake:

  * "fixfox window border goes behind telegram, making it terrible to work with dinwows" — a hosted
    window's CONTENT is a floating sway surface while its FRAME is drawn by this shell, which is the
    TILED window underneath. sway paints floating above tiled, always, so a window's own border and
    title bar are covered by any other app — even while it is the window you are using. Two layers
    for one window. Nothing in z-order or CSS can fix that.
  * "telegram on desktop is swallowing windows and its not separating", "Settings is now glitching
    my screen and telegram, sticking to that", "click on notifications you get a desktop window and
    weird colors" — all of them the parking-and-screenshot machinery, which exists only to fake
    "bring to front" against the same constraint.
  * "firefox is colliding with steam and fighting for focus on desktop now too" — two owners of
    focus, arguing.
  * "cyberpunk 2077 loads in small window, does not capture mouse" — a game placed into a frame in
    the moment before it can go fullscreen.

With the compositor owning them: the border is on the same layer as the content, stacking is just
stacking, focus has one owner, and nothing places, parks or screenshots anything.

They do NOT lose their taskbar button. `nativeTasks` is the path for windows the desktop does not
host, and it already carries the icon, the focus/minimise toggle, maximise and close — it was built
for exactly this and was only ever reached by windows adoption had missed.

The frames remain one flag away (`pc_os_host_native`), because looking like part of the desktop is
the real thing being traded off.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")


def test_native_windows_are_not_hosted_by_default():
    """THE CHANGE. Adoption is what wrapped a compositor window in a frame we then had to place."""
    block = OS_JS[OS_JS.index("const _hostNative = (()=>{"):]
    block = block[:block.index("changed=true;\n    }") + 20]
    assert "if(_hostNative) for(const r of rows)" in block, (
        "every native window is adopted into a PosterChan frame again")
    assert "'pc_os_host_native'" in block


def test_the_old_behaviour_is_still_reachable():
    """A flag, not a deletion: the frames are what make a hosted app look like part of the desktop,
    and that is a real thing to want back."""
    block = OS_JS[OS_JS.index("const _hostNative = (()=>{"):][:400]
    assert "localStorage.getItem('pc_os_host_native') === '1'" in block
    assert "catch(_){ return false; }" in block, (
        "a browser with storage disabled would throw inside the window enumeration")


def test_an_unhosted_window_still_gets_a_taskbar_button():
    """The thing that makes this safe. Without it the change would simply lose every native app."""
    assert "nativeTasks=rows.filter(r=>!nativeWins().some(" in OS_JS, (
        "nativeTasks no longer collects the windows the desktop does not host")
    bar = OS_JS[OS_JS.index("+ nativeTasks.map(w =>"):]
    bar = bar[:bar.index("</div>")]
    # The inline _ [] X buttons were REMOVED at the owner's request ("i do not want to see _ [] X
    # on every taskbar app ... rightclick on a taskbar open app will suffice"). What must survive is
    # the window's presence in the bar and a way to act on it, which is the context menu.
    assert 'data-kind="native"' in bar, "an unhosted window has no taskbar button at all"
    assert "oncontextmenu" in OS_JS, "the taskbar has no right-click menu, so nothing can act on it"
    assert "appIcon(w)" in bar, "the button lost its icon"


def test_clicking_that_button_focuses_minimises_and_restores():
    """One button, three states — the same behaviour a hosted frame's taskbar entry had."""
    handler = OS_JS[OS_JS.index("if(b.dataset.kind === 'native'){"):]
    handler = handler[:handler.index("const w = wins.find(")]
    assert "pcWM.hide(w.id)" in handler        # focused → minimise
    assert "pcWM.show(w.id)" in handler        # parked → restore
    assert "_focusNativeDecorated(w.id)" in handler
    assert "await pcWM.snapshot()" in handler, (
        "Steam's cached focus flag can lag its helper windows, so its task button cannot reliably minimise")
    assert "if(live.focused && !live.stashed) await pcWM.hide(w.id)" in handler


def test_the_compositor_is_asked_to_decorate_them():
    """If sway is not drawing the border, an unhosted window has none at all — which would be worse
    than the bug this fixes."""
    assert "pcWM.decorate" in OS_JS
    block = OS_JS[OS_JS.index("if(pcWM.decorate) for(const r of rows)"):]
    block = block[:block.index("\n    }") + 6]
    assert "_nativeDecorated" in block, "decoration is re-requested every pass"


def test_the_placement_machinery_is_left_intact():
    """Deliberately NOT deleted. With no hosted windows it simply has nothing to act on, and the
    flag brings the old model back whole — removing it would make that flag a lie."""
    for kept in ("function stashPlan", "nativeFullscreen", "native-stashed"):
        assert kept in OS_JS or kept in (ROOT / "static/js/client/osnative.js").read_text(
            encoding="utf-8"), f"{kept} was removed along with the hosting"
