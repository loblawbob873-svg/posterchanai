from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "desktop/main.js").read_text()
PRELOAD = (ROOT / "desktop/preload.js").read_text()
OS = (ROOT / "static/js/client/os.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()


def test_native_monitor_handoff_notifies_only_the_destination_renderer():
    handler = MAIN.split("ipcMain.handle('pc:wm:handoff'", 1)[1].split(
        "ipcMain.handle('pc:wm:handoff-frame'", 1)[0]
    assert "record.browser.webContents.send('pc:wm:native-handoff', moved)" in handler
    assert handler.index("_nativeOwners.set") < handler.index("pc:wm:native-handoff")
    assert "BrowserWindow.getAllWindows" not in handler


def test_destination_adopts_and_decorates_before_periodic_reconciliation():
    assert "onNativeHandoff" in PRELOAD
    receiver = OS.split("if(pcWM.onNativeHandoff)", 1)[1].split(
        "if(pcWM.onHandoffFrame)", 1)[0]
    assert "adoptNative(row)" in receiver
    assert "pcWM.decorate(id)" in receiver
    assert "requestAnimationFrame(()=>nsync())" in receiver
    assert "_nativeHandoffOff()" in OS


def test_native_recovery_surface_is_never_plain_black():
    rule = CSS.split(".osw-native .osw-body{", 1)[1].split("}", 1)[0]
    assert "var(--panel" in rule
    assert "#05050c" not in rule
