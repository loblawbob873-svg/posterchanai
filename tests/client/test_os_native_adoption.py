"""Regression guards for native-window adoption and stable taskbar controls."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")


def test_every_shell_lookup_accepts_electron_44_wayland_app_id_case():
    """Otherwise the shell adopts itself as a recursive black native window."""
    # Adoption, native sync, Task Manager focus, Alt+Tab's shell-focus handoff, the raise that puts
    # the Alt+Tab chooser above the floating apps. (A general version of that raise briefly
    # existed for the start menu and notifications and was REMOVED: fullscreening the shell hides
    # every window on the workspace, so opening the menu emptied the desktop.)
    #
    # THE COUNT IS A PROXY AND THE PROPERTY IS BELOW. It has now been bumped twice by legitimate new
    # call sites; what actually matters is that no site spells the lookup any other way, which the
    # next assertion checks directly.
    assert OS.count("/^(?:posterchan(?:-desktop)?|place\\.poster\\.desktop)$/i") == 5
    # And no site spells it any other way: a lookup that misses `place.poster.desktop` adopts the
    # shell as a recursive black native window, which is the failure this guard exists for.
    assert "posterchan(?:-desktop)?" not in OS.replace(
        "/^(?:posterchan(?:-desktop)?|place\\.poster\\.desktop)$/i", "")


def test_late_xwayland_metadata_gets_a_reconciliation_pass():
    """Firefox may have no class/title in window::new; a later event must still frame it."""
    event = OS[OS.index("if(ev.name === 'window')") : OS.index("if(ev.name !== 'tick')")]
    assert "adoptAll()" in event
    assert "setTimeout(reconcile" in event


def test_native_adoption_loads_installed_icons_before_building_task_rows():
    """A machine app launched before Start opens still gets its real icon on the taskbar."""
    start = OS.index("async function adoptAll()")
    end = OS.index("function closeWin(w, opts)", start)
    body = OS[start:end]
    assert body.index("await PCOSShell.machineApps()") < body.index("PCOSShell.taskbarRows(list)")


def test_native_taskbar_menu_has_the_same_snap_layouts_as_posterchan_windows():
    assert "{label:'Snap left'" in OS
    assert "{label:'Snap right'" in OS
    assert "{label:'Maximize'" in OS
    assert "pcWM.snap(w.id,'left')" in OS


def test_early_native_taskbar_move_adopts_then_uses_live_frame_movement():
    start = OS.index("function nativeTaskbarMove(row)")
    body = OS[start:OS.index("// ---- snapping", start)]
    assert "nativeWins().find" in body
    assert "adoptNative(row)" in body
    assert "taskbarMove(w)" in body
    assert "pcWM.command(" not in body


def test_hardware_watch_does_not_rebuild_the_start_button():
    """Battery/volume events update the tray without replacing the Start image DOM node."""
    watch = OS[OS.index("PCOSShell.watch(() =>") : OS.index("}).then(off =>", OS.index("PCOSShell.watch(() =>"))]
    assert "PCOSShell.paintTray(shell)" in watch
    assert "if(changed){ drawBar(); return; }" in watch


def test_firefox_quit_removes_its_frame_in_the_window_event_reconciliation():
    """There must be no four-second black placeholder after the compositor closes Firefox."""
    start = OS.index("async function adoptAll()")
    end = OS.index("function closeWin(w, opts)", start)
    adopt = OS[start:end]
    assert "if(!r){" in adopt
    assert "closeWin(w, { killNative:false" in adopt
    assert adopt.index("closeWin(w, { killNative:false") < adopt.index("return changed;")


def test_cancelled_native_folder_chooser_uses_the_same_immediate_close_cleanup():
    """Blossom's webkitdirectory chooser is native too; Cancel must not leave a black frame."""
    event = OS[OS.index("if(ev.name === 'window')") : OS.index("if(ev.name !== 'tick')")]
    start = OS.index("async function adoptAll()")
    end = OS.index("function closeWin(w, opts)", start)
    adopt = OS[start:end]
    assert "reconcile()" in event
    assert "if(ev.change === 'new')" in event
    # Close events take the immediate pass; they do not depend on the delayed metadata retry.
    assert event.index("reconcile();") < event.index("if(ev.change === 'new')")
    assert "closeWin(w, { killNative:false" in adopt


def test_cross_monitor_disappearance_detaches_without_killing_or_stealing_focus():
    """Scoped rows omit a window during handoff; global ids distinguish that from Quit."""
    start = OS.index("async function adoptAll()")
    end = OS.index("function closeWin(w, opts)", start)
    adopt = OS[start:end]
    assert "allIds.has(Number(w.native))" in adopt
    assert "preserveFocus:!!(allIds && allIds.has(Number(w.native)))" in adopt


def test_recovery_restores_sways_focused_native_after_enumerating_all_frames():
    """Creating each recovered frame focuses it; enumeration order must not become z-order."""
    start = OS.index("async function adoptAll()")
    end = OS.index("function closeWin(w, opts)", start)
    adopt = OS[start:end]
    # `!r.own` joined the condition when a popped-out PosterChan window became a taskbar row: it is
    # our own toplevel, and hosting it would wrap this client in a screenshot of itself.
    created = adopt.index("for(const r of rows) if(!r.own && !nativeWins()")
    restored = adopt.index("const focusedNative = rows.find")
    assert restored > created
    assert "focusWin(fw, false)" in adopt[restored:]
