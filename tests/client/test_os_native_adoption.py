"""Regression guards for native-window adoption and stable taskbar controls."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")


def test_late_xwayland_metadata_gets_a_reconciliation_pass():
    """Firefox may have no class/title in window::new; a later event must still frame it."""
    event = OS[OS.index("if(ev.name === 'window')") : OS.index("if(ev.name !== 'tick')")]
    assert "adoptAll()" in event
    assert "setTimeout(reconcile" in event


def test_hardware_watch_does_not_rebuild_the_start_button():
    """Battery/volume events update the tray without replacing the Start image DOM node."""
    watch = OS[OS.index("PCOSShell.watch(() =>") : OS.index("}).then(off =>", OS.index("PCOSShell.watch(() =>"))]
    assert "PCOSShell.paintTray(shell)" in watch
    assert "if(changed){ drawBar(); return; }" in watch

