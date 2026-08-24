"""Regression guards for native-window adoption and stable taskbar controls."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")


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


def test_hardware_watch_does_not_rebuild_the_start_button():
    """Battery/volume events update the tray without replacing the Start image DOM node."""
    watch = OS[OS.index("PCOSShell.watch(() =>") : OS.index("}).then(off =>", OS.index("PCOSShell.watch(() =>"))]
    assert "PCOSShell.paintTray(shell)" in watch
    assert "if(changed){ drawBar(); return; }" in watch
