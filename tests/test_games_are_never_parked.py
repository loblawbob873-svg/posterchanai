"""A FULLSCREEN GAME IS NOT A WINDOW TO MANAGE — and every change to the desktop must keep believing it.

Asked for directly while the window rewrite was in flight: "make sure this works with steam games
good too don't forget".

Steam titles are fullscreened by the compositor (`for_window [class="^steam_app_.*"] fullscreen
enable, inhibit_idle fullscreen`), and the desktop's placement pass already knows to leave them
alone — the comment beside the guard records what happens otherwise: "Turning every new surface
floating in place() silently cancelled a game's fullscreen/pointer lock and let the mouse escape to
another monitor."

Three things have to stay true, and each is one line away from not being:

  1. The fullscreen branch runs BEFORE the parking branch. If parking ever wins, a PosterChan window
     overlapping a game takes the game off the screen mid-play and replaces it with a screenshot.
  2. Parking a game would also mean grim-CAPTURING it — a full-resolution screen grab every time a
     window moves over it, which is a stutter you cannot debug from a log.
  3. The compositor rule that makes a game fullscreen in the first place has to survive edits to
     sway.config — the window rewrite added rules to that same file.

This is a source-order test on purpose. The ordering is the property, and it is invisible to any
test that only asks whether both branches exist.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
SWAY = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config").read_text(encoding="utf-8")
MAIN_JS = (ROOT / "desktop/main.js").read_text(encoding="utf-8")


def _placement_loop() -> str:
    start = OS_JS.index("const plan = NAT().stashPlan(")
    return OS_JS[start:OS_JS.index("_natShell=null; _natShellAt=0; _natAgain=true;", start)]


def test_a_fullscreen_client_is_decided_before_anything_can_park_it():
    """THE ORDERING, which is the whole guarantee. A game reaches its own branch and leaves the loop
    before the stash branch can consider it."""
    loop = _placement_loop()
    assert "it.w.nativeFullscreen" in loop, "the fullscreen guard is gone from the placement pass"
    assert "stash.has(it.native)" in loop, "re-read this test: the parking branch has moved"
    assert loop.index("it.w.nativeFullscreen") < loop.index("stash.has(it.native)"), (
        "parking is now decided before fullscreen, so a PosterChan window overlapping a game would "
        "take the game off the screen and leave a screenshot in its place")


def test_the_fullscreen_branch_leaves_the_loop():
    """It must `continue`, not fall through into placement — placing a fullscreen surface is what
    cancelled the pointer lock."""
    loop = _placement_loop()
    branch = loop[loop.index("if(it.w.nativeFullscreen){"):]
    branch = branch[:branch.index("it.w.el.classList.remove('native-fullscreen-frame')")]
    assert branch.count("continue;") >= 2, (
        "the fullscreen and settling-game branches must each leave the loop rather than fall "
        "through into placement")


def test_a_game_is_never_screen_captured():
    """Previews exist for parked windows. A game is never parked, so it is never grabbed — but the
    capture path also refuses on its own, because a full-resolution grim of a running game is a
    stutter with nothing in any log to explain it."""
    handler = MAIN_JS[MAIN_JS.index("ipcMain.handle('pc:wm:preview'"):]
    handler = handler[:handler.index("ipcMain.handle('pc:wm:close'")]
    assert "target.stashed" in handler and "visible===false" in handler.replace(" ", ""), (
        "the preview no longer refuses a window that is not parked and visible")


def test_the_compositor_still_fullscreens_steam_titles():
    """The rule the whole contract rests on, in a file the window rewrite also edits."""
    assert re.search(r'for_window \[class="\^steam_app_\.\*"\] fullscreen enable', SWAY), (
        "the Steam fullscreen rule is gone from sway.config")
    assert "inhibit_idle fullscreen" in SWAY, "a game would let the screen blank mid-play"


def test_the_new_window_rule_cannot_match_a_game():
    """The rewrite floats PosterChan windows by TITLE while sharing the desktop's app_id. That match
    must be anchored to our own application, or a game whose title happened to start the same way
    would be dragged out of fullscreen."""
    for line in SWAY.splitlines():
        if 'title="^PosterChan Window"' in line:
            assert "app_id=" in line or "class=" in line, (
                "the window float rule matches on title alone, so any client could match it: " + line)
            assert "posterchan" in line.lower(), (
                "the window float rule is not anchored to our own application: " + line)


# ---------------------------------------------------------------------------------------------
# AND THE WINDOW BEFORE FULLSCREEN ARRIVES — which is where this actually broke, and which every
# test above was blind to. They asserted the fullscreen guard; the game is not fullscreen yet when
# the damage is done.

def test_a_game_is_recognised_before_it_is_fullscreen():
    """Reported as "cyberpunk 2077, loads in small window, does not capture mouse, game loads full
    screen then when you click, goes to desktop and loads cyberpunk in small window".

    `nativeFullscreen` is only true once the game HAS fullscreen. The surface maps first, this
    desktop sizes it into a frame, and sway's `for_window [class="^steam_app_.*"] fullscreen enable`
    arrives after — so the game opens small, and placing it is what cancels its pointer lock.

    The class is available immediately and is the same string sway keys its own rule on."""
    assert "const _GAME_APP" in OS_JS, "nothing identifies a game before it is fullscreen"
    match = re.search(r"const _GAME_APP = (/.*?/i);", OS_JS)
    assert match, "the game pattern has moved"
    pattern = match.group(1)
    assert "steam_app_" in pattern, "the Steam class is not recognised"
    # The Steam CLIENT is an ordinary window and must keep its frame.
    assert r"steam_app_\d+" in pattern, (
        "the pattern would match Steam's own UI, which is a normal window people resize")


def test_the_placement_pass_skips_a_game_that_is_not_yet_fullscreen():
    """The two conditions are ORed in the branch that leaves the loop, so a game is exempt from the
    first frame it maps in — not from the moment it wins fullscreen."""
    loop = _placement_loop()
    assert "it.w.nativeGame && (Date.now() - (it.w.nativeGameAt || 0)) < GAME_SETTLE_MS" in loop, (
        "a game is only spared once it is already fullscreen, which is after it has been placed "
        "small and had its pointer lock cancelled")
    assert loop.index("it.w.nativeGame") < loop.index("stash.has(it.native)")


def test_a_game_is_spared_placement_but_never_forced_fullscreen():
    """THE OTHER HALF, and the one that was missing. The exemption was written into the branch that
    also calls `pcWM.fullscreen(id,true)`, so every game window was promoted to fullscreen and held
    there on top of everything — reported as "steam is overlapping terminal". Sway's own
    `for_window [class=^steam_app_] fullscreen enable` rule is what makes a game fullscreen; the
    shell only has to keep its hands off while it starts."""
    loop = _placement_loop()
    game = loop[loop.index("if(it.w.nativeGame &&"):]
    game = game[:game.index("continue;") + len("continue;")]
    assert "pcWM.fullscreen" not in game, (
        "the shell forces a game fullscreen again — a windowed game then covers every PosterChan "
        "window for as long as it runs")


def test_the_exemption_ends_so_a_windowed_game_stops_owning_the_screen():
    """Unbounded, this is indistinguishable from the bug it replaced: a game that never goes
    fullscreen would be exempt from parking for ever and the terminal could never be used."""
    import re as _re
    m = _re.search(r"const GAME_SETTLE_MS = (\d+);", OS_JS)
    assert m, "the settle window is gone — the exemption is unbounded again"
    assert 10000 <= int(m.group(1)) <= 120000, (
        f"a {int(m.group(1))}ms settle window is not a launch window")


def test_the_flag_is_set_when_the_window_is_adopted_and_kept_current():
    """At adoption, because the first placement pass can run before any refresh; and on refresh,
    because a game relaunching into an existing frame arrives as a new compositor window."""
    adopt = OS_JS[OS_JS.index("function adoptNative(nw){"):]
    adopt = adopt[:adopt.index("\n  function ")]
    assert "w.nativeGame=isGameApp(nw)" in adopt
    assert "if(isGameApp(r) && !w.nativeGame){ w.nativeGame=true; w.nativeGameAt=Date.now(); }" in OS_JS, (
        "the flag is never refreshed, so a game that maps before its class is readable stays a "
        "normal window for ever")


def test_late_xwayland_game_class_gets_one_fullscreen_request():
    """Sway's map-time rule cannot match a WM_CLASS that Proton publishes after mapping."""
    block = OS_JS[OS_JS.index("const liveGameIds = new Set"):]
    block = block[:block.index("/* A window opened for a view")]
    assert "isGameApp(r)" in block
    assert "await pcWM.fullscreen(id,true)" in block
    assert "_gameFullscreenAsked.has(id)" in block
    assert "_gameFullscreenAsked.add(id)" in block
    assert "_gameFullscreenAsked.delete(id)" in block


def test_proton_gets_bounded_late_metadata_reconciliation():
    """A final steam_app WM_CLASS can arrive well after the first XWayland map event."""
    assert "for(const ms of [180,900,2500]) setTimeout(reconcile, ms)" in OS_JS


def test_native_event_path_fullscreens_every_late_proton_surface_once():
    """The actual Cyberpunk launch creates two 1030x771 surfaces after REDlauncher exits. Both
    final-class events must be handled in main, independent of renderer/output timing."""
    main = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
    fn = main[main.index("function enforceNativeGameFullscreen(ev){"):]
    fn = fn[:fn.index("\nasync function wireShellRecovery")]
    assert r"steam_app_\d+" in fn and "gamescope" in fn
    assert "c.window_properties" in fn and "p.class" in fn
    assert "wm().fullscreen(id,true)" in fn
    assert "_nativeGameFullscreenAsked.has(id)" in fn
    assert "ev.change==='close'" in fn and "_nativeGameFullscreenAsked.delete(id)" in fn
    assert "wm().on('window', enforceNativeGameFullscreen)" in main


def test_native_event_path_requeries_the_tree_for_wm_class():
    """Live Sway emitted no class-change event: the event stayed anonymous while get_tree already
    exposed steam_app_1091500. Bounded native sweeps must therefore inspect wm.windows()."""
    main = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
    fn = main[main.index("async function reconcileNativeGameFullscreen(){"):]
    fn = fn[:fn.index("function enforceNativeGameFullscreen(ev){")]
    assert "await wm().windows()" in fn
    assert "row&&row.app" in fn
    assert "wm().fullscreen(id,true)" in fn
    assert "for(const ms of [180,900,2500])" in fn
    assert "if(_nativeGameReconcileTimers.size)return" in fn
    event = main[main.index("function enforceNativeGameFullscreen(ev){"):]
    event = event[:event.index("async function wireShellRecovery")]
    assert "scheduleNativeGameReconcile()" in event


def test_new_firefox_window_is_expanded_once_not_left_as_a_square_preview():
    main = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
    fn = main[main.index("async function reconcileNativeGameFullscreen(){"):]
    fn = fn[:fn.index("function scheduleNativeGameReconcile(){")]
    assert "/firefox/i.test(identity)" in fn
    assert "wm().snap(id,'max')" in fn
    assert "_nativeBrowserSized.has(id)" in fn
    assert "_nativeBrowserSized.add(id)" in fn
    assert "_nativeBrowserSized.delete(id)" in fn


def test_the_class_is_read_from_either_wayland_or_x11():
    """Steam and most games are XWayland, so `app_id` is empty and the class is the only name they
    have — wm.js folds both into `app` for exactly this reason."""
    fn = OS_JS[OS_JS.index("function isGameApp(nw){"):]
    fn = fn[:fn.index("\n  }") + 4]
    assert "nw.app" in fn and "nw.class" in fn
    assert "catch(_){ return false; }" in fn, "an unreadable window throws into the placement pass"
